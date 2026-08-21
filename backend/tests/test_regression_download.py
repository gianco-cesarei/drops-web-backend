"""Regression guard — the existing single-download flow (/api/v1/downloads)
must keep working unchanged after the cloud-storage additions (Task 3 verify)."""

from __future__ import annotations

from conftest import join_bpm_threads


def test_health_ok(make_client):
    client, app = make_client()
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_existing_download_requires_auth(make_client):
    client, app = make_client()
    client.cookies.clear()
    resp = client.post("/api/v1/downloads", json={"url": "https://www.youtube.com/watch?v=abc"})
    assert resp.status_code == 401


def test_existing_single_download_still_local(make_client):
    """The classic flow must still produce a locally-served file and must NOT
    be diverted into the cloud/catalog path."""
    client, app = make_client()
    resp = client.post(
        "/api/v1/downloads",
        json={"url": "https://www.youtube.com/watch?v=abc", "quality": "320"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "ready"
    assert body.get("file_url") == f"/api/v1/downloads/{body['id']}/file"
    # No cloud side effects on the classic endpoint.
    assert "track_id" not in body
    assert "stream_url" not in body
    assert app.state.tracks.list_tracks("dj") == []

    # The file is actually downloadable.
    file_resp = client.get(body["file_url"])
    assert file_resp.status_code == 200
    assert file_resp.headers["content-type"] == "audio/mpeg"
    join_bpm_threads()


def test_existing_download_rejects_unsupported_url(make_client):
    client, app = make_client()
    resp = client.post("/api/v1/downloads", json={"url": "https://example.com/foo"})
    assert resp.status_code == 400


def test_existing_download_rejects_bad_quality(make_client):
    client, app = make_client()
    resp = client.post(
        "/api/v1/downloads",
        json={"url": "https://www.youtube.com/watch?v=abc", "quality": "999"},
    )
    assert resp.status_code == 400
