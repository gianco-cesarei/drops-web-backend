"""Precision tests for bpm_analyzer.analyze_bpm on real WAV/MP3 sample files
(Task 3: "verificare la precisione del calcolo BPM su file campione WAV/MP3").

Uses synthetic click tracks (tests/audio_fixtures.py) at exact, known tempos
rather than real music, so the pass/fail boundary tests the analyzer's
accuracy rather than the ambiguity of a real recording. Both WAV and an
ffmpeg-encoded MP3 of the same signal are analyzed so a codec round-trip
can't silently shift onset timing.

Skipped when ffmpeg isn't on PATH - analyze_bpm shells out to it to decode,
same hard runtime dependency the Dockerfile installs, so this is "skip when
the environment can't run the real thing," not a soft/optional check.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from audio_fixtures import make_click_track_wav
from bpm_analyzer import BpmAnalysisError, analyze_bpm

FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None
pytestmark = pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="ffmpeg not installed")

# Click tracks are near-noiseless, so this bounds the analyzer's own error,
# not measurement noise. 90/128/174 sit clear of the algorithm's MIN/MAX_BPM
# edges and its half/double-tempo prior center.
TOLERANCE_BPM = 2.0
MIN_CONFIDENCE = 0.5


def _encode_mp3(wav_path: Path, mp3_path: Path) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav_path), "-codec:a", "libmp3lame", "-b:a", "192k", str(mp3_path)],
        check=True,
        timeout=30,
    )
    return mp3_path


@pytest.mark.parametrize("bpm", [90.0, 128.0, 174.0])
def test_wav_click_track_bpm_within_tolerance(tmp_path: Path, bpm: float):
    wav_path = make_click_track_wav(tmp_path / f"click_{int(bpm)}.wav", bpm, duration_s=12.0)
    result = analyze_bpm(wav_path, max_seconds=12.0)
    assert abs(result["bpm"] - bpm) <= TOLERANCE_BPM, result
    assert result["bpm_confidence"] >= MIN_CONFIDENCE, result


@pytest.mark.parametrize("bpm", [90.0, 128.0, 174.0])
def test_mp3_click_track_bpm_within_tolerance(tmp_path: Path, bpm: float):
    wav_path = make_click_track_wav(tmp_path / f"click_{int(bpm)}.wav", bpm, duration_s=12.0)
    mp3_path = _encode_mp3(wav_path, tmp_path / f"click_{int(bpm)}.mp3")
    result = analyze_bpm(mp3_path, max_seconds=12.0)
    assert abs(result["bpm"] - bpm) <= TOLERANCE_BPM, result
    assert result["bpm_confidence"] >= MIN_CONFIDENCE, result


def test_wav_and_mp3_agree_closely(tmp_path: Path):
    """The codec round-trip shouldn't meaningfully shift the estimate."""
    wav_path = make_click_track_wav(tmp_path / "click.wav", 128.0, duration_s=12.0)
    mp3_path = _encode_mp3(wav_path, tmp_path / "click.mp3")
    wav_result = analyze_bpm(wav_path, max_seconds=12.0)
    mp3_result = analyze_bpm(mp3_path, max_seconds=12.0)
    assert abs(wav_result["bpm"] - mp3_result["bpm"]) <= 0.5


def test_silence_raises_bpm_analysis_error(tmp_path: Path):
    import wave

    path = tmp_path / "silence.wav"
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(22050)
        wf.writeframes(b"\x00\x00" * 22050 * 5)
    with pytest.raises(BpmAnalysisError):
        analyze_bpm(path, max_seconds=5.0)


def test_missing_file_raises_bpm_analysis_error(tmp_path: Path):
    with pytest.raises(BpmAnalysisError):
        analyze_bpm(tmp_path / "does-not-exist.wav")
