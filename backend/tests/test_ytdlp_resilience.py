from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import yt_dlp

import download_engine
import media_core


def test_missing_cookie_path_is_not_written_as_cookie_content(monkeypatch, tmp_path: Path):
    missing = tmp_path / "cookies.txt"
    monkeypatch.setenv("DROPS_YTDLP_COOKIES", str(missing))
    monkeypatch.setattr(media_core.Path, "is_dir", lambda _self: False)

    assert media_core.ytdlp_cookiefile() is None


def test_raw_netscape_cookies_get_private_permissions(monkeypatch, tmp_path: Path):
    raw = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\tvalue"
    monkeypatch.setenv("DROPS_YTDLP_COOKIES", raw)
    monkeypatch.setattr(media_core.tempfile, "gettempdir", lambda: str(tmp_path))

    result = Path(media_core.ytdlp_cookiefile() or "")

    assert result.read_text() == raw
    assert result.stat().st_mode & 0o777 == 0o600


def test_malformed_cookie_file_is_rejected(monkeypatch, tmp_path: Path):
    malformed = tmp_path / "cookies.txt"
    malformed.write_text("<html>not cookies</html>")
    monkeypatch.setenv("DROPS_YTDLP_COOKIES", str(malformed))
    monkeypatch.setattr(media_core.Path, "is_dir", lambda _self: False)

    assert media_core.ytdlp_cookiefile() is None


def test_bot_check_has_clean_user_message():
    error = yt_dlp.utils.DownloadError(
        "ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies-from-browser"
    )

    message = media_core.public_ytdlp_error(error)

    assert "verifica temporanea" in message
    assert "--cookies" not in message


def test_proxy_survives_bot_check_retry(monkeypatch, tmp_path: Path):
    options_seen = []

    class FakeYoutubeDL:
        calls = 0

        def __init__(self, options):
            options_seen.append(dict(options))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download):
            assert download is True
            self.__class__.calls += 1
            if self.__class__.calls == 1:
                raise yt_dlp.utils.DownloadError("Sign in to confirm you’re not a bot")
            return {"title": "Track", "duration": 120}

    monkeypatch.setattr(download_engine.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr(download_engine.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(download_engine, "ytdlp_cookiefile", lambda: None)
    monkeypatch.setattr(download_engine, "ytdlp_extractor_args", lambda: {"youtube": {}})
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    settings = SimpleNamespace(max_duration_seconds=900, max_file_bytes=100_000_000)

    result = download_engine.attempt_download(
        job_dir, "https://youtube.com/watch?v=abc", "320", settings, 0.0,
        proxy="http://proxy.example:8080",
    )

    assert result["title"] == "Track"
    assert options_seen[0]["proxy"] == "http://proxy.example:8080"
    assert options_seen[1]["proxy"] == "http://proxy.example:8080"


def test_proxy_is_dropped_after_proxy_auth_failure(monkeypatch, tmp_path: Path):
    options_seen = []

    class FakeYoutubeDL:
        calls = 0

        def __init__(self, options):
            options_seen.append(dict(options))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download):
            self.__class__.calls += 1
            if self.__class__.calls == 1:
                raise yt_dlp.utils.DownloadError("407 Proxy Authentication Required")
            return {"title": "Track", "duration": 120}

    monkeypatch.setattr(download_engine.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr(download_engine.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(download_engine, "ytdlp_cookiefile", lambda: None)
    monkeypatch.setattr(download_engine, "ytdlp_extractor_args", lambda: {"youtube": {}})
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    settings = SimpleNamespace(max_duration_seconds=900, max_file_bytes=100_000_000)

    download_engine.attempt_download(
        job_dir, "https://youtube.com/watch?v=abc", "320", settings, 0.0,
        proxy="http://proxy.example:8080",
    )

    assert "proxy" in options_seen[0]
    assert "proxy" not in options_seen[1]


def test_build_search_queries():
    queries = download_engine.build_search_queries(
        artist="Artist - Topic",
        title="Track Title (Official Video) [HQ]",
        raw_title="01. Artist - Track Title (Official Video)",
        catalog_no="REC123",
    )
    assert "Artist Track Title" in queries
    assert "Artist Track Title audio" in queries
    assert "REC123 Track Title" in queries


def test_download_multi_source_search_url_cascade(monkeypatch, tmp_path: Path):
    urls_downloaded = []

    def fake_attempt_download(job_dir, url, quality, settings, started, proxy=None):
        urls_downloaded.append(url)
        if "ytsearch5:Artist Track" in url:
            return {"title": "Artist - Track", "duration": 180}
        raise RuntimeError("Download failed for candidate")

    monkeypatch.setattr(download_engine, "attempt_download", fake_attempt_download)
    monkeypatch.setattr(download_engine, "find_soundcloud_match", lambda *args, **kwargs: None)

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    settings = SimpleNamespace(max_duration_seconds=900, max_file_bytes=100_000_000)

    info, source = download_engine.download_multi_source(
        job_dir=job_dir,
        job_id="test-job-1",
        native_url="https://soundcloud.com/search?q=Artist%20Track",
        artist="Artist",
        title="Track",
        duration=180,
        quality="320",
        settings=settings,
        started=0.0,
    )

    assert source == "youtube"
    assert info["title"] == "Artist - Track"
    assert any("ytsearch5:" in url for url in urls_downloaded)

