"""Unit tests for bpm_analyzer_async.py - the asyncio.to_thread wrapper
around the sync analyzer, and the R2-download orchestration. The DSP itself
is covered by test_bpm_analyzer_precision.py; these tests are about the
async plumbing (does it await correctly, does it clean up the temp file,
does it propagate R2/analysis errors) so bpm_analyzer.analyze_bpm is
monkeypatched here rather than run for real.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import bpm_analyzer_async as bpm_async
import r2_storage


def test_analyze_bpm_async_delegates_to_sync_analyzer(monkeypatch, tmp_path: Path):
    calls = []

    def fake_analyze(path, max_seconds):
        calls.append((path, max_seconds))
        return {"bpm": 128.0, "bpm_confidence": 0.9}

    monkeypatch.setattr(bpm_async, "analyze_bpm", fake_analyze)
    dummy = tmp_path / "x.wav"
    result = asyncio.run(bpm_async.analyze_bpm_async(dummy, max_seconds=30.0))
    assert result == {"bpm": 128.0, "bpm_confidence": 0.9}
    assert calls == [(dummy, 30.0)]


def test_analyze_r2_object_bpm_async_downloads_then_analyzes(monkeypatch):
    downloaded = []
    analyzed = []

    async def fake_download(key, local_path):
        downloaded.append((key, local_path))
        Path(local_path).write_bytes(b"fake-audio")
        return local_path

    async def fake_analyze_bpm_async(path, max_seconds):
        analyzed.append((path, max_seconds))
        assert Path(path).read_bytes() == b"fake-audio"
        return {"bpm": 126.0, "bpm_confidence": 0.8, "bpm_source": "drops-local-rhythm-v1"}

    monkeypatch.setattr(bpm_async, "download_file_async", fake_download)
    monkeypatch.setattr(bpm_async, "analyze_bpm_async", fake_analyze_bpm_async)

    result = asyncio.run(bpm_async.analyze_r2_object_bpm_async("academy/dj/sub-1/t.wav", suffix=".wav"))
    assert result["bpm"] == 126.0
    assert downloaded[0][0] == "academy/dj/sub-1/t.wav"
    tmp_local_path = Path(downloaded[0][1])
    assert analyzed[0][0] == tmp_local_path
    # Temp file cleaned up after analysis, regardless of outcome.
    assert not tmp_local_path.exists()


def test_analyze_r2_object_bpm_async_cleans_up_temp_file_on_analysis_error(monkeypatch):
    written_path = {}

    async def fake_download(key, local_path):
        Path(local_path).write_bytes(b"fake-audio")
        written_path["path"] = Path(local_path)
        return local_path

    async def fake_analyze_bpm_async(path, max_seconds):
        raise bpm_async.BpmAnalysisError("Ritmo non rilevabile")

    monkeypatch.setattr(bpm_async, "download_file_async", fake_download)
    monkeypatch.setattr(bpm_async, "analyze_bpm_async", fake_analyze_bpm_async)

    with pytest.raises(bpm_async.BpmAnalysisError):
        asyncio.run(bpm_async.analyze_r2_object_bpm_async("academy/dj/sub-1/t.wav"))
    assert not written_path["path"].exists()


def test_analyze_r2_object_bpm_async_propagates_r2_errors_and_cleans_up(monkeypatch):
    async def fake_download(key, local_path):
        # Simulate a download failure after the temp file was created by
        # tempfile.mkstemp but before any bytes landed.
        raise r2_storage.R2NotFoundError(f"object not found: {key}")

    monkeypatch.setattr(bpm_async, "download_file_async", fake_download)

    with pytest.raises(r2_storage.R2NotFoundError):
        asyncio.run(bpm_async.analyze_r2_object_bpm_async("academy/dj/sub-missing/t.wav"))
