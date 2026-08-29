"""Async entry points for BPM analysis, on top of the sync onset/tempo
analyzer in bpm_analyzer.py (FFmpeg decode + NumPy autocorrelation).

Why wrap instead of reimplement: bpm_analyzer.analyze_bpm already does real
onset-detection/autocorrelation tempo estimation (spectral flux onsets, then
autocorrelation of the onset curve with a log-normal prior around 120 BPM to
resolve half/double-tempo ambiguity - see its docstring). Pulling in
librosa or aubio for the same job would add a heavy/compiled dependency
(aubio in particular is notoriously fragile to pip-install) for something
this module already does correctly and has under test.

The work is CPU-bound (NumPy FFTs) plus one blocking subprocess call
(ffmpeg), so "async" here means "doesn't block the FastAPI event loop while
it runs", via asyncio.to_thread - not a rewrite of the DSP itself.

analyze_r2_object_bpm_async is the piece the Academy analyze-bpm endpoint
calls: pull the submission's bytes from R2 (genuinely async I/O, via
r2_storage_async.download_file_async) into a temp file, analyze it, always
clean the temp file up.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from bpm_analyzer import MAX_ANALYSIS_SECONDS, BpmAnalysisError, analyze_bpm
from r2_storage_async import download_file_async

logger = logging.getLogger("drops.bpm.async")

__all__ = ["BpmAnalysisError", "analyze_bpm_async", "analyze_r2_object_bpm_async"]


async def analyze_bpm_async(path: Path, max_seconds: float = MAX_ANALYSIS_SECONDS) -> dict[str, Any]:
    """Async twin of ``bpm_analyzer.analyze_bpm`` - runs the same sync
    analysis in a worker thread so callers can ``await`` it without blocking
    the event loop or occupying a ThreadPoolExecutor download-job slot."""
    return await asyncio.to_thread(analyze_bpm, path, max_seconds)


async def analyze_r2_object_bpm_async(
    r2_key: str, *, suffix: str = ".audio", max_seconds: float = MAX_ANALYSIS_SECONDS,
) -> dict[str, Any]:
    """Download ``r2_key`` from R2 and run BPM analysis on it.

    Raises r2_storage.R2Error (propagated from the download) or
    BpmAnalysisError (propagated from the analysis); the caller (the
    analyze-bpm endpoint) maps both to HTTP responses. The temp file is
    always removed, success or failure.
    """
    fd, tmp_name = tempfile.mkstemp(suffix=suffix, prefix="drops-bpm-")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        await download_file_async(r2_key, str(tmp_path))
        return await analyze_bpm_async(tmp_path, max_seconds=max_seconds)
    finally:
        tmp_path.unlink(missing_ok=True)
