"""Integration tests for the Academy submission endpoints:
presign -> (simulated direct-to-R2 upload) -> complete -> list -> stream.

Uses the TestClient + fake_r2 fixtures from conftest.py. fake_r2's ``objects``
dict simulates the bucket's real contents, letting these tests exercise the
"upload landed" vs. "still pending" vs. "range out of bounds" branches
without any network.
"""

from __future__ import annotations

import r2_storage
import web_app
from bpm_analyzer import BpmAnalysisError


def _presign(client, **overrides):
    payload = {
        "filename": "my_track.wav",
        "content_type": "audio/wav",
        "size_bytes": 5_000_000,
        "title": "Late Night Mix",
        "bpm": 126.0,
        "genre": "Minimal House",
        "focus_area": "Mixdown & Low-end",
    }
    payload.update(overrides)
    return client.post("/api/v1/academy/submissions/presign", json=payload)


# --- presign -----------------------------------------------------------------


def test_presign_requires_auth(make_client, fake_r2):
    client, _ = make_client()
    client.cookies.clear()
    resp = _presign(client)
    assert resp.status_code == 401


def test_presign_503_when_r2_not_configured(make_client):
    client, _ = make_client()
    resp = _presign(client)
    assert resp.status_code == 503


def test_presign_rejects_unsupported_content_type(make_client, fake_r2):
    client, _ = make_client()
    resp = _presign(client, content_type="video/mp4")
    assert resp.status_code == 400


def test_presign_rejects_oversize_file(make_client, fake_r2):
    client, _ = make_client()
    resp = _presign(client, size_bytes=100_000_001)
    assert resp.status_code == 413


def test_presign_rejects_zero_size(make_client, fake_r2):
    client, _ = make_client()
    resp = _presign(client, size_bytes=0)
    assert resp.status_code == 413


def test_presign_success_creates_pending_row(make_client, fake_r2):
    client, app = make_client()
    resp = _presign(client)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["upload_url"] == "https://r2.example/upload"
    assert body["upload_fields"]["key"] == body["key"]
    assert body["max_bytes"] == 100_000_000
    assert body["key"].startswith("academy/dj/")

    row = app.state.academy.get(body["submission_id"])
    assert row["status"] == "pending"
    assert row["title"] == "Late Night Mix"
    assert row["bpm"] == 126.0


# --- complete ------------------------------------------------------------


def test_complete_requires_auth(make_client, fake_r2):
    client, _ = make_client()
    presigned = _presign(client).json()
    client.cookies.clear()
    resp = client.post(f"/api/v1/academy/submissions/{presigned['submission_id']}/complete")
    assert resp.status_code == 401


def test_complete_unknown_submission_404(make_client, fake_r2):
    client, _ = make_client()
    resp = client.post("/api/v1/academy/submissions/nonexistent/complete")
    assert resp.status_code == 404


def test_complete_before_upload_lands_returns_409(make_client, fake_r2):
    client, _ = make_client()
    presigned = _presign(client).json()
    # Nothing written to fake_r2["objects"][key] yet - the direct upload
    # "hasn't landed" from R2's point of view.
    resp = client.post(f"/api/v1/academy/submissions/{presigned['submission_id']}/complete")
    assert resp.status_code == 409


def test_complete_success_marks_ready(make_client, fake_r2):
    client, app = make_client()
    presigned = _presign(client).json()
    fake_r2["objects"][presigned["key"]] = b"RIFF" + b"\x00" * 4_999_996  # simulate the landed upload

    resp = client.post(f"/api/v1/academy/submissions/{presigned['submission_id']}/complete")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ready"
    assert body["size_bytes"] == 5_000_000

    assert app.state.academy.get(presigned["submission_id"])["status"] == "ready"


def test_complete_is_idempotent(make_client, fake_r2):
    client, _ = make_client()
    presigned = _presign(client).json()
    fake_r2["objects"][presigned["key"]] = b"x" * 1000

    first = client.post(f"/api/v1/academy/submissions/{presigned['submission_id']}/complete")
    second = client.post(f"/api/v1/academy/submissions/{presigned['submission_id']}/complete")
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["size_bytes"] == first.json()["size_bytes"]


