from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
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


def test_httponly_netscape_cookies_are_valid(monkeypatch, tmp_path: Path):
    raw = "# Netscape HTTP Cookie File\n#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t2147483647\tLOGIN_INFO\tabc123val"
    monkeypatch.setenv("DROPS_YTDLP_COOKIES", raw)
    monkeypatch.setattr(media_core.tempfile, "gettempdir", lambda: str(tmp_path))

    result = Path(media_core.ytdlp_cookiefile() or "")

    assert result.read_text() == raw


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


def test_bot_check_is_not_retried_before_multi_source_fallback(monkeypatch, tmp_path: Path):
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
            raise yt_dlp.utils.DownloadError("Sign in to confirm you’re not a bot")

    monkeypatch.setattr(download_engine.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr(download_engine.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(download_engine, "ytdlp_cookiefile", lambda: None)
    monkeypatch.setattr(download_engine, "ytdlp_extractor_args", lambda: {"youtube": {}})
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    settings = SimpleNamespace(max_duration_seconds=900, max_file_bytes=100_000_000)

    with pytest.raises(yt_dlp.utils.DownloadError, match="confirm"):
        download_engine.attempt_download(
            job_dir, "https://youtube.com/watch?v=abc", "320", settings, 0.0,
            proxy="http://proxy.example:8080",
        )

    assert FakeYoutubeDL.calls == 1
    assert options_seen[0]["proxy"] == "http://proxy.example:8080"


def test_lock_timeout_fails_without_entering_ytdlp(monkeypatch, tmp_path: Path):
    class BusyLock:
        def acquire(self, timeout):
            assert timeout >= 1
            return False

        def release(self):
            raise AssertionError("unacquired lock must not be released")

    monkeypatch.setattr(download_engine, "YTDLP_LOCK", BusyLock())
    monkeypatch.setattr(download_engine, "ytdlp_cookiefile", lambda: None)
    monkeypatch.setattr(download_engine, "ytdlp_extractor_args", lambda: {"youtube": {}})
    job_dir = tmp_path / "job"
    job_dir.mkdir()

    with pytest.raises(yt_dlp.utils.DownloadError, match="lock timeout"):
        download_engine.attempt_download(
            job_dir,
            "https://youtube.com/watch?v=abc",
            "320",
            SimpleNamespace(max_duration_seconds=900, max_file_bytes=100_000_000),
            0.0,
        )


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


def test_ytdlp_extractor_args_with_cookies_prioritizes_web_and_no_skip():
    args = media_core.ytdlp_extractor_args(has_cookies=True)
    yt = args["youtube"]
    assert "web" in yt["player_client"]
    assert "web_safari" in yt["player_client"]
    assert "player_skip" not in yt or "web" not in yt["player_skip"]


def test_ytdlp_extractor_args_without_cookies_skips_web(monkeypatch):
    # Without POT provider: must skip web to avoid datacenter bot-check
    monkeypatch.setattr(media_core, "_pot_provider_extractor_args", lambda: {})
    monkeypatch.delenv("DROPS_YTDLP_PO_TOKEN", raising=False)
    args = media_core.ytdlp_extractor_args(has_cookies=False)
    yt = args["youtube"]
    assert "android" in yt["player_client"]
    assert "web" in yt.get("player_skip", [])

    # With POT provider: allows web + mweb
    monkeypatch.setattr(media_core, "_pot_provider_extractor_args", lambda: {"youtubepot-bgutilhttp": {"base_url": ["http://127.0.0.1:4416"]}})
    args_pot = media_core.ytdlp_extractor_args(has_cookies=False)
    yt_pot = args_pot["youtube"]
    assert "web" in yt_pot["player_client"]
    assert "player_skip" not in yt_pot or "web" not in yt_pot["player_skip"]


def test_ytdlp_user_agent_matching_and_override(monkeypatch):
    monkeypatch.delenv("DROPS_YTDLP_USER_AGENT", raising=False)
    assert media_core.ytdlp_user_agent(has_cookies=False) is None
    assert "Mozilla/5.0" in (media_core.ytdlp_user_agent(has_cookies=True) or "")

    monkeypatch.setenv("DROPS_YTDLP_USER_AGENT", "CustomUserAgent/2.0")
    assert media_core.ytdlp_user_agent(has_cookies=True) == "CustomUserAgent/2.0"
    assert media_core.ytdlp_user_agent(has_cookies=False) == "CustomUserAgent/2.0"


def test_ytdlp_cookiefile_direct_when_writable(monkeypatch, tmp_path: Path):
    writable = tmp_path / "cookies.txt"
    writable.write_text("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\tvalue\n")
    monkeypatch.setenv("DROPS_YTDLP_COOKIES", str(writable))
    monkeypatch.setattr(media_core.Path, "is_dir", lambda _self: False)

    res = media_core.ytdlp_cookiefile()
    assert res == str(writable.resolve())


def test_ytdlp_cookiefile_copy_when_readonly(monkeypatch, tmp_path: Path):
    import os
    ro_dir = tmp_path / "secrets"
    ro_dir.mkdir()
    ro_file = ro_dir / "cookies.txt"
    ro_file.write_text("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\tvalue\n")
    # Mark read-only
    os.chmod(ro_file, 0o400)
    monkeypatch.setenv("DROPS_YTDLP_COOKIES", str(ro_file))
    monkeypatch.setattr(media_core.Path, "is_dir", lambda _self: False)

    res = media_core.ytdlp_cookiefile()
    # Must NOT be the read-only file itself, but a writable temp copy
    assert res != str(ro_file.resolve())
    assert res is not None
    assert Path(res).is_file()
    assert os.access(res, os.W_OK)
    os.chmod(ro_file, 0o600)


def test_attempt_download_client_tiers_rotate_web_when_cookies_present(monkeypatch, tmp_path: Path):
    options_seen = []

    class FakeYoutubeDL:
        def __init__(self, options):
            options_seen.append(dict(options))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download):
            return {"title": "Test Track", "duration": 120}

    monkeypatch.setattr(download_engine.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr(download_engine, "ytdlp_cookiefile", lambda: "/tmp/fake-cookies.txt")

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    settings = SimpleNamespace(max_duration_seconds=900, max_file_bytes=100_000_000)

    download_engine.attempt_download(
        job_dir, "https://youtube.com/watch?v=qPcX4F5J4fk", "320", settings, 0.0
    )

    assert len(options_seen) >= 1
    first_attempt_opts = options_seen[0]
    assert first_attempt_opts.get("cookiefile") == "/tmp/fake-cookies.txt"
    assert first_attempt_opts.get("user_agent") is not None
    yt_args = first_attempt_opts["extractor_args"]["youtube"]
    assert "web" in yt_args["player_client"]
    assert "web" not in yt_args.get("player_skip", [])


def test_download_multi_source_falls_back_to_unauth_android_on_bot_check(monkeypatch, tmp_path: Path):
    """When native YouTube download hits bot-check (e.g. challenged cookies on datacenter IP)
    and SoundCloud has no matching track, it must fall back to unauthenticated android client
    which succeeds without bot-check."""
    attempts = []

    def fake_attempt_download(job_dir, url, quality, settings, started, proxy=None, force_unauth=False):
        attempts.append({"url": url, "force_unauth": force_unauth})
        if not force_unauth and "youtube.com" in url:
            raise yt_dlp.utils.DownloadError("Sign in to confirm you’re not a bot")
        if force_unauth and "youtube.com" in url:
            return {"title": "Ulo S. - Visitors From The Low End", "duration": 491}
        raise RuntimeError("Candidate failed")

    monkeypatch.setattr(download_engine, "attempt_download", fake_attempt_download)
    monkeypatch.setattr(download_engine, "find_soundcloud_match", lambda *args, **kwargs: None)

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    settings = SimpleNamespace(max_duration_seconds=900, max_file_bytes=100_000_000)

    info, source = download_engine.download_multi_source(
        job_dir=job_dir,
        job_id="test-unauth-fallback",
        native_url="https://www.youtube.com/watch?v=qPcX4F5J4fk",
        artist="Ulo S.",
        title="Visitors From The Low End",
        duration=491,
        quality="320",
        settings=settings,
        started=0.0,
    )

    assert source == "youtube"
    assert info["title"] == "Ulo S. - Visitors From The Low End"
    assert len(attempts) >= 2
    assert attempts[0]["force_unauth"] is False
    assert attempts[-1]["force_unauth"] is True


