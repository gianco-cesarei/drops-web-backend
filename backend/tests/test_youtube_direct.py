"""Sezione 1 · Task 1.1 — /api/download/youtube-direct."""

from __future__ import annotations

from conftest import join_bpm_threads


def test_requires_auth(make_client):
    client, app = make_client()
    client.cookies.clear()
    resp = client.post("/api/download/youtube-direct", json={"url": "https://www.youtube.com/watch?v=abc"})
    assert resp.status_code == 401


def test_rejects_non_youtube_url(make_client):
    client, app = make_client()
    resp = client.post("/api/download/youtube-direct", json={"url": "https://soundcloud.com/x/y"})
    assert resp.status_code == 400
    assert "YouTube" in resp.json()["detail"]


def test_youtube_direct_uploads_to_cloud(make_client, fake_r2):
    client, app = make_client()
    resp = client.post(
        "/api/download/youtube-direct",
        json={"url": "https://www.youtube.com/watch?v=abc123", "genre": "Techno"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    task_id = body["task_id"]

    # Synchronous executor => already ready when the request returns.
    assert body["status"] == "ready"
    assert body["quality"] == "320"
    assert body["source"] == "youtube"
    assert "stream_url" in body and body["stream_url"] == f"/api/stream/{body['track_id']}"
    # Cloud flow does not expose a local file endpoint.
    assert "file_url" not in body

    # A durable catalog row was written and the object landed in R2.
    assert len(fake_r2["uploaded"]) == 1
    r2_key = fake_r2["uploaded"][0]
    assert r2_key.startswith("dj/Techno/")
    assert r2_key.endswith(".mp3")

    track = app.state.tracks.get_track(body["track_id"])
    assert track is not None
    assert track["user_id"] == "dj"
    assert track["genre"] == "Techno"
    assert track["r2_key"] == r2_key

    # Status endpoint returns the same terminal state.
    status = client.get(f"/api/download/youtube-direct/{task_id}")
    assert status.status_code == 200
    assert status.json()["status"] == "ready"

    # BPM is computed off the critical path and persisted to the catalog DB.
    join_bpm_threads()
    assert app.state.tracks.get_track(body["track_id"])["bpm"] == 128.0


def test_youtube_direct_local_when_r2_disabled(make_client):
    """Without R2 configured the endpoint still works, keeping the file local."""
    client, app = make_client()
    resp = client.post(
        "/api/download/youtube-direct",
        json={"url": "https://youtu.be/xyz"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "ready"
    # No cloud promotion: local file endpoint, no track/stream.
    assert body.get("file_url")
    assert "track_id" not in body
    assert "stream_url" not in body
    join_bpm_threads()


def test_status_unknown_task_404(make_client):
    client, app = make_client()
    resp = client.get("/api/download/youtube-direct/does-not-exist")
    assert resp.status_code == 404
