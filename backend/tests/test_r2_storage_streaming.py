"""Unit tests for the R2 helpers added for Academy uploads/streaming:
build_academy_object_key, generate_presigned_post, head_object, get_object
(and their R2NotFoundError / R2InvalidRangeError mapping). A fake boto3-style
client is injected via r2_storage._get_client so no network/credentials are
needed - same offline-only approach as the rest of the suite.
"""

from __future__ import annotations

import pytest

import r2_storage


class _ClientError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeClient:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.presigned_post_calls: list[dict] = []

    def generate_presigned_post(self, *, Bucket, Key, Fields, Conditions, ExpiresIn):
        self.presigned_post_calls.append(
            {"Bucket": Bucket, "Key": Key, "Fields": Fields, "Conditions": Conditions, "ExpiresIn": ExpiresIn}
        )
        return {"url": f"https://r2.example/{Bucket}", "fields": {**Fields, "key": Key}}

    def head_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise _ClientError("404")
        return {"ContentLength": len(self.objects[Key]), "ContentType": "audio/mpeg"}

    def get_object(self, *, Bucket, Key, Range=None):
        if Key not in self.objects:
            raise _ClientError("NoSuchKey")
        data = self.objects[Key]
        if Range is None:
            return {"Body": data, "ContentLength": len(data)}
        if Range == "bytes=9999999-":
            raise _ClientError("InvalidRange")
        return {"Body": data, "ContentLength": len(data), "ContentRange": f"bytes 0-{len(data)-1}/{len(data)}"}


@pytest.fixture
def fake_client(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(r2_storage, "is_configured", lambda: True)
    monkeypatch.setattr(r2_storage, "bucket_name", lambda: "drops-library")
    monkeypatch.setattr(r2_storage, "presign_ttl_seconds", lambda: 3600)
    monkeypatch.setattr(r2_storage, "_get_client", lambda: client)
    return client


def test_build_academy_object_key_layout():
    key = r2_storage.build_academy_object_key("dj", "sub-123", "My Feedback Track.wav")
    assert key == "academy/dj/sub-123/My Feedback Track.wav"


def test_build_academy_object_key_defaults_extension_and_name():
    key = r2_storage.build_academy_object_key("dj", "sub-1", None)
    assert key == "academy/dj/sub-1/track.mp3"


def test_build_academy_object_key_keeps_wav_extension():
    key = r2_storage.build_academy_object_key("dj", "sub-1", "master_final_v2.WAV")
    assert key == "academy/dj/sub-1/master_final_v2.wav"


def test_generate_presigned_post_sets_content_length_range(fake_client):
    result = r2_storage.generate_presigned_post(
        "academy/dj/sub-1/t.wav", content_type="audio/wav", max_bytes=100_000_000,
    )
    assert result["url"] == "https://r2.example/drops-library"
    assert result["fields"]["key"] == "academy/dj/sub-1/t.wav"
    call = fake_client.presigned_post_calls[0]
    assert ["content-length-range", 1, 100_000_000] in call["Conditions"]


def test_generate_presigned_post_not_configured(monkeypatch):
    monkeypatch.setattr(r2_storage, "is_configured", lambda: False)
    with pytest.raises(r2_storage.R2Error):
        r2_storage.generate_presigned_post("k", content_type="audio/wav", max_bytes=100)


def test_head_object_not_found_raises_typed_error(fake_client):
    with pytest.raises(r2_storage.R2NotFoundError):
        r2_storage.head_object("missing/key.wav")


def test_head_object_success(fake_client):
    fake_client.objects["k.wav"] = b"0123456789"
    meta = r2_storage.head_object("k.wav")
    assert meta["ContentLength"] == 10


def test_get_object_full(fake_client):
    fake_client.objects["k.mp3"] = b"abcdefgh"
    obj = r2_storage.get_object("k.mp3")
    assert obj["ContentLength"] == 8
    assert "ContentRange" not in obj


def test_get_object_with_range(fake_client):
    fake_client.objects["k.mp3"] = b"abcdefgh"
    obj = r2_storage.get_object("k.mp3", range_header="bytes=0-3")
    assert obj["ContentRange"] == "bytes 0-7/8"


def test_get_object_not_found(fake_client):
    with pytest.raises(r2_storage.R2NotFoundError):
        r2_storage.get_object("missing.mp3")


def test_get_object_invalid_range_reports_total_size(fake_client):
    fake_client.objects["k.mp3"] = b"abcdefgh"
    with pytest.raises(r2_storage.R2InvalidRangeError) as exc_info:
        r2_storage.get_object("k.mp3", range_header="bytes=9999999-")
    assert exc_info.value.total_size == 8
