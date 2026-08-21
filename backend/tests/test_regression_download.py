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


def test_multisource_download_promoted_to_cloud(make_client, fake_r2):
    """With R2 configured, the classic flow ALSO uploads to R2 and writes a
    catalog row - while keeping the local file so /file and zip still work."""
    client, app = make_client()
    resp = client.post(
        "/api/v1/downloads",
        json={"url": "https://www.youtube.com/watch?v=abc", "quality": "320"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "ready"
    # Local file endpoint is kept (session downloads / zip unaffected)...
    assert body.get("file_url") == f"/api/v1/downloads/{body['id']}/file"
    # ...and a durable cloud copy + stream link now exist.
    assert body.get("track_id")
    assert body.get("stream_url") == f"/api/stream/{body['track_id']}"
    assert len(fake_r2["uploaded"]) == 1

    track = app.state.tracks.get_track(body["track_id"])
    assert track is not None and track["user_id"] == "dj"

    # Local file is still downloadable.
    assert client.get(body["file_url"]).status_code == 200

    # BPM lands in both the job and the catalog row.
    join_bpm_threads()
    assert app.state.tracks.get_track(body["track_id"])["bpm"] == 128.0


def test_multisource_survives_r2_upload_failure(make_client, fake_r2, monkeypatch):
    """If the R2 upload throws, the download must still succeed locally with no
    catalog row (best-effort promotion)."""
    import r2_storage

    def boom(*args, **kwargs):
        raise r2_storage.R2Error("simulated outage")

    monkeypatch.setattr(r2_storage, "upload_file", boom)

    client, app = make_client()
    resp = client.post(
        "/api/v1/downloads",
        json={"url": "https://www.youtube.com/watch?v=abc", "quality": "320"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "ready"
    assert body.get("file_url")
    assert "track_id" not in body
    assert app.state.tracks.list_tracks("dj") == []
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
