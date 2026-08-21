"""Sezione 3 · Task 3.2 — /api/stream/{track_id} presigned URLs + ownership."""

from __future__ import annotations


def test_requires_auth(make_client, fake_r2):
    client, app = make_client()
    track = app.state.tracks.create_track(user_id="dj", r2_key="dj/Techno/A - B.mp3", artist="A", title="B", genre="Techno")
    client.cookies.clear()
    resp = client.get(f"/api/stream/{track['track_id']}")
    assert resp.status_code == 401


def test_stream_returns_presigned_url(make_client, fake_r2):
    client, app = make_client()
    track = app.state.tracks.create_track(
        user_id="dj", r2_key="dj/Techno/A - B.mp3", artist="A", title="B", genre="Techno", bpm=126.0,
    )
    resp = client.get(f"/api/stream/{track['track_id']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["track_id"] == track["track_id"]
    assert body["url"].startswith("https://r2.example/dj/Techno/A - B.mp3")
    assert body["expires_in"] == 3600
    assert body["bpm"] == 126.0


def test_unknown_track_404(make_client, fake_r2):
    client, app = make_client()
    resp = client.get("/api/stream/nonexistent-id")
    assert resp.status_code == 404


def test_cannot_stream_another_users_track(make_client, fake_r2):
    """Ownership is enforced: a track owned by someone else is 404, never a
    usable presigned URL."""
    client, app = make_client()
    other = app.state.tracks.create_track(
        user_id="another-user", r2_key="another-user/Techno/X - Y.mp3", artist="X", title="Y", genre="Techno",
    )
    resp = client.get(f"/api/stream/{other['track_id']}")
    assert resp.status_code == 404


def test_stream_503_when_r2_not_configured(make_client):
    """Track exists and is owned, but cloud storage isn't configured."""
    client, app = make_client()
    track = app.state.tracks.create_track(user_id="dj", r2_key="dj/Techno/A - B.mp3", artist="A", title="B", genre="Techno")
    resp = client.get(f"/api/stream/{track['track_id']}")
    assert resp.status_code == 503


def test_list_tracks_only_own(make_client, fake_r2):
    client, app = make_client()
    app.state.tracks.create_track(user_id="dj", r2_key="dj/Techno/A - B.mp3", artist="A", title="B", genre="Techno")
    app.state.tracks.create_track(user_id="other", r2_key="other/House/C - D.mp3", artist="C", title="D", genre="House")
    resp = client.get("/api/tracks")
    assert resp.status_code == 200
    tracks = resp.json()["tracks"]
    assert len(tracks) == 1
    assert tracks[0]["user_id"] == "dj"
