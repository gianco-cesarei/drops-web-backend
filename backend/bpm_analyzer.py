"""Analisi BPM locale con FFmpeg + NumPy, isolata dal flusso download."""

from __future__ import annotations

import math
import shutil
import subprocess
from pathlib import Path
from typing import Any


ANALYZER_SOURCE = "drops-local-rhythm-v1"
MIN_BPM = 50.0
MAX_BPM = 220.0
MAX_ANALYSIS_SECONDS = 180.0
SAMPLE_RATE = 11025
FRAME_LENGTH = 1024
HOP_LENGTH = 256


class BpmAnalysisError(RuntimeError):
    pass


def _tempo_candidates(primary: float) -> list[float]:
    candidates: list[float] = []
    for value in (primary, primary / 2.0, primary * 2.0):
        if MIN_BPM <= value <= MAX_BPM and all(abs(value - old) >= 1.0 for old in candidates):
            candidates.append(round(value, 2))
    return candidates


def _decode_audio(path: Path, ffmpeg_path: str | None, max_seconds: float):
    try:
        import numpy as np
    except ImportError as exc:
        raise BpmAnalysisError("Motore numerico BPM non installato") from exc

    executable = ffmpeg_path or shutil.which("ffmpeg")
    if not executable:
        raise BpmAnalysisError("FFmpeg non disponibile per analisi BPM")
    command = [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-t",
        str(max_seconds),
        "-i",
        str(path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-f",
        "f32le",
        "pipe:1",
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max_seconds + 30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BpmAnalysisError(f"Decodifica audio fallita: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace")[:240]
        raise BpmAnalysisError(f"FFmpeg non legge file audio: {detail}")
    audio = np.frombuffer(result.stdout, dtype="<f4")
    if audio.size < FRAME_LENGTH * 4:
        raise BpmAnalysisError("Audio troppo breve per analisi BPM")
    return audio, np


def analyze_bpm(
    path: Path,
    max_seconds: float = MAX_ANALYSIS_SECONDS,
    ffmpeg_path: str | None = None,
) -> dict[str, Any]:
    """Stima BPM da file audio. Errore non invalida download già completato."""
    if not path.is_file():
        raise BpmAnalysisError("File audio non trovato")

    audio, np = _decode_audio(path, ffmpeg_path, max_seconds)
    try:
        frames = np.lib.stride_tricks.sliding_window_view(audio, FRAME_LENGTH)[::HOP_LENGTH]
        window = np.hanning(FRAME_LENGTH).astype(np.float32)
        magnitude = np.abs(np.fft.rfft(frames * window, axis=1))
        log_magnitude = np.log1p(magnitude)
        flux = np.maximum(np.diff(log_magnitude, axis=0), 0.0).sum(axis=1)

        local_mean = np.convolve(flux, np.ones(16) / 16.0, mode="same")
        onset = np.maximum(flux - local_mean, 0.0)
        onset_max = float(np.max(onset)) if onset.size else 0.0
        if onset_max <= 1e-9:
            raise BpmAnalysisError("Ritmo non rilevabile")
        onset = onset / onset_max

        fft_size = 1 << (2 * onset.size - 1).bit_length()
        spectrum = np.fft.rfft(onset, fft_size)
        autocorrelation = np.fft.irfft(spectrum * np.conj(spectrum), fft_size)[: onset.size]
        autocorrelation /= np.arange(onset.size, 0, -1)

        frame_rate = SAMPLE_RATE / HOP_LENGTH
        min_lag = max(1, int(frame_rate * 60.0 / MAX_BPM))
        max_lag = min(onset.size - 2, int(frame_rate * 60.0 / MIN_BPM) + 1)
        if max_lag <= min_lag:
            raise BpmAnalysisError("Audio troppo breve per stimare periodicità")

        lags = np.arange(min_lag, max_lag + 1)
        bpms = 60.0 * frame_rate / lags
        # Prior molto largo centrato su 120: riduce errori half/double senza
        # impedire valori validi tra 50 e 220 BPM.
        prior = np.exp(-0.5 * (np.log2(bpms / 120.0) / 0.75) ** 2)
        scores = autocorrelation[lags] * prior
        peak_index = int(np.argmax(scores))
        peak_lag = float(lags[peak_index])

        # Interpolazione parabolica: supera risoluzione intera dei frame.
        if 0 < peak_index < scores.size - 1:
            left, center, right = scores[peak_index - 1 : peak_index + 2]
            denominator = float(left - 2.0 * center + right)
            if abs(denominator) > 1e-12:
                peak_lag += 0.5 * float(left - right) / denominator

        tempo = 60.0 * frame_rate / peak_lag
        if not math.isfinite(tempo) or not MIN_BPM <= tempo <= MAX_BPM:
            raise BpmAnalysisError("Stima BPM fuori intervallo")

        raw_peak = float(autocorrelation[int(round(peak_lag))])
        zero_lag = float(autocorrelation[0]) or 1.0
        periodicity = max(0.0, min(1.0, raw_peak / zero_lag * 4.0))
        excluded = np.ones(scores.size, dtype=bool)
        excluded[max(0, peak_index - 2) : peak_index + 3] = False
        second = float(np.max(scores[excluded])) if np.any(excluded) else 0.0
        separation = max(0.0, min(1.0, (float(scores[peak_index]) - second) / (abs(float(scores[peak_index])) + 1e-9) * 4.0))
        confidence = round(0.75 * periodicity + 0.25 * separation, 3)

        local_peaks = (
            (onset[1:-1] > onset[:-2])
            & (onset[1:-1] >= onset[2:])
            & (onset[1:-1] >= np.percentile(onset, 75))
        )
        seconds = float(audio.size) / SAMPLE_RATE
        return {
            "bpm": round(tempo, 2),
            "bpm_rounded": int(round(tempo)),
            "bpm_confidence": confidence,
            "bpm_candidates": _tempo_candidates(tempo),
            "bpm_source": ANALYZER_SOURCE,
            "bpm_manual": False,
            "bpm_beats_detected": int(np.count_nonzero(local_peaks)),
            "bpm_analyzed_seconds": round(seconds, 2),
        }
    except BpmAnalysisError:
        raise
    except Exception as exc:
        raise BpmAnalysisError(f"Analisi BPM fallita: {exc}") from exc
