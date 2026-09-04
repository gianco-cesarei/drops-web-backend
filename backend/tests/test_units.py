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


def test_explicit_youtube_failure_without_strict_match_never_falls_back_blindly(monkeypatch, tmp_path: Path):
    requested_url = "https://www.youtube.com/watch?v=JbySohLL3io"
    calls = []

    def failing_attempt(job_dir, url, quality, settings, started, *, proxy=None):
        calls.append((url, proxy))
        raise RuntimeError("youtube verification required")

    monkeypatch.setattr(download_engine, "attempt_download", failing_attempt)
    monkeypatch.setattr(download_engine, "find_soundcloud_match", lambda *args, **kwargs: None)

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

    assert calls == [(requested_url, "http://proxy.invalid:8080"), ("scsearch5:Miles Mercer Voice Control", None)]


def test_explicit_youtube_failure_uses_only_validated_direct_fallback(monkeypatch, tmp_path: Path):
    requested_url = "https://www.youtube.com/watch?v=JbySohLL3io"
    fallback_url = "https://soundcloud.com/miles-mercer/voice-control"
    calls = []
    searches = []

    def fake_attempt(job_dir, url, quality, settings, started, *, proxy=None):
        calls.append((url, proxy))
        if url == requested_url:
            raise RuntimeError("youtube verification required")
        assert url == fallback_url
        return {"title": "Miles Mercer - Voice Control", "duration": 200}

    def fake_match(*args, **kwargs):
        searches.append(kwargs)
        return fallback_url

    monkeypatch.setattr(download_engine, "attempt_download", fake_attempt)
    monkeypatch.setattr(download_engine, "find_soundcloud_match", fake_match)

    _, source = download_engine.download_multi_source(
        tmp_path,
        "job-fallback",
        requested_url,
        "Miles Mercer",
        "Voice Control",
        200,
        "320",
        SimpleNamespace(),
        0.0,
        proxy="http://proxy.invalid:8080",
    )

    assert source == "soundcloud"
    assert searches == [{"raw_title": None, "catalog_no": None, "strict": True, "label": None}]
    assert calls == [
        (requested_url, "http://proxy.invalid:8080"),
        (fallback_url, None),
    ]


def test_strict_candidate_rejects_wrong_artist_even_when_title_and_duration_match():
    accepted, _, reason = download_engine.strict_candidate_match(
        "PR 300608",
        "Days",
        180,
        {"title": "Days", "uploader": "Martin Garrix", "duration": 180},
    )

    assert accepted is False
    assert reason == "artist_mismatch"


def test_strict_candidate_accepts_matching_artist_title_and_duration():
    accepted, score, reason = download_engine.strict_candidate_match(
        "Miles Mercer",
        "Voice Control",
        200,
        {
            "title": "Miles Mercer - Voice Control [FOR07]",
            "uploader": "Foresight Records",
            "duration": 204,
        },
    )

    assert accepted is True
    assert score >= 0.75
    assert reason == "accepted"


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


def test_extract_version_info():
    v1 = download_engine.extract_version_info("Dennis Ferrer - Hey Hey (Manoo Remix)")
    assert v1["remixer"] == "manoo"
    assert v1["version_type"] == "remix"

    v2 = download_engine.extract_version_info("Voice Control (Tale Of Us Edit)")
    assert v2["remixer"] == "tale of us"
    assert v2["version_type"] == "edit"

    v3 = download_engine.extract_version_info("Track (Henrik Schwarz Remix)")
    assert v3["remixer"] == "henrik schwarz"
    assert v3["version_type"] == "remix"

    v4 = download_engine.extract_version_info("Track (Extended Mix)")
    assert v4["remixer"] is None
    assert v4["version_type"] == "extended mix"


def test_strict_candidate_remix_mismatch():
    accepted, score, reason = download_engine.strict_candidate_match(
        "Dennis Ferrer",
        "Hey Hey (Manoo Remix)",
        300,
        {
            "title": "Dennis Ferrer - Hey Hey (Tale Of Us Remix)",
            "uploader": "Afterlife",
            "duration": 300,
        },
    )
    assert accepted is False
    assert reason == "remix_mismatch"


def test_strict_candidate_remix_match_accepts():
    accepted, score, reason = download_engine.strict_candidate_match(
        "Dennis Ferrer",
        "Hey Hey (Manoo Remix)",
        300,
        {
            "title": "Dennis Ferrer - Hey Hey (Manoo Remix) [OBJ001]",
            "uploader": "Objectivity",
            "duration": 302,
        },
        catalog_no="OBJ001",
        label="Objectivity",
    )
    assert accepted is True
    assert reason == "accepted"
    assert score >= 0.85


def test_build_search_queries_preserves_remix_and_label():
    queries = download_engine.build_search_queries(
        artist="Dennis Ferrer",
        title="Hey Hey (Manoo Remix)",
        catalog_no="OBJ001",
        label="Objectivity",
    )
    assert any("Manoo Remix" in q for q in queries)
    assert any("OBJ001" in q for q in queries)
    assert any("Objectivity" in q for q in queries)


def test_youtube_failure_falls_back_to_nonstrict_soundcloud(monkeypatch, tmp_path: Path):
    requested_url = "https://www.youtube.com/watch?v=JbySohLL3io"
    sc_fallback_url = "https://soundcloud.com/artist/track"
    calls = []

    def fake_attempt(job_dir, url, quality, settings, started, *, proxy=None):
        calls.append(url)
        if url == requested_url:
            raise RuntimeError("Sign in to confirm you’re not a bot")
        assert url == sc_fallback_url
        return {"title": "Artist - Track", "duration": 180}

    def fake_match(*args, **kwargs):
        if kwargs.get("strict"):
            return None
        return sc_fallback_url

    monkeypatch.setattr(download_engine, "attempt_download", fake_attempt)
    monkeypatch.setattr(download_engine, "find_soundcloud_match", fake_match)

    job_dir = tmp_path / "job"
    job_dir.mkdir()

    info, source = download_engine.download_multi_source(
        job_dir=job_dir,
        job_id="test-fallback",
        native_url=requested_url,
        artist="Artist",
        title="Track",
        duration=180,
        quality="320",
        settings=SimpleNamespace(max_duration_seconds=900, max_file_bytes=100_000_000),
        started=0.0,
    )

    assert source == "soundcloud"
    assert info["title"] == "Artist - Track"
    assert calls == [requested_url, sc_fallback_url]


