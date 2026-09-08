"""Cross-origin, range-aware /file serving + signed file-url tokens.

The Archivio front runs each track through a Web Audio graph (EQ + master
volume) and can fetch every track to build a .zip. That needs the /file
endpoint to be reachable cross-origin without the session cookie (via a
short-lived signed token) and to honour Range requests. These tests lock in
that contract.
"""

from __future__ import annotations

from conftest import join_bpm_threads


def _ready_download(client) -> dict:
    resp = client.post(
        "/api/v1/downloads",
        json={"url": "https://www.youtube.com/watch?v=abc", "quality": "320"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "ready"
    join_bpm_threads()
    return body


# --- file-url (token minting) ------------------------------------------------

def test_file_url_requires_auth(make_client):
    client, _ = make_client()
    body = _ready_download(client)
    client.cookies.clear()
    resp = client.get(f"/api/v1/downloads/{body['id']}/file-url")
    assert resp.status_code == 401


def test_file_url_returns_signed_url(make_client):
    client, _ = make_client()
    body = _ready_download(client)
    resp = client.get(f"/api/v1/downloads/{body['id']}/file-url")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["token"]
    assert data["expires_in"] == 300
    assert data["url"].endswith(f"/api/v1/downloads/{body['id']}/file?token={data['token']}")


def test_file_url_unknown_job_404(make_client):
    client, _ = make_client()
    resp = client.get("/api/v1/downloads/does-not-exist/file-url")
    assert resp.status_code == 404


# --- token-authenticated (cookie-less) access --------------------------------

def test_token_grants_cookieless_access(make_client):
    client, _ = make_client()
    body = _ready_download(client)
    token = client.get(f"/api/v1/downloads/{body['id']}/file-url").json()["token"]
    # Drop the session cookie entirely: this is the crossOrigin="anonymous" case.
    client.cookies.clear()
    resp = client.get(f"/api/v1/downloads/{body['id']}/file?token={token}")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "audio/mpeg"
    assert resp.headers["accept-ranges"] == "bytes"
    assert "cache-control" in resp.headers
    assert resp.content == b"ID3fake-audio-bytes"


def test_missing_credentials_rejected(make_client):
    client, _ = make_client()
    body = _ready_download(client)
    client.cookies.clear()
    resp = client.get(f"/api/v1/downloads/{body['id']}/file")
    assert resp.status_code == 401


def test_invalid_token_rejected(make_client):
    client, _ = make_client()
    body = _ready_download(client)
    client.cookies.clear()
    resp = client.get(f"/api/v1/downloads/{body['id']}/file?token=not-a-real-token")
    assert resp.status_code == 401


def test_token_for_other_file_rejected(make_client):
    client, _ = make_client()
    body = _ready_download(client)
    token = client.get(f"/api/v1/downloads/{body['id']}/file-url").json()["token"]
    client.cookies.clear()
    # Same valid token, different job_id in the path -> must not unlock it.
    resp = client.get(f"/api/v1/downloads/some-other-job/file?token={token}")
    assert resp.status_code == 403


# --- Range / seek ------------------------------------------------------------

def test_range_returns_206_partial(make_client):
    client, _ = make_client()
    body = _ready_download(client)
    full = b"ID3fake-audio-bytes"
    resp = client.get(
        f"/api/v1/downloads/{body['id']}/file",
        headers={"Range": "bytes=0-3"},
    )
    assert resp.status_code == 206, resp.text
    assert resp.headers["content-range"] == f"bytes 0-3/{len(full)}"
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.headers["content-length"] == "4"
    assert resp.content == full[0:4]


def test_range_suffix(make_client):
    client, _ = make_client()
    body = _ready_download(client)
    full = b"ID3fake-audio-bytes"
    resp = client.get(
        f"/api/v1/downloads/{body['id']}/file",
        headers={"Range": "bytes=-5"},
    )
    assert resp.status_code == 206, resp.text
    assert resp.content == full[-5:]
    assert resp.headers["content-range"] == f"bytes {len(full) - 5}-{len(full) - 1}/{len(full)}"


def test_unsatisfiable_range_416(make_client):
    client, _ = make_client()
    body = _ready_download(client)
    full = b"ID3fake-audio-bytes"
    resp = client.get(
        f"/api/v1/downloads/{body['id']}/file",
        headers={"Range": f"bytes={len(full) + 10}-{len(full) + 20}"},
    )
    assert resp.status_code == 416
    assert resp.headers["content-range"] == f"bytes */{len(full)}"


def test_head_returns_headers_no_body(make_client):
    client, _ = make_client()
    body = _ready_download(client)
    resp = client.head(f"/api/v1/downloads/{body['id']}/file")
    assert resp.status_code == 200
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.headers["content-length"] == str(len(b"ID3fake-audio-bytes"))
    assert resp.content == b""


# --- R2 fallback still works cross-origin ------------------------------------

def test_r2_fallback_serves_with_range(make_client, fake_r2):
    """When the local file is gone but an R2 copy exists, /file streams from R2
    with Accept-Ranges and honours Range (seek works cross-origin)."""
    import os

    client, app = make_client()
    body = _ready_download(client)
    assert body.get("r2_key") or app.state.store.get_job_by_id(body["id"])["r2_key"]
    # Simulate the local TTL cleanup: remove the on-disk file, keep the R2 copy.
    row = app.state.store.get_job_by_id(body["id"])
    os.remove(row["file_path"])

    token = client.get(f"/api/v1/downloads/{body['id']}/file-url").json()["token"]
    client.cookies.clear()

    resp = client.get(f"/api/v1/downloads/{body['id']}/file?token={token}")
    assert resp.status_code == 200, resp.text
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.content == b"fake-audio-bytes"

    part = client.get(
        f"/api/v1/downloads/{body['id']}/file?token={token}",
        headers={"Range": "bytes=0-3"},
    )
    assert part.status_code == 206, part.text
    assert part.content == b"fake"
    assert part.headers["content-range"] == "bytes 0-3/16"


# --- Wildcard CORS (route 1: signed URL, Access-Control-Allow-Origin: *) ------

def test_wildcard_cors_on_token_get(make_client):
    client, _ = make_client()
    body = _ready_download(client)
    token = client.get(f"/api/v1/downloads/{body['id']}/file-url").json()["token"]
    client.cookies.clear()
    resp = client.get(
        f"/api/v1/downloads/{body['id']}/file?token={token}",
        headers={"Origin": "https://some-front.example"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["access-control-allow-origin"] == "*"
    # Wildcard must not be paired with credentials.
    assert "access-control-allow-credentials" not in resp.headers
    assert "Accept-Ranges" in resp.headers["access-control-expose-headers"]


def test_wildcard_cors_on_206(make_client):
    client, _ = make_client()
    body = _ready_download(client)
    resp = client.get(
        f"/api/v1/downloads/{body['id']}/file",
        headers={"Origin": "https://some-front.example", "Range": "bytes=0-3"},
    )
    assert resp.status_code == 206
    assert resp.headers["access-control-allow-origin"] == "*"


def test_preflight_options_wildcard(make_client):
    client, _ = make_client()
    body = _ready_download(client)
    resp = client.options(
        f"/api/v1/downloads/{body['id']}/file",
        headers={
            "Origin": "https://some-front.example",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "range",
        },
    )
    assert resp.status_code in (200, 204), resp.text
    assert resp.headers["access-control-allow-origin"] == "*"
    assert "GET" in resp.headers["access-control-allow-methods"]
    assert "Range" in resp.headers["access-control-allow-headers"]


def test_job_lookup_tracks_fallback(make_client):
    """When store.get_job returns None (e.g. SQLite wiped), check tracks catalog."""
    client, app = make_client()
    
    # 1. Create a track in tracks store only (NOT in SQLite jobs store)
    app.state.tracks.create_track(
        track_id="test-fallback-job-123",
        user_id="dj",
        r2_key="dj/House/Test Artist - Test Track.mp3",
        artist="Test Artist",
        title="Test Track",
        genre="House",
        bpm=124.0,
    )
    
    assert app.state.store.get_job("test-fallback-job-123", "dj") is None
    
    # 2. Test GET /api/v1/downloads/{job_id}
    resp = client.get("/api/v1/downloads/test-fallback-job-123")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == "test-fallback-job-123"
    assert body["status"] == "ready"
    assert body["artist"] == "Test Artist"
    assert body["title"] == "Test Track"
    assert body["r2_key"] == "dj/House/Test Artist - Test Track.mp3"
    assert body["bpm"] == 124.0

    # 3. Test GET /api/v1/downloads/{job_id}/file-url
    url_resp = client.get("/api/v1/downloads/test-fallback-job-123/file-url")
    assert url_resp.status_code == 200, url_resp.text
    token = url_resp.json()["token"]
    assert token

    # 4. Test GET /api/v1/downloads/{job_id}/file with token (cookieless)
    client.cookies.clear()
    file_resp = client.get(f"/api/v1/downloads/test-fallback-job-123/file?token={token}")
    # Without mock R2 file uploaded, fallback to _serve_r2_file will raise 404 because file isn't in R2,
    # but the job lookup itself succeeded (did not fail at 404 Download not found).
    assert file_resp.status_code == 404
    assert file_resp.json()["detail"] == "File temporaneo scaduto sul server. Rilancia il download dalla sorgente."

    # 5. Ownership isolation: another user cannot access this track
    # Re-authenticate as "dj" (since cookies were cleared in step 4)
    login_resp = client.post("/api/v1/auth/login", json={"username": "dj", "password": "correct horse battery staple"})
    assert login_resp.status_code == 200

    app.state.tracks.create_track(
        track_id="other-user-track-999",
        user_id="other_user",
        r2_key="other/Track.mp3",
    )
    # Logged in as "dj", trying to access "other_user"'s track
    resp_other = client.get("/api/v1/downloads/other-user-track-999")
    assert resp_other.status_code == 404

