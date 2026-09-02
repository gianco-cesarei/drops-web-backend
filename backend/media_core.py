import json
import os
import re
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import importlib.metadata
import logging
from pathlib import Path

import yt_dlp

logger = logging.getLogger("drops.media")


ALLOWED_DOMAINS = ("youtube.com", "youtu.be", "soundcloud.com", "music.youtube.com", "bandcamp.com", "hearthis.at")


# Single-flight lock shared by the download worker and the BPM engine: only
# one yt-dlp extraction runs per process at a time. Concurrent yt-dlp calls
# from the same IP add up to more "bot-like" traffic and resource contention;
# other jobs block here and run once the lock frees, they don't fail.
YTDLP_LOCK = threading.Lock()
_COOKIE_FILE_LOCK = threading.Lock()


YTDLP_PLAYER_CLIENTS = ["mweb", "android", "ios"]


def ytdlp_extractor_args() -> dict:
    """youtube player clients to try, shared by download and BPM (same engine).

    Render's datacenter IPs trip YouTube's "Sign in to confirm you're not a
    bot" check on the default web client; android/ios/mweb clients
    provide mobile API streams without the desktop web wall.
    """
    args: dict = {
        "youtube": {
            "player_client": list(YTDLP_PLAYER_CLIENTS),
        }
    }
    args.update(_pot_provider_extractor_args())
    return args


def _pot_provider_extractor_args() -> dict:
    """Best-effort PO token via bgutil-ytdlp-pot-provider.

    Helps dodge YouTube's bot-check but never required: needs the pip plugin
    installed (client-side, talks to whichever backend below is available).
    Two backends, tried in order:

    1. HTTP server (DROPS_YTDLP_BGUTIL_HTTP_BASE_URL) - our own Docker image
       runs the bgutil Node server as a sidecar process and sets this env var
       once it's confirmed up (see run_web.py). Preferred: one long-lived
       server handles every request instead of spawning a fresh process per
       token.
    2. Script mode (DROPS_YTDLP_BGUTIL_SCRIPT) - a cloned bgutil script dir
       plus node/deno on PATH, for setups that don't run our Docker image
       (e.g. local dev with the repo cloned manually).

    Neither present -> skip silently, yt-dlp proceeds without a PO token
    exactly like it does today.
    """
    try:
        importlib.metadata.distribution("bgutil-ytdlp-pot-provider")
    except importlib.metadata.PackageNotFoundError:
        logger.info("pot provider: bgutil-ytdlp-pot-provider non installato, PO token disabilitato")
        return {}
    http_base_url = os.environ.get("DROPS_YTDLP_BGUTIL_HTTP_BASE_URL", "").strip()
    if http_base_url:
        return {"youtubepot-bgutilhttp": {"base_url": http_base_url}}
    script_home = os.environ.get("DROPS_YTDLP_BGUTIL_SCRIPT", "").strip() or str(Path.home() / "bgutil-ytdlp-pot-provider" / "server")
    if not os.path.isdir(script_home):
        logger.info("pot provider: nessun server HTTP configurato e script bgutil non trovato in %s, PO token disabilitato", script_home)
        return {}
    return {"youtubepot-bgutilscript": {"server_home": script_home}}


def ytdlp_cookiefile() -> str | None:
    """Path to a Netscape-format cookies file for yt-dlp, shared by download and BPM.

    Render's datacenter IPs get YouTube's "Sign in to confirm you're not a bot"
    bot-check; a browser-exported cookies file is yt-dlp's documented workaround.
    Supports:
    1. DROPS_YTDLP_COOKIES env var (file path or raw Netscape cookie text)
    2. Auto-discovery of Render Secret Files in /etc/secrets/ (cookies.txt, etc.)
    """
    candidates: list[str] = []
    env_val = os.environ.get("DROPS_YTDLP_COOKIES", "").strip()
    if env_val:
        if os.path.isfile(env_val):
            candidates.append(env_val)
        elif _valid_netscape_cookies(env_val):
            try:
                return _write_private_cookie_copy(env_val)
            except Exception as e:
                logger.warning("Failed to write cookies from env var: %s", e)
        else:
            logger.warning("DROPS_YTDLP_COOKIES non e' un file leggibile ne' contenuto Netscape valido; cookie disabilitati")

    # Auto-detect Render Secret Files in /etc/secrets/
    secrets_dir = Path("/etc/secrets")
    if secrets_dir.is_dir():
        try:
            allowed_names = {"cookies.txt", "youtube-cookies.txt", "youtube_cookies.txt"}
            for p in sorted(secrets_dir.iterdir()):
                if p.is_file() and p.name.lower() in allowed_names:
                    candidates.append(str(p))
        except Exception:
            pass

    for path in candidates:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            try:
                content = Path(path).read_text(encoding="utf-8")
                if not _valid_netscape_cookies(content):
                    logger.warning("Cookie file configurato non e' Netscape valido; cookie disabilitati")
                    continue
                return _write_private_cookie_copy(content)
            except Exception:
                logger.warning("Cookie file configurato non leggibile; cookie disabilitati")

    return None


