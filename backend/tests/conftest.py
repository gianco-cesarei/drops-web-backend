"""Shared pytest fixtures.

The suite runs entirely offline: yt-dlp, Discogs, R2 and the BPM analyzer are
all monkeypatched so tests exercise *our* control flow (validation, auth,
job/catalog state, presigned-url minting, ownership) without any network or
external service. The FastAPI executor is swapped for a synchronous one so a
download job runs to completion inside the request, making assertions
deterministic.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from argon2 import PasswordHasher  # noqa: E402

TEST_USERNAME = "dj"
TEST_PASSWORD = "correct horse battery staple"


class _SyncExecutor:
    """Runs submitted callables inline so download jobs finish within the
    request. Mirrors the tiny slice of ThreadPoolExecutor the app uses."""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)
        return None

    def shutdown(self, *args, **kwargs):
        pass


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def base_env(state_dir: Path, monkeypatch):
    password_hash = PasswordHasher().hash(TEST_PASSWORD)
    env = {
        "DROPS_WEB_USERNAME": TEST_USERNAME,
        "DROPS_WEB_PASSWORD_HASH": password_hash,
        "DROPS_WEB_ENV": "test",
        "DROPS_WEB_STATE_DIR": str(state_dir),
        "DROPS_WEB_COOKIE_SECURE": "0",
        "DROPS_WEB_ALLOW_MISSING_ORIGIN": "1",
        "DROPS_WEB_SESSION_SECRET": "test-secret-please-change",
        # No DATABASE_URL -> TrackStore uses a local SQLite file under state_dir.
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    # Clear any R2/DB config leaking in from the real environment.
    for key in (
        "DATABASE_URL", "DROPS_R2_ACCOUNT_ID", "DROPS_R2_ACCESS_KEY_ID",
        "DROPS_R2_SECRET_ACCESS_KEY", "DROPS_R2_BUCKET", "DROPS_R2_ENDPOINT_URL",
        "DROPS_YTDLP_COOKIES",
    ):
        monkeypatch.delenv(key, raising=False)
    return env


@pytest.fixture
def fake_media(monkeypatch):
    """Patch the download/enrich/bpm externals in the web_app namespace."""
    import web_app

    def fake_resolve_track(url):
        return {
            "title": "Exclusive Track",
            "artist": "Test Artist",
            "raw_title": "Test Artist - Exclusive Track",
            "cover_url": None,
            "duration": 200,
        }

    def _write_audio(job_dir: Path):
        job_dir.mkdir(parents=True, exist_ok=True)
        f = job_dir / "source.mp3"
        f.write_bytes(b"ID3fake-audio-bytes")
        return f

    def fake_attempt_download(job_dir, url, quality, settings, started, *, proxy=None):
        _write_audio(Path(job_dir))
        return {"title": "Exclusive Track", "duration": 200}

    def fake_multi_source(job_dir, job_id, native_url, artist, title, duration,
                          quality, settings, started, *, proxy=None, raw_title=None, catalog_no=None):
        _write_audio(Path(job_dir))
        return {"title": title or "Exclusive Track", "duration": 200}, "youtube"

    def fake_tag(*args, **kwargs):
        return True

    def fake_bpm(path, max_seconds=120):
        return {"bpm": 128.0, "bpm_confidence": 0.91}

    monkeypatch.setattr(web_app, "resolve_track", fake_resolve_track)
    monkeypatch.setattr(web_app, "attempt_download", fake_attempt_download)
    monkeypatch.setattr(web_app, "download_multi_source", fake_multi_source)
    monkeypatch.setattr(web_app, "tag_audio_file", fake_tag)
    monkeypatch.setattr(web_app, "analyze_bpm", fake_bpm)
    return {"resolve": fake_resolve_track}


@pytest.fixture
def fake_r2(monkeypatch):
    """Patch r2_storage as if R2 were configured, without any network.

    ``objects`` simulates what's actually sitting in the bucket - keyed by R2
    object key, valued by its raw bytes. ``upload_file`` (the sync single-track
    path) populates it automatically; the Academy presign flow doesn't (the
    browser uploads directly to R2, bypassing this backend), so tests for that
    flow seed ``objects[key] = b"..."`` themselves to simulate "the direct
    upload landed" before calling the complete/stream endpoints.
    """
    import io
    import re

    import r2_storage

    uploaded: list[str] = []
    objects: dict[str, bytes] = {}

    monkeypatch.setattr(r2_storage, "is_configured", lambda: True)
    monkeypatch.setattr(r2_storage, "presign_ttl_seconds", lambda: 3600)

    def fake_upload(local_path, key, *, content_type="audio/mpeg"):
        uploaded.append(key)
        objects[key] = b"fake-audio-bytes"
        return key

    def fake_presign(key, *, expires_in=None):
        return f"https://r2.example/{key}?sig=deadbeef&exp={expires_in or 3600}"

    def fake_presigned_post(key, *, content_type, max_bytes, expires_in=None):
        return {
            "url": "https://r2.example/upload",
            "fields": {"key": key, "Content-Type": content_type, "policy": "fake-policy"},
        }

    def fake_head_object(key):
        if key not in objects:
            raise r2_storage.R2NotFoundError(f"object not found: {key}")
        return {"ContentLength": len(objects[key]), "ContentType": "audio/mpeg"}

    _RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")

    def fake_get_object(key, *, range_header=None):
        if key not in objects:
            raise r2_storage.R2NotFoundError(f"object not found: {key}")
        data = objects[key]
        total = len(data)
        if not range_header:
            return {"Body": io.BytesIO(data), "ContentLength": total, "ContentType": "audio/mpeg"}
        match = _RANGE_RE.match(range_header)
        if not match or not (match.group(1) or match.group(2)):
            raise r2_storage.R2InvalidRangeError(f"bad range: {range_header}", total_size=total)
        start_s, end_s = match.groups()
        if start_s:
            start, end = int(start_s), (int(end_s) if end_s else total - 1)
        else:
            start, end = max(0, total - int(end_s)), total - 1
        if start >= total or start > end:
            raise r2_storage.R2InvalidRangeError(f"bad range: {range_header}", total_size=total)
        end = min(end, total - 1)
        chunk = data[start:end + 1]
        return {
            "Body": io.BytesIO(chunk),
            "ContentLength": len(chunk),
            "ContentType": "audio/mpeg",
            "ContentRange": f"bytes {start}-{end}/{total}",
        }

    def fake_delete(key):
        objects.pop(key, None)

    monkeypatch.setattr(r2_storage, "upload_file", fake_upload)
    monkeypatch.setattr(r2_storage, "generate_presigned_url", fake_presign)
    monkeypatch.setattr(r2_storage, "generate_presigned_post", fake_presigned_post)
    monkeypatch.setattr(r2_storage, "head_object", fake_head_object)
    monkeypatch.setattr(r2_storage, "get_object", fake_get_object)
    monkeypatch.setattr(r2_storage, "delete_object", fake_delete)
    return {"uploaded": uploaded, "objects": objects}


@pytest.fixture
def make_client(base_env, fake_media):
    """Factory returning an authenticated TestClient with a synchronous executor.

    R2 is left *unconfigured* by default; pass r2_enabled via the fake_r2
    fixture in the test to turn it on.
    """
    from fastapi.testclient import TestClient
    import web_app

    created = []

    def _make():
        app = web_app.create_app()
        client = TestClient(app)
        client.__enter__()  # run lifespan (starts real executor)
        app.state.executor = _SyncExecutor()  # then force synchronous jobs
        # Authenticate.
        resp = client.post("/api/v1/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD})
        assert resp.status_code == 200, resp.text
        created.append(client)
        return client, app

    yield _make

    for client in created:
        try:
            client.__exit__(None, None, None)
        except Exception:
            pass


def join_bpm_threads(timeout: float = 5.0) -> None:
    """Wait for the background BPM/cleanup daemon threads to finish so DB
    assertions (track bpm) and disk cleanup are observable."""
    import threading

    for thread in list(threading.enumerate()):
        if thread.name.startswith(("ytd-bpm-", "bpm-")):
            thread.join(timeout)
