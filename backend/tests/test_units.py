"""Unit tests for the pure pieces: R2 key building and the catalog store."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import download_engine
import r2_storage
import track_store


def test_build_object_key_layout():
    key = r2_storage.build_object_key("dj", "Techno", "Artist", "Title")
    assert key == "dj/Techno/Artist - Title.mp3"


def test_build_object_key_keeps_accents_drops_unsafe():
    key = r2_storage.build_object_key("dj", "Minimal / Deep", "Rødhåd", "Track: Name/Weird ⚡")
    # slash -> dash, accents kept, control/emoji/colon stripped.
    assert key == "dj/Minimal - Deep/Rødhåd - Track Name-Weird.mp3"


def test_build_object_key_placeholders_when_missing():
    key = r2_storage.build_object_key("dj", None, None, None)
    assert key == "dj/Unknown/Unknown Artist - Unknown Title.mp3"


def test_is_configured_false_without_env(monkeypatch):
    for name in ("DROPS_R2_ACCOUNT_ID", "DROPS_R2_ACCESS_KEY_ID", "DROPS_R2_SECRET_ACCESS_KEY", "DROPS_R2_BUCKET", "DROPS_R2_ENDPOINT_URL"):
        monkeypatch.delenv(name, raising=False)
    assert r2_storage.is_configured() is False


def test_is_configured_true_with_env(monkeypatch):
    monkeypatch.setenv("DROPS_R2_ACCOUNT_ID", "acct")
    monkeypatch.setenv("DROPS_R2_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("DROPS_R2_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("DROPS_R2_BUCKET", "bucket")
    assert r2_storage.is_configured() is True


def test_track_store_crud(tmp_path: Path):
    store = track_store.TrackStore(tmp_path / "state")
    created = store.create_track(user_id="dj", r2_key="dj/Techno/A - B.mp3", artist="A", title="B", genre="Techno")
    tid = created["track_id"]
    assert store.get_track(tid)["title"] == "B"
    assert store.update_bpm(tid, 128.0) is True
    assert store.get_track(tid)["bpm"] == 128.0
    assert [t["track_id"] for t in store.list_tracks("dj")] == [tid]
    assert store.list_tracks("someone-else") == []
    # ownership-scoped delete
    assert store.delete_track(tid, user_id="wrong") is None
    assert store.delete_track(tid, user_id="dj") == "dj/Techno/A - B.mp3"
    assert store.get_track(tid) is None


def test_track_store_normalizes_postgres_url(tmp_path: Path):
    assert track_store.resolve_database_url(tmp_path, "postgres://u:p@h:5432/db").startswith("postgresql+psycopg2://")
    assert track_store.resolve_database_url(tmp_path, "postgresql://u:p@h:5432/db").startswith("postgresql+psycopg2://")
    assert track_store.resolve_database_url(tmp_path, "").startswith("sqlite:///")


def test_explicit_youtube_url_never_substitutes_soundcloud(monkeypatch, tmp_path: Path):
    requested_url = "https://www.youtube.com/watch?v=JbySohLL3io"
    calls = []

    def fake_attempt(job_dir, url, quality, settings, started, *, proxy=None):
        calls.append((url, proxy))
        return {"title": "Miles Mercer - Voice Control [FOR07]", "duration": 200}

    def unexpected_soundcloud_search(*args, **kwargs):
        raise AssertionError("explicit YouTube URL must not search SoundCloud")

    monkeypatch.setattr(download_engine, "attempt_download", fake_attempt)
    monkeypatch.setattr(download_engine, "find_soundcloud_match", unexpected_soundcloud_search)

    info, source = download_engine.download_multi_source(
        tmp_path,
        "job-1",
        requested_url,
        "Miles Mercer",
        "Voice Control",
        None,
        "320",
        SimpleNamespace(),
        0.0,
        proxy="http://proxy.invalid:8080",
        raw_title="Miles Mercer - Voice Control [FOR07]",
    )

    assert source == "youtube"
    assert info["title"] == "Miles Mercer - Voice Control [FOR07]"
    assert calls == [(requested_url, "http://proxy.invalid:8080")]


def test_explicit_youtube_failure_never_falls_back_to_search(monkeypatch, tmp_path: Path):
    requested_url = "https://www.youtube.com/watch?v=JbySohLL3io"
    calls = []

    def failing_attempt(job_dir, url, quality, settings, started, *, proxy=None):
        calls.append((url, proxy))
        raise RuntimeError("youtube verification required")

    def unexpected_soundcloud_search(*args, **kwargs):
        raise AssertionError("explicit YouTube URL must not search SoundCloud")

    monkeypatch.setattr(download_engine, "attempt_download", failing_attempt)
    monkeypatch.setattr(download_engine, "find_soundcloud_match", unexpected_soundcloud_search)

    with pytest.raises(RuntimeError, match="youtube verification required"):
        download_engine.download_multi_source(
            tmp_path,
            "job-2",
            requested_url,
            "Miles Mercer",
            "Voice Control",
            None,
            "320",
            SimpleNamespace(),
            0.0,
            proxy="http://proxy.invalid:8080",
        )

    assert calls == [(requested_url, "http://proxy.invalid:8080")]


def test_youtube_search_page_still_uses_metadata_search(monkeypatch, tmp_path: Path):
    calls = []

    def fake_attempt(job_dir, url, quality, settings, started, *, proxy=None):
        calls.append((url, proxy))
        return {"title": "Artist - Track", "duration": 180}

    monkeypatch.setattr(download_engine, "attempt_download", fake_attempt)
    monkeypatch.setattr(download_engine, "find_soundcloud_match", lambda *args, **kwargs: None)

    _, source = download_engine.download_multi_source(
        tmp_path,
        "job-search",
        "https://www.youtube.com/results?search_query=Artist+Track",
        "Artist",
        "Track",
        None,
        "320",
        SimpleNamespace(),
        0.0,
        proxy="http://proxy.invalid:8080",
    )

    assert source == "youtube"
    assert calls == [("ytsearch5:Artist Track", "http://proxy.invalid:8080")]