def _valid_netscape_cookies(content: str) -> bool:
    if not content.lstrip().startswith("# Netscape HTTP Cookie File"):
        return False
    for line in content.splitlines():
        if line and not line.startswith("#") and len(line.split("\t")) == 7:
            return True
    return False


def _write_private_cookie_copy(content: str) -> str:
    """Write process-private cookie copy without following predictable symlinks."""
    path = os.path.join(tempfile.gettempdir(), f"drops-youtube-cookies-{os.getpid()}.txt")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    with _COOKIE_FILE_LOCK:
        fd = os.open(path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as handle:
                handle.write(content)
        finally:
            os.close(fd)
    return path


def public_ytdlp_error(exc: Exception) -> str:
    """Return stable user-facing text without leaking yt-dlp CLI guidance."""
    detail = str(exc).casefold()
    if "sign in to confirm you’re not a bot" in detail or "sign in to confirm you're not a bot" in detail:
        return "YouTube richiede una verifica temporanea. Riprova più tardi."
    if "private video" in detail:
        return "Video YouTube privato o non accessibile."
    if "video unavailable" in detail or "this video is unavailable" in detail:
        return "Video YouTube non disponibile."
    if "not available in your country" in detail or "geo" in detail and "blocked" in detail:
        return "Video non disponibile dalla regione del servizio."
    return "Download non riuscito. Riprova tra poco."


def ytdlp_proxy() -> str | None:
    """Optional outbound proxy for yt-dlp's YouTube attempt, from DROPS_YTDLP_PROXY.

    Empty/unset means off - most deployments never set this.
    """
    value = os.environ.get("DROPS_YTDLP_PROXY", "").strip()
    return value or None


def safe_filename(name: str, ext: str) -> str:
    clean = "".join(c for c in name if c.isalnum() or c in " .-_()[]").strip()[:80]
    return f"{clean}.{ext}" if clean else f"audio.{ext}"


def is_supported_url(value: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(value.strip())
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip(".")
    return any(host == domain or host.endswith(f".{domain}") for domain in ALLOWED_DOMAINS)


YOUTUBE_DOMAINS = ("youtube.com", "youtu.be", "music.youtube.com")


def is_youtube_url(value: str) -> bool:
    """True only for YouTube URLs (used by the YouTube-only direct endpoint)."""
    try:
        parsed = urllib.parse.urlsplit(value.strip())
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip(".")
    return any(host == domain or host.endswith(f".{domain}") for domain in YOUTUBE_DOMAINS)


_NOISE_BRACKET_TOKENS = (
    "official", "free download", "premiere", "label", "records", "out now",
    "video oficial", "audio oficial", "hq", "hd", "4k", "1080p", "lyric video",
    "lyrics", "visualizer", "official visualizer", "clip officiel", "remastered",
    "remaster", "full album", "stream & download",
)
_NOISE_STANDALONE = ("free download", "premiere", "out now", "official video", "official audio")
_BRACKET_RE = re.compile(r"[\(\[][^\(\)\[\]]*[\)\]]")
_SPLIT_RE = re.compile(r"\s+[-–—:|//]\s+")
_VINYL_POS_RE = re.compile(r"^(?:[a-dA-D][1-4]?|[1-4]|#[0-9]+)(?:\.|\s*[-–—]|\s+)\s*")
_LEADING_JUNK_RE = re.compile(r"^(?:(?:19|20)\d{2}[\s_.-]?\d{2,4}(?:[\s_.-]?\d{1,4})*|track[\s_-]?\d+|\d{1,3}[\s._-]+|#[0-9]{1,3}[\s._-]*)\s*", re.IGNORECASE)
_CURATOR_CHANNELS = {
    "hate", "hate lab", "moskalus", "slav", "the_substance", "the substance", "substance",
    "boiler room", "cercle", "colors", "colorsxstudios", "houseum",
    "gazzz696", "feel my bicep", "trommel", "meoko", "furthur",
    "the expanse", "jiddisch", "nightclubber ro", "sweet melodies",
}


def strip_noise(raw: str) -> str:
    """Strip boilerplate noise like (Official Video), [Premiere], vinyl positions, dates, etc."""
    cleaned = _LEADING_JUNK_RE.sub("", raw.strip())
    def _drop_if_noise(match: re.Match) -> str:
        full = match.group(0)
        inner = full[1:-1].strip().lower()
        if any(token in inner for token in _NOISE_BRACKET_TOKENS):
            return ""
        if re.search(r"remix(?:19|20)\d{4,}", inner):
            return ""
        if full.startswith("[") and ("free" in inner or "download" in inner or "premiere" in inner or "official" in inner):
            return ""
        return full

    cleaned = _BRACKET_RE.sub(_drop_if_noise, cleaned)
    for token in _NOISE_STANDALONE:
        cleaned = re.sub(re.escape(token), "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -–—:|")
    cleaned = _VINYL_POS_RE.sub("", cleaned).strip(" -–—:|")
    return cleaned


_strip_noise = strip_noise


def parse_artist_title(raw_title: str, fallback_artist: str | None = None) -> tuple[str | None, str]:
    """Best-effort "Artist - Title" split, with noise like (Official Video) and upload codes stripped first."""
    cleaned = strip_noise(raw_title)
    parts = _SPLIT_RE.split(cleaned, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        artist = _VINYL_POS_RE.sub("", parts[0].strip()).strip(" -–—:|")
        title = _VINYL_POS_RE.sub("", parts[1].strip()).strip(" -–—:|")
        return artist, title
    if fallback_artist:
        fb = re.sub(r"\s*-\s*Topic\b", "", fallback_artist, flags=re.IGNORECASE).strip()
        fb = re.sub(r"\s*\((?:Topic|Official)\)\s*", "", fb, flags=re.IGNORECASE).strip()
        fb_clean = re.sub(r"[^a-z0-9]+", " ", fb.casefold()).strip()
        if fb_clean in _CURATOR_CHANNELS or "premiere" in fb_clean or "repost" in fb_clean:
            return None, cleaned.strip()
        return fb.strip() or None, cleaned.strip()
    return None, cleaned.strip()


def _oembed(endpoint: str, url: str) -> dict | None:
    query = urllib.parse.urlencode({"url": url, "format": "json"})
    request = urllib.request.Request(f"{endpoint}?{query}", headers={"User-Agent": "Drops/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return json.loads(response.read())
    except Exception as exc:
        # Bare Exception, not just URLError/ValueError: http.client exceptions
        # (IncompleteRead, BadStatusLine, ...) are not OSError subclasses, so
        # urllib does not wrap them into URLError - they'd otherwise escape
        # here and break resolve_track's "never raises" guarantee. Mirrors
        # the same bare-except pattern used in _resolve_via_ytdlp below.
        logger.info("resolve_track oembed fallita endpoint=%s error=%s", endpoint, exc)
        return None


def _resolve_via_ytdlp(url: str) -> dict:
    """Metadata-only fallback when oEmbed can't answer (private/unlisted/oembed-less sources).

    skip_download=True never resolves a playable stream format, so this never
    reaches the parts of yt-dlp that trigger YouTube's bot-check on a cold IP.
    """
    options = {
        "skip_download": True, "quiet": True, "no_warnings": True, "noplaylist": True,
        "socket_timeout": 15, "extractor_args": ytdlp_extractor_args(),
    }
    cookies = ytdlp_cookiefile()
    if cookies:
        options["cookiefile"] = cookies
    try:
        with YTDLP_LOCK, yt_dlp.YoutubeDL(options) as ydl:
            # extract_info can return None without raising on some
            # flat/playlist extraction paths; treat that the same as an
            # extraction failure instead of crashing on info.get(...) below.
            info = ydl.extract_info(url, download=False) or {}
    except Exception as exc:
        logger.info("resolve_track ytdlp fallback fallito url_host=%s error=%r", urllib.parse.urlsplit(url).hostname, str(exc)[:200])
        return {"title": None, "artist": None, "raw_title": None, "cover_url": None, "duration": None}
    raw_title = str(info.get("title")) if info.get("title") else None
    if raw_title:
        artist, title = parse_artist_title(raw_title, info.get("uploader"))
    else:
        artist, title = info.get("uploader"), None
    return {"title": title, "artist": artist, "raw_title": raw_title, "cover_url": info.get("thumbnail"), "duration": info.get("duration")}


def resolve_track(url: str) -> dict:
    """Fast, metadata-only track recognition - never downloads audio, never touches the bot wall.

    Order: oEmbed (no auth, no bot-check) for YouTube/SoundCloud, then a
    yt-dlp skip_download fallback. Always returns a dict, never raises -
    recognition failures degrade to an unknown-track job instead of blocking
    the "card appears instantly" flow.
    """
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    endpoint = None
    if host == "soundcloud.com" or host.endswith(".soundcloud.com"):
        endpoint = "https://soundcloud.com/oembed"
    elif host in {"youtube.com", "youtu.be", "music.youtube.com"} or host.endswith((".youtube.com", ".youtu.be")):
        endpoint = "https://www.youtube.com/oembed"
    oembed = _oembed(endpoint, url) if endpoint else None
    if oembed and oembed.get("title"):
        raw_title = str(oembed["title"])
        artist, title = parse_artist_title(raw_title, oembed.get("author_name"))
        return {"title": title, "artist": artist, "raw_title": raw_title, "cover_url": oembed.get("thumbnail_url"), "duration": None}
    return _resolve_via_ytdlp(url)


def tag_audio_file(
    file_path: Path | str,
    *,
    title: str | None = None,
    artist: str | None = None,
    album: str | None = None,
    label: str | None = None,
    year: int | None = None,
    genre: str | None = None,
    bpm: float | int | None = None,
    cover_data: bytes | None = None,
    cover_mime: str = "image/jpeg",
) -> bool:
    """Write ID3v2.3 tags and embedded cover art to an MP3 file via mutagen."""
    try:
        from mutagen.id3 import ID3, TIT2, TPE1, TALB, TPUB, TDRC, TCON, TBPM, APIC, ID3NoHeaderError
    except ImportError:
        logger.info("mutagen non installato, skip scrittura tag ID3")
        return False

    target = Path(file_path)
    if not target.exists() or target.suffix.lower() != ".mp3":
        return False

    try:
        try:
            tags = ID3(target)
        except ID3NoHeaderError:
            tags = ID3()

        if title:
            tags["TIT2"] = TIT2(encoding=3, text=str(title))
        if artist:
            tags["TPE1"] = TPE1(encoding=3, text=str(artist))
        if album:
            tags["TALB"] = TALB(encoding=3, text=str(album))
        if label:
            tags["TPUB"] = TPUB(encoding=3, text=str(label))
        if year:
            tags["TDRC"] = TDRC(encoding=3, text=str(year))
        if genre:
            tags["TCON"] = TCON(encoding=3, text=str(genre))
        if bpm:
            tags["TBPM"] = TBPM(encoding=3, text=str(int(round(float(bpm)))))

        if cover_data:
            tags["APIC"] = APIC(
                encoding=3,
                mime=cover_mime,
                type=3,  # Front cover
                desc="Cover",
                data=cover_data,
            )

        tags.save(target, v2_version=3)
        return True
    except Exception as exc:
        logger.warning("tag_audio_file fallito per %s detail=%r", target.name, str(exc)[:200])
        return False
