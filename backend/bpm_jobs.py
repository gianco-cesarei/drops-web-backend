"""Asynchronous, disposable BPM jobs for the private Spotify library."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yt_dlp

from bpm_analyzer import analyze_bpm
from media_core import YTDLP_LOCK, ytdlp_cookiefile, ytdlp_extractor_args


def normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def stable_track_key(track_key: str | None, isrc: str | None, artist: str, title: str) -> str:
    if isrc and isrc.strip():
        return f"isrc:{isrc.strip().upper()}"
    if track_key and track_key.strip():
        return f"spotify:{track_key.strip()}"
    return f"name:{normalize_key(artist)}:{normalize_key(title)}"


class BpmJobManager:
    def __init__(self, state_dir: Path, max_workers: int = 1):
        self.cache_path = state_dir / "bpm-cache.json"
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._cache = self._read_cache()
        self._executor = None
        self.max_workers = max(1, min(2, max_workers))

    def bind_executor(self, executor) -> None:
        self._executor = executor

    def _read_cache(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.cache_path.read_text())
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_cache(self) -> None:
        temporary = self.cache_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._cache, separators=(",", ":")))
        temporary.replace(self.cache_path)

    def submit(self, *, track_key: str | None, artist: str, title: str, isrc: str | None, source_url: str | None) -> dict[str, Any]:
        key = stable_track_key(track_key, isrc, artist, title)
        with self._lock:
            cached = self._cache.get(key)
            if isinstance(cached, dict) and cached.get("bpm") is not None:
                job_id = str(uuid.uuid4())
                self._jobs[job_id] = {"id": job_id, "status": "ready", "track_key": key, **cached}
                return {"job_id": job_id, "status": "ready", "bpm": cached["bpm"], "confidence": cached.get("confidence")}
            if self._executor is None:
                raise RuntimeError("BPM worker unavailable")
            job_id = str(uuid.uuid4())
            self._jobs[job_id] = {"id": job_id, "status": "queued", "track_key": key, "bpm": None, "confidence": None}
            self._executor.submit(self._run, job_id, key, artist, title, isrc, source_url)
            return {"job_id": job_id, "status": "queued"}

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def _run(self, job_id: str, key: str, artist: str, title: str, isrc: str | None, source_url: str | None) -> None:
        with self._lock:
            self._jobs[job_id]["status"] = "running"
        temporary_dir = Path(tempfile.mkdtemp(prefix="drops-bpm-"))
        try:
            result = self._compute(temporary_dir, artist, title, isrc, source_url)
            with self._lock:
                self._cache[key] = {"bpm": result["bpm"], "confidence": result.get("bpm_confidence"), "source": result.get("bpm_source")}
                self._write_cache()
                self._jobs[job_id].update(status="ready", bpm=result["bpm"], confidence=result.get("bpm_confidence"))
        except Exception:
            with self._lock:
                self._jobs[job_id].update(status="error", error="BPM non disponibile. Riprova.")
        finally:
            shutil.rmtree(temporary_dir, ignore_errors=True)

    def _compute(self, directory: Path, artist: str, title: str, isrc: str | None, source_url: str | None) -> dict[str, Any]:
        query = f"{artist} {title}".strip()
        candidates = [source_url] if source_url and self._allowed_source(source_url) else []
        candidates.extend([f"scsearch1:{query}", f"ytsearch1:{query}"])
        cookies = ytdlp_cookiefile()
        last_error: Exception | None = None
        for source in candidates:
            # YouTube's bot-check is intermittent per player client/IP, so retry
            # the same source a couple of times before falling through to the
            # next search engine.
            for attempt in range(1, 3 + 1):
                try:
                    options = {
                        "format": "bestaudio/best", "outtmpl": str(directory / "source.%(ext)s"),
                        "quiet": True, "no_warnings": True, "noplaylist": True,
                        "socket_timeout": 15, "retries": 1,
                        "extractor_args": ytdlp_extractor_args(),
                    }
                    if cookies:
                        options["cookiefile"] = cookies
                    with YTDLP_LOCK, yt_dlp.YoutubeDL(options) as downloader:
                        downloader.extract_info(source, download=True)
                    files = [path for path in directory.iterdir() if path.is_file() and not path.name.endswith((".part", ".ytdl"))]
                    if not files:
                        raise RuntimeError("audio missing")
                    return analyze_bpm(files[0], max_seconds=180)
                except Exception as exc:
                    last_error = exc
                    for path in directory.iterdir():
                        if path.is_file():
                            path.unlink(missing_ok=True)
                    if not isinstance(exc, yt_dlp.utils.DownloadError) or attempt == 3:
                        break
                    time.sleep(1)
        raise RuntimeError("BPM sources unavailable") from last_error

    @staticmethod
    def _allowed_source(value: str) -> bool:
        host = urlparse(value).hostname or ""
        return host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be" or host == "soundcloud.com" or host.endswith(".soundcloud.com")
