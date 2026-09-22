"""
Audio Quality Verifier & Spectrogram Frequency Cutoff Analyzer for DropSoul.
Performs FFT spectral analysis using NumPy and FFmpeg to calculate the true frequency cutoff (Hz),
generate the 22-bar frequency distribution for the frontend UI, and detect fake/upscaled 320k transcodes.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

import numpy as np

logger = logging.getLogger("dropsoul.verifier")


class QualityVerdict(str, Enum):
    FLAC_LOSSLESS = "FLAC_LOSSLESS"          # Lossless / full spectrum >= 21.0 kHz
    VERIFIED_320K = "VERIFIED_320K"          # Cutoff >= 19.5 kHz (Genuine 320k studio standard)
    ACCEPTABLE_192K = "ACCEPTABLE_192K"      # Cutoff 18.0 - 19.5 kHz (192k-256k)
    DOWNSIZED = "DOWNSIZED"                  # Cutoff < 18.0 kHz (WebRip / Fake 320k upscale from 128k)
    HUNTING = "HUNTING"                      # Slot queued waiting for high-quality master
    ANALYSIS_ERROR = "ANALYSIS_ERROR"


@dataclass
class QualityReport:
    is_genuine: bool
    verdict: QualityVerdict
    cutoff_frequency_hz: float
    nominal_bitrate_kbps: Optional[int]
    sample_rate_hz: int
    format: str
    spectrum_bars: List[float] = field(default_factory=list)
    details: str = ""

    def to_dict(self) -> dict:
        return {
            "is_genuine": self.is_genuine,
            "verdict": self.verdict.value,
            "cutoff_frequency_hz": round(self.cutoff_frequency_hz, 1),
            "nominal_bitrate_kbps": self.nominal_bitrate_kbps,
            "sample_rate_hz": self.sample_rate_hz,
            "format": self.format,
            "spectrum_bars": [round(b, 3) for b in self.spectrum_bars],
            "details": self.details,
        }


class AudioQualityVerifier:
    def __init__(
        self,
        hq_threshold_hz: float = 19500.0,
        ffmpeg_bin: Optional[str] = None,
        ffprobe_bin: Optional[str] = None,
    ):
        self.hq_threshold_hz = hq_threshold_hz
        self.ffmpeg_bin = ffmpeg_bin or shutil.which("ffmpeg") or "/usr/bin/ffmpeg" or "/opt/homebrew/bin/ffmpeg"
        self.ffprobe_bin = ffprobe_bin or shutil.which("ffprobe") or "/usr/bin/ffprobe" or "/opt/homebrew/bin/ffprobe"

    def get_audio_metadata(self, file_path: str) -> Tuple[Optional[int], int, str, float]:
        """Returns (nominal_bitrate_kbps, sample_rate, format, duration_seconds)."""
        cmd = [
            self.ffprobe_bin,
            "-v", "error",
            "-show_entries", "format=bit_rate,format_name,duration:stream=sample_rate,bit_rate",
            "-of", "json",
            file_path,
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            info = json.loads(res.stdout)
            format_info = info.get("format", {})
            streams = info.get("streams", [])
            stream_info = streams[0] if streams else {}

            bit_rate = stream_info.get("bit_rate") or format_info.get("bit_rate")
            nominal_bitrate = int(int(bit_rate) / 1000) if bit_rate else None
            sample_rate = int(stream_info.get("sample_rate", 44100))
            fmt = format_info.get("format_name", "unknown").split(",")[0]
            duration = float(format_info.get("duration", 0.0))
            return nominal_bitrate, sample_rate, fmt, duration
        except Exception:
            return None, 44100, "unknown", 0.0

    def analyze(self, file_path: str) -> QualityReport:
        """
        Computes FFT frequency response, determines true cutoff frequency (Hz),
        and generates the 22-bar frequency distribution.
        """
        if not os.path.exists(file_path):
            return QualityReport(
                is_genuine=False,
                verdict=QualityVerdict.ANALYSIS_ERROR,
                cutoff_frequency_hz=0.0,
                nominal_bitrate_kbps=None,
                sample_rate_hz=44100,
                format="none",
                spectrum_bars=[0.05] * 22,
                details=f"File non trovato: {file_path}",
            )

        nominal_bitrate, sample_rate, fmt, duration = self.get_audio_metadata(file_path)

        # Slice 30 seconds from 20% into the song
        offset = max(5.0, duration * 0.2) if duration > 20 else 0.0
        slice_len = 30.0 if duration > 35 else max(5.0, duration)

        target_sr = 44100
        cmd = [
            self.ffmpeg_bin,
            "-v", "error",
            "-ss", str(offset),
            "-t", str(slice_len),
            "-i", file_path,
            "-f", "s16le",
            "-ac", "1",
            "-ar", str(target_sr),
            "-",
        ]

        try:
            proc = subprocess.run(cmd, capture_output=True, check=True)
            raw_audio = proc.stdout
        except Exception as e:
            logger.error("FFmpeg decode error: %s", e)
            return QualityReport(
                is_genuine=False,
                verdict=QualityVerdict.ANALYSIS_ERROR,
                cutoff_frequency_hz=0.0,
                nominal_bitrate_kbps=nominal_bitrate,
                sample_rate_hz=sample_rate,
                format=fmt,
                spectrum_bars=[0.05] * 22,
                details=f"Errore decodifica FFmpeg: {e}",
            )

        if len(raw_audio) < target_sr * 2 * 2:  # at least 2 seconds
            return QualityReport(
                is_genuine=False,
                verdict=QualityVerdict.ANALYSIS_ERROR,
                cutoff_frequency_hz=0.0,
                nominal_bitrate_kbps=nominal_bitrate,
                sample_rate_hz=sample_rate,
                format=fmt,
                spectrum_bars=[0.05] * 22,
                details="Traccia troppo breve per analisi spettrale",
            )

        samples = np.frombuffer(raw_audio, dtype=np.int16).astype(np.float32)

        # Multi-window Averaged STFT
        n_fft = 4096
        hop_size = 2048
        window = np.hanning(n_fft)

        num_frames = (len(samples) - n_fft) // hop_size
        if num_frames <= 0:
            num_frames = 1
            samples = np.pad(samples, (0, max(0, n_fft - len(samples))))

        accumulated_mag = np.zeros(n_fft // 2 + 1, dtype=np.float64)
        frames_to_process = min(num_frames, 200)
        for i in range(frames_to_process):
            start = i * hop_size
            frame = samples[start : start + n_fft] * window
            fft_res = np.abs(np.fft.rfft(frame))
            accumulated_mag += fft_res

        mean_mag = accumulated_mag / max(1, frames_to_process)
        freqs = np.fft.rfftfreq(n_fft, d=1.0 / target_sr)

        max_val = np.max(mean_mag)
        if max_val <= 1e-9:
            cutoff_hz = 0.0
            bars = [0.05] * 22
        else:
            mag_db = 20.0 * np.log10(mean_mag / max_val + 1e-9)

            # Frequency cutoff detection threshold (-56 dB relative to peak)
            noise_threshold_db = -56.0

            # Scan from 21.5 kHz downwards
            cutoff_hz = 0.0
            for idx in range(len(freqs) - 1, -1, -1):
                f = freqs[idx]
                if f > 21800:
                    continue
                if mag_db[idx] > noise_threshold_db:
                    # Require 3 consecutive bins above threshold to avoid isolated clicks
                    prev_bins = mag_db[max(0, idx - 4) : idx + 1]
                    if np.mean(prev_bins) > noise_threshold_db - 3.0:
                        cutoff_hz = float(f)
                        break

            # Build 22 normalized bars (0 Hz to 22050 Hz)
            bars = []
            total_bars = 22
            bar_step = (target_sr / 2.0) / total_bars
            for b in range(total_bars):
                low_f = b * bar_step
                high_f = (b + 1) * bar_step
                mask = (freqs >= low_f) & (freqs < high_f)
                if np.any(mask):
                    val_db = np.mean(mag_db[mask])
                    # Map [-60dB, 0dB] to [0.08, 1.0]
                    norm = (val_db + 60.0) / 60.0
                    norm = float(np.clip(norm, 0.08, 1.0))
                else:
                    norm = 0.08

                # If frequency is above cutoff, sharply attenuate the bar
                if low_f > cutoff_hz:
                    norm = 0.05
                bars.append(norm)

        # Verdict assignment
        is_lossless = fmt in ["flac", "wav", "alac"] or (nominal_bitrate and nominal_bitrate > 600)
        if is_lossless and cutoff_hz >= 21000:
            verdict = QualityVerdict.FLAC_LOSSLESS
            is_genuine = True
        elif cutoff_hz >= self.hq_threshold_hz:
            verdict = QualityVerdict.VERIFIED_320K
            is_genuine = True
        elif cutoff_hz >= 18000:
            verdict = QualityVerdict.ACCEPTABLE_192K
            is_genuine = True
        else:
            verdict = QualityVerdict.DOWNSIZED
            is_genuine = False

        details = f"Cutoff a {cutoff_hz:.0f} Hz (Soglia Studio: {self.hq_threshold_hz:.0f} Hz)"

        return QualityReport(
            is_genuine=is_genuine,
            verdict=verdict,
            cutoff_frequency_hz=cutoff_hz,
            nominal_bitrate_kbps=nominal_bitrate,
            sample_rate_hz=sample_rate,
            format=fmt,
            spectrum_bars=bars,
            details=details,
        )
