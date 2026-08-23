from __future__ import annotations

import pytest

import web_app


class FakeYoutubeDL:
    payload = {}

    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def extract_info(self, url, download=False):
        return self.payload


@pytest.fixture
def playlist_client(make_client, monkeypatch):
    FakeYoutubeDL.payload = {
        "title": "FOR Playlist",
        "entries": [
            {
                "id": "anotherTrack",
                "title": "Another Track",
                "extractor_key": "Youtube",
                "uploader": "Label",
                "duration": 180,
            },
            {
                "id": "JbySohLL3io",
                "title": "Miles Mercer - Voice Control [FOR07]",
                "extractor_key": "Youtube",
                "uploader": "Label",
                "duration": 200,
            },
        ],
    }
    monkeypatch.setattr(web_app.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    return make_client()[0]


def test_resolve_marks_selected_track_inside_playlist(playlist_client):
    response = playlist_client.post(
        "/api/v1/playlists/resolve",
        json={"url": "https://www.youtube.com/watch?v=JbySohLL3io&list=PL123"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["url_type"] == "track_in_playlist"
    assert body["playlist_id"] == "PL123"
    assert body["selected_track_id"] == "JbySohLL3io"
    assert body["selected_track_url"] == "https://www.youtube.com/watch?v=JbySohLL3io"
    assert body["selected_track"]["title"] == "Miles Mercer - Voice Control [FOR07]"
    assert body["count"] == 2


def test_resolve_marks_playlist_without_selected_track(playlist_client):
    response = playlist_client.post(
        "/api/v1/playlists/resolve",
        json={"url": "https://www.youtube.com/playlist?list=PL123"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["url_type"] == "playlist"
    assert body["playlist_id"] == "PL123"
    assert body["selected_track_id"] is None
    assert body["selected_track"] is None


def test_youtu_be_track_in_playlist_is_detected():
    context = web_app._youtube_url_context("https://youtu.be/JbySohLL3io?list=PL123")

    assert context == {
        "url_type": "track_in_playlist",
        "playlist_id": "PL123",
        "selected_track_id": "JbySohLL3io",
        "selected_track_url": "https://www.youtube.com/watch?v=JbySohLL3io",
    }
