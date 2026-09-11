import threading
import time
from pathlib import Path
from unittest.mock import patch

from media_core import FFMPEG_SEMAPHORE


def test_ffmpeg_semaphore_concurrency():
    """Verify FFMPEG_SEMAPHORE restricts concurrent execution to at most 2."""
    max_observed = 0
    current_active = 0
    lock = threading.Lock()

    def worker():
        nonlocal max_observed, current_active
        with FFMPEG_SEMAPHORE:
            with lock:
                current_active += 1
                if current_active > max_observed:
                    max_observed = current_active
            time.sleep(0.05)
            with lock:
                current_active -= 1

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert max_observed <= 2, f"FFMPEG_SEMAPHORE allowed {max_observed} > 2 concurrent executions"


def test_instant_duplicate_skip_postgres(make_client):
    """Verify that a track already present in TrackStore is skipped instantly as 'ready'."""
    client, app = make_client()
    tracks = app.state.tracks
    owner = "dj"

    # Pre-seed track in Postgres/SQLite tracks table
    seeded = tracks.create_track(
        user_id=owner,
        r2_key=f"{owner}/Deep House/Kerri Chandler - Atmospheric.mp3",
        artist="Kerri Chandler",
        title="Atmospheric",
        genre="Deep House",
        bpm=124.0,
    )
    assert seeded["track_id"]

    # Submit download for the exact same artist and title
    res = client.post(
        "/api/v1/downloads",
        json={
            "url": "https://www.youtube.com/watch?v=Atmospheric01",
            "artist": "Kerri Chandler",
            "title": "Atmospheric",
            "quality": "320",
        },
    )
    assert res.status_code == 202
    data = res.json()
    assert data["status"] == "ready"
    assert data["artist"] == "Kerri Chandler"
    assert data["title"] == "Atmospheric"
    assert data["r2_key"] == f"{owner}/Deep House/Kerri Chandler - Atmospheric.mp3"
    assert data["bpm"] == 124.0


def test_batch_downloads_endpoint(make_client):
    """Verify batch submission of multiple tracks returns all jobs with instant duplicate handling."""
    client, app = make_client()
    tracks = app.state.tracks
    owner = "dj"

    # Pre-seed one duplicate
    tracks.create_track(
        user_id=owner,
        r2_key=f"{owner}/House/Track 1.mp3",
        artist="DJ Test",
        title="Track 1",
        bpm=126.0,
    )

    mock_oembed = {
        "https://www.youtube.com/watch?v=mock1": {"title": "Track 1", "artist": "DJ Test", "cover_url": "https://img/1.jpg"},
        "https://www.youtube.com/watch?v=mock2": {"title": "Track 2", "artist": "DJ Other", "cover_url": "https://img/2.jpg"},
    }

    with patch("web_app.resolve_track_oembed", side_effect=lambda u: mock_oembed.get(u, {"title": "Unknown"})):
        res = client.post(
            "/api/v1/downloads/batch",
            json={
                "urls": [
                    "https://www.youtube.com/watch?v=mock1",
                    "https://www.youtube.com/watch?v=mock2",
                ],
                "quality": "320",
            },
        )
        assert res.status_code == 202
        payload = res.json()
        assert "downloads" in payload
        assert len(payload["downloads"]) == 2

        # The first should be recognized as duplicate -> status: ready
        job1 = payload["downloads"][0]
        assert job1["status"] == "ready"
        assert job1["title"] == "Track 1"
        assert job1["artist"] == "DJ Test"

        # The second should be newly queued
        job2 = payload["downloads"][1]
        assert job2["status"] in ("recognized", "downloading", "enriching", "ready")
        assert job2["title"] == "Track 2"
        assert job2["artist"] == "DJ Other"
        assert job2["cover_url"] == "https://img/2.jpg"
