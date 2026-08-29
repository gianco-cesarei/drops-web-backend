"""Synthetic audio generation for BPM analyzer tests.

Not a test module itself (no test_ functions) - a shared helper imported by
the tests that need real audio bytes with a known, exact tempo.
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path


def make_click_track_wav(path: Path, bpm: float, *, duration_s: float = 12.0, sample_rate: int = 22050) -> Path:
    """Write a mono 16-bit PCM WAV of decaying clicks at an exact ``bpm``.

    A click track is the sharpest possible test signal for an onset-detection
    tempo estimator: each beat is an unambiguous transient, so the analyzer's
    accuracy is bounded by the algorithm itself rather than by how "musical"
    the source is - exactly what a unit test for the analyzer wants.
    """
    n_samples = int(duration_s * sample_rate)
    samples = [0.0] * n_samples
    click_len = int(0.02 * sample_rate)  # ~20ms decaying click
    beat_interval = 60.0 / bpm
    t = 0.0
    while t < duration_s:
        start = int(t * sample_rate)
        for i in range(click_len):
            idx = start + i
            if idx >= n_samples:
                break
            envelope = math.exp(-i / (click_len * 0.25))
            samples[idx] += envelope * math.sin(2 * math.pi * 2000 * i / sample_rate)
        t += beat_interval
    peak = max(1e-9, max(abs(s) for s in samples))
    pcm = struct.pack(
        f"<{n_samples}h",
        *(int(max(-1.0, min(1.0, s / peak)) * 32767 * 0.9) for s in samples),
    )
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return path