def test_complete_cannot_be_called_by_another_owner(make_client, fake_r2):
    client, app = make_client()
    row = app.state.academy.create_pending(
        user_id="someone-else", r2_key="academy/someone-else/sub-x/t.wav", content_type="audio/wav",
        submission_id="sub-x",
    )
    fake_r2["objects"][row["r2_key"]] = b"x" * 10
    resp = client.post(f"/api/v1/academy/submissions/{row['submission_id']}/complete")
    assert resp.status_code == 404


# --- list ------------------------------------------------------------------


def test_list_scoped_to_owner(make_client, fake_r2):
    client, app = make_client()
    app.state.academy.create_pending(user_id="dj", r2_key="k1", content_type="audio/mpeg", submission_id="a")
    app.state.academy.create_pending(user_id="other", r2_key="k2", content_type="audio/mpeg", submission_id="b")
    resp = client.get("/api/v1/academy/submissions")
    assert resp.status_code == 200
    ids = [s["submission_id"] for s in resp.json()["submissions"]]
    assert ids == ["a"]


# --- stream ------------------------------------------------------------------


def _ready_submission(app, fake_r2, *, user_id="dj", content=b"abcdefghij" * 100):
    key = f"academy/{user_id}/sub-stream/track.mp3"
    app.state.academy.create_pending(
        user_id=user_id, r2_key=key, content_type="audio/mpeg", submission_id="sub-stream",
    )
    fake_r2["objects"][key] = content
    app.state.academy.mark_ready("sub-stream", size_bytes=len(content))
    return "sub-stream", content


def test_stream_requires_auth(make_client, fake_r2):
    client, app = make_client()
    submission_id, _ = _ready_submission(app, fake_r2)
    client.cookies.clear()
    resp = client.get(f"/api/v1/academy/submissions/{submission_id}/stream")
    assert resp.status_code == 401


def test_stream_unknown_submission_404(make_client, fake_r2):
    client, _ = make_client()
    resp = client.get("/api/v1/academy/submissions/nonexistent/stream")
    assert resp.status_code == 404


def test_stream_another_owners_submission_404(make_client, fake_r2):
    client, app = make_client()
    submission_id, _ = _ready_submission(app, fake_r2, user_id="someone-else")
    resp = client.get(f"/api/v1/academy/submissions/{submission_id}/stream")
    assert resp.status_code == 404


def test_stream_pending_submission_returns_409(make_client, fake_r2):
    client, app = make_client()
    presigned = _presign(client).json()
    resp = client.get(f"/api/v1/academy/submissions/{presigned['submission_id']}/stream")
    assert resp.status_code == 409


def test_stream_full_file_200_with_accept_ranges(make_client, fake_r2):
    client, app = make_client()
    submission_id, content = _ready_submission(app, fake_r2)
    resp = client.get(f"/api/v1/academy/submissions/{submission_id}/stream")
    assert resp.status_code == 200
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.headers["content-length"] == str(len(content))
    assert resp.content == content


def test_stream_range_request_returns_206_partial_content(make_client, fake_r2):
    client, app = make_client()
    submission_id, content = _ready_submission(app, fake_r2)
    resp = client.get(
        f"/api/v1/academy/submissions/{submission_id}/stream", headers={"Range": "bytes=0-9"},
    )
    assert resp.status_code == 206
    assert resp.headers["content-range"] == f"bytes 0-9/{len(content)}"
    assert resp.headers["content-length"] == "10"
    assert resp.content == content[:10]


def test_stream_out_of_bounds_range_returns_416(make_client, fake_r2):
    client, app = make_client()
    submission_id, content = _ready_submission(app, fake_r2)
    resp = client.get(
        f"/api/v1/academy/submissions/{submission_id}/stream",
        headers={"Range": f"bytes={len(content) + 1000}-"},
    )
    assert resp.status_code == 416
    assert resp.headers["content-range"] == f"bytes */{len(content)}"


def test_stream_malformed_range_unit_falls_back_to_full(make_client, fake_r2):
    """A Range header we don't understand (e.g. non-'bytes' unit) degrades to
    a full 200 response instead of erroring - same as most CDNs/servers."""
    client, app = make_client()
    submission_id, content = _ready_submission(app, fake_r2)
    resp = client.get(
        f"/api/v1/academy/submissions/{submission_id}/stream", headers={"Range": "items=0-1"},
    )
    assert resp.status_code == 200
    assert resp.content == content


