"""Unit tests for the aioboto3-based async R2 path (r2_storage_async.py).

No real network / aioboto3 wiring involved: ``_client_ctx`` is monkeypatched
with a fake async context manager, and ``upload_file_async`` is monkeypatched
directly where the batch-concurrency test only cares about scheduling, not
the S3 call itself. Mirrors the offline-only style of the rest of the suite
(see tests/conftest.py docstring).

Run with plain ``asyncio.run`` inside sync test functions rather than
pytest-asyncio, so no extra test dependency is needed.
"""

from __future__ import annotations

import asyncio

import pytest

import r2_storage_async as r2a


class _FakeAsyncClient:
    def __init__(self, *, fail_with: Exception | None = None):
        self.fail_with = fail_with
        self.upload_calls: list[tuple[str, str, str]] = []
        self.presign_calls: list[tuple[str, int]] = []
        self.delete_calls: list[str] = []

    async def upload_file(self, *, Filename, Bucket, Key, ExtraArgs=None):
        if self.fail_with:
            raise self.fail_with
        self.upload_calls.append((Filename, Bucket, Key))

    async def generate_presigned_url(self, operation, *, Params, ExpiresIn):
        if self.fail_with:
            raise self.fail_with
        self.presign_calls.append((Params["Key"], ExpiresIn))
        return f"https://r2.example/{Params['Key']}?exp={ExpiresIn}"

    async def delete_object(self, *, Bucket, Key):
        if self.fail_with:
            raise self.fail_with
        self.delete_calls.append(Key)


class _FakeClientCtx:
    """Async context manager standing in for aioboto3's client() call."""

    def __init__(self, client: _FakeAsyncClient):
        self.client = client

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *exc_info):
        return False


@pytest.fixture
def fake_client(monkeypatch):
    client = _FakeAsyncClient()
    monkeypatch.setattr(r2a, "_client_ctx", lambda: _FakeClientCtx(client))
    monkeypatch.setattr(r2a, "is_configured", lambda: True)
    monkeypatch.setattr(r2a, "bucket_name", lambda: "drops-library")
    monkeypatch.setattr(r2a, "presign_ttl_seconds", lambda: 3600)
    return client


def test_upload_file_async_not_configured(monkeypatch):
    monkeypatch.setattr(r2a, "is_configured", lambda: False)
    with pytest.raises(r2a.R2Error, match="not configured"):
        asyncio.run(r2a.upload_file_async("/tmp/x.mp3", "dj/Techno/A - B.mp3"))


def test_upload_file_async_success(fake_client):
    key = asyncio.run(r2a.upload_file_async("/tmp/x.mp3", "dj/Techno/A - B.mp3"))
    assert key == "dj/Techno/A - B.mp3"
    assert fake_client.upload_calls == [("/tmp/x.mp3", "drops-library", "dj/Techno/A - B.mp3")]


def test_upload_file_async_wraps_client_errors(monkeypatch):
    client = _FakeAsyncClient(fail_with=RuntimeError("boom"))
    monkeypatch.setattr(r2a, "_client_ctx", lambda: _FakeClientCtx(client))
    monkeypatch.setattr(r2a, "is_configured", lambda: True)
    monkeypatch.setattr(r2a, "bucket_name", lambda: "drops-library")
    with pytest.raises(r2a.R2Error, match="upload failed: RuntimeError"):
        asyncio.run(r2a.upload_file_async("/tmp/x.mp3", "key.mp3"))


def test_generate_presigned_url_async_clamps_ttl(fake_client):
    url = asyncio.run(r2a.generate_presigned_url_async("key.mp3", expires_in=5))
    # clamped up to the 60s floor, same rule as the sync module.
    assert url == "https://r2.example/key.mp3?exp=60"
    assert fake_client.presign_calls == [("key.mp3", 60)]


def test_generate_presigned_url_async_not_configured(monkeypatch):
    monkeypatch.setattr(r2a, "is_configured", lambda: False)
    with pytest.raises(r2a.R2Error):
        asyncio.run(r2a.generate_presigned_url_async("key.mp3"))


def test_delete_object_async_best_effort_swallows_errors(monkeypatch):
    client = _FakeAsyncClient(fail_with=RuntimeError("gone"))
    monkeypatch.setattr(r2a, "_client_ctx", lambda: _FakeClientCtx(client))
    monkeypatch.setattr(r2a, "is_configured", lambda: True)
    monkeypatch.setattr(r2a, "bucket_name", lambda: "drops-library")
    # must not raise
    asyncio.run(r2a.delete_object_async("key.mp3"))


def test_delete_object_async_noop_when_not_configured(monkeypatch):
    monkeypatch.setattr(r2a, "is_configured", lambda: False)
    calls = []
    monkeypatch.setattr(r2a, "_client_ctx", lambda: calls.append("should not be called"))
    asyncio.run(r2a.delete_object_async("key.mp3"))
    assert calls == []


def test_upload_many_async_empty_list_short_circuits():
    assert asyncio.run(r2a.upload_many_async([])) == []


def test_upload_many_async_mixed_success_and_failure(monkeypatch):
    async def fake_upload(local_path, key, *, content_type="audio/mpeg"):
        if "bad" in key:
            raise r2a.R2Error("simulated failure")
        return key

    monkeypatch.setattr(r2a, "upload_file_async", fake_upload)
    results = asyncio.run(
        r2a.upload_many_async(
            [("/tmp/a.mp3", "dj/a.mp3"), ("/tmp/b.mp3", "dj/bad.mp3")]
        )
    )
    by_key = {r.key: r for r in results}
    assert by_key["dj/a.mp3"].ok is True
    assert by_key["dj/bad.mp3"].ok is False
    assert "simulated failure" in by_key["dj/bad.mp3"].error


def test_upload_many_async_respects_concurrency_cap(monkeypatch):
    active = 0
    peak = 0
    lock = asyncio.Lock()

    async def fake_upload(local_path, key, *, content_type="audio/mpeg"):
        nonlocal active, peak
        async with lock:
            active += 1
            peak = max(peak, active)
        await asyncio.sleep(0.01)
        async with lock:
            active -= 1
        return key

    monkeypatch.setattr(r2a, "upload_file_async", fake_upload)
    items = [(f"/tmp/{i}.mp3", f"dj/{i}.mp3") for i in range(10)]
    results = asyncio.run(r2a.upload_many_async(items, max_concurrency=3))
    assert all(r.ok for r in results)
    assert peak <= 3