# --- analyze-bpm -------------------------------------------------------------
#
# The real onset/tempo DSP is covered by test_bpm_analyzer_precision.py; here
# web_app.analyze_r2_object_bpm_async is monkeypatched (same pattern
# conftest.fake_media uses for analyze_bpm) so these tests are about routing,
# auth, status-gating and error-mapping, not the analyzer itself.


def _fake_analyzer(monkeypatch, *, result=None, error=None):
    async def fake(r2_key, *, suffix=".mp3", max_seconds=180.0):
        if error is not None:
            raise error
        return result

    monkeypatch.setattr(web_app, "analyze_r2_object_bpm_async", fake)


def test_analyze_bpm_requires_auth(make_client, fake_r2):
    client, app = make_client()
    submission_id, _ = _ready_submission(app, fake_r2)
    client.cookies.clear()
    resp = client.post(f"/api/v1/academy/submissions/{submission_id}/analyze-bpm")
    assert resp.status_code == 401


def test_analyze_bpm_unknown_submission_404(make_client, fake_r2):
    client, _ = make_client()
    resp = client.post("/api/v1/academy/submissions/nonexistent/analyze-bpm")
    assert resp.status_code == 404


def test_analyze_bpm_another_owners_submission_404(make_client, fake_r2):
    client, app = make_client()
    submission_id, _ = _ready_submission(app, fake_r2, user_id="someone-else")
    resp = client.post(f"/api/v1/academy/submissions/{submission_id}/analyze-bpm")
    assert resp.status_code == 404


def test_analyze_bpm_not_ready_returns_409(make_client, fake_r2):
    client, _ = make_client()
    presigned = _presign(client).json()
    resp = client.post(f"/api/v1/academy/submissions/{presigned['submission_id']}/analyze-bpm")
    assert resp.status_code == 409


def test_analyze_bpm_503_when_r2_not_configured(make_client, fake_r2, monkeypatch):
    client, app = make_client()
    submission_id, _ = _ready_submission(app, fake_r2)
    monkeypatch.setattr(r2_storage, "is_configured", lambda: False)
    resp = client.post(f"/api/v1/academy/submissions/{submission_id}/analyze-bpm")
    assert resp.status_code == 503


def test_analyze_bpm_success_updates_row(make_client, fake_r2, monkeypatch):
    client, app = make_client()
    submission_id, _ = _ready_submission(app, fake_r2)
    _fake_analyzer(
        monkeypatch,
        result={"bpm": 128.41, "bpm_confidence": 0.97, "bpm_source": "drops-local-rhythm-v1"},
    )
    resp = client.post(f"/api/v1/academy/submissions/{submission_id}/analyze-bpm")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["bpm"] == 128.41
    assert body["bpm_confidence"] == 0.97
    assert body["bpm_source"] == "drops-local-rhythm-v1"
    assert app.state.academy.get(submission_id)["bpm"] == 128.41


def test_analyze_bpm_analysis_failure_returns_422(make_client, fake_r2, monkeypatch):
    client, app = make_client()
    submission_id, _ = _ready_submission(app, fake_r2)
    _fake_analyzer(monkeypatch, error=BpmAnalysisError("Ritmo non rilevabile"))
    resp = client.post(f"/api/v1/academy/submissions/{submission_id}/analyze-bpm")
    assert resp.status_code == 422


def test_analyze_bpm_missing_object_returns_404(make_client, fake_r2, monkeypatch):
    client, app = make_client()
    submission_id, _ = _ready_submission(app, fake_r2)
    _fake_analyzer(monkeypatch, error=r2_storage.R2NotFoundError("object not found"))
    resp = client.post(f"/api/v1/academy/submissions/{submission_id}/analyze-bpm")
    assert resp.status_code == 404


def test_analyze_bpm_r2_error_returns_502(make_client, fake_r2, monkeypatch):
    client, app = make_client()
    submission_id, _ = _ready_submission(app, fake_r2)
    _fake_analyzer(monkeypatch, error=r2_storage.R2Error("network blip"))
    resp = client.post(f"/api/v1/academy/submissions/{submission_id}/analyze-bpm")
    assert resp.status_code == 502
