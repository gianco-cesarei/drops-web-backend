"""Multi-source download engine: SoundCloud first (matched by similarity and
duration), the link's own native source (usually YouTube) always last,
transparently to the caller."""

from __future__ import annotations

import difflib
import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yt_dlp

from media_core import (
    FFMPEG_SEMAPHORE,
    YTDLP_LOCK,
    is_youtube_url,
    strip_noise,
    ytdlp_cookiefile,
    ytdlp_extractor_args,
    ytdlp_js_runtimes,
    ytdlp_source_address,
    ytdlp_user_agent,
)

logger = logging.getLogger("drops.download")

AUDIO_QUALITY = {"128": "128", "192": "192", "320": "320", "mp3": "320", "hq": "320", "flac": "0"}

# Our own abort messages (progress hook / duration check) - never retryable,
# retrying an oversized/too-long media just repeats the same failure.
DOWNLOAD_ABORT_MESSAGES = {
    "Download duration limit exceeded",
    "Download size limit exceeded",
    "Media duration limit exceeded",
}

YOUTUBE_FALLBACK_ERROR_MARKERS = (
    "sign in to confirm",
    "youtube verification required",
    "video unavailable",
    "this video is unavailable",
    "private video",
    "not available in your country",
    "age-restricted",
    "confirm your age",
    "no video formats found",
    "requested format is not available",
)

YOUTUBE_NO_RETRY_MARKERS = (
    "sign in to confirm",
    "youtube verification required",
    "private video",
    "this video has been removed",
    "account has been terminated",
    "copyright",
)

SOUNDCLOUD_SEARCH_COUNT = 5
DURATION_TOLERANCE_SECONDS = 15
DURATION_CLOSE_TOLERANCE_SECONDS = 5
SIMILARITY_THRESHOLD = 0.5


def _normalize(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").casefold()).strip()


def similarity(a: str | None, b: str | None) -> float:
    return difflib.SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


def _token_overlap(query: str, target: str) -> float:
    query_words = set(_normalize(query).split())
    if not query_words:
        return 0.0
    target_words = set(_normalize(target).split())
    return len(query_words & target_words) / len(query_words)


def _normalize_descriptors(value: str | None) -> str:
    return _normalize(value)


MODIFIER_ADJECTIVES = {
    "extended", "original", "club", "radio", "vip", "instrumental", "vocal", "dub",
    "full", "long", "short", "edit", "rework", "acoustic", "live", "special", "main",
    "bootleg", "version", "mix", "cut",
}

_VERSION_RE = re.compile(
    r"\(([^()]+?)\s+(remix|edit|dub|vip|rework|version|mix|club mix|extended mix|original mix|bootleg|acapella|instrumental)\)"
    r"|\[([^\[\]]+?)\s+(remix|edit|dub|vip|rework|version|mix|club mix|extended mix|original mix|bootleg|acapella|instrumental)\]"
    r"|(?:^|\s*[-–—:]\s*)([A-Za-z0-9\s&.+'-]+?)\s+(remix|edit|dub|vip|rework|version|mix|club mix|extended mix|original mix|bootleg|acapella|instrumental)\b",
    re.IGNORECASE,
)


def extract_version_info(text: str | None) -> dict[str, Any]:
    """Extract remixer name and version type (e.g., Manoo Remix, Tale Of Us Edit, Extended Mix, Dub)."""
    if not text:
        return {"remixer": None, "version_type": None, "descriptors": set()}

    norm_text = _normalize(text)
    descriptors = set()
    remixer = None
    version_type = None

    for m in _VERSION_RE.finditer(text):
        raw_prefix = m.group(1) or m.group(3) or m.group(5) or ""
        v_type = (m.group(2) or m.group(4) or m.group(6) or "").lower().strip()
        prefix_norm = _normalize(raw_prefix)

        prefix_words = set(prefix_norm.split())
        if prefix_words and not prefix_words.issubset(MODIFIER_ADJECTIVES):
            remixer = prefix_norm
            version_type = v_type
            descriptors.update(prefix_words)
            descriptors.add(v_type)
            break
        else:
            if prefix_norm:
                version_type = f"{prefix_norm} {v_type}".strip()
                descriptors.update(prefix_words)
            else:
                version_type = v_type
            descriptors.add(v_type)

    if not version_type:
        for kw in ["remix", "edit", "dub", "vip", "rework", "acapella", "instrumental", "extended"]:
            if kw in norm_text.split():
                descriptors.add(kw)
                if not version_type:
                    version_type = kw

    return {
        "remixer": remixer,
        "version_type": version_type,
        "descriptors": descriptors,
    }


def score_candidate(
    artist: str | None,
    title: str | None,
    entry: dict[str, Any],
    *,
    catalog_no: str | None = None,
    label: str | None = None,
) -> float:
    candidate_title = str(entry.get("title") or "")
    candidate_uploader = str(entry.get("uploader") or "")
    candidate_combined = f"{candidate_title} {candidate_uploader}".strip()
    cand_norm = _normalize(candidate_combined)

    title_words = set(_normalize(title).split()) if title else set()
    if title and title_words:
        t_overlap = _token_overlap(title, candidate_combined)
        t_desc_overlap = _token_overlap(_normalize_descriptors(title), _normalize_descriptors(candidate_combined))
        effective_t_overlap = max(t_overlap, t_desc_overlap)
        if effective_t_overlap == 0.0:
            return 0.0
        if len(title_words) >= 2 and effective_t_overlap < 0.6:
            return 0.3 * effective_t_overlap
    else:
        t_overlap = 0.0
        effective_t_overlap = 0.0

    combined_query = f"{artist or ''} {title or ''}".strip()
    scores = [
        similarity(title, candidate_title),
        similarity(_normalize_descriptors(title), _normalize_descriptors(candidate_title)),
        similarity(combined_query, candidate_title),
        _token_overlap(combined_query, candidate_combined),
        effective_t_overlap,
    ]
    if artist:
        scores.append((similarity(title, candidate_title) + similarity(artist, candidate_uploader)) / 2)
        scores.append((effective_t_overlap + _token_overlap(artist, candidate_combined)) / 2)

    base_score = max(scores)

    ref_v = extract_version_info(title)
    cand_v = extract_version_info(candidate_combined)

    if ref_v["remixer"]:
        ref_remixer = ref_v["remixer"]
        cand_remixer = cand_v["remixer"]
        if cand_remixer and cand_remixer != ref_remixer:
            return base_score * 0.2
        if ref_remixer not in cand_norm:
            return base_score * 0.3
        base_score = max(base_score, 0.85)

    if ref_v["version_type"]:
        ref_vt = ref_v["version_type"]
        if ref_vt in {"dub", "vip", "acapella", "instrumental"} and ref_vt not in cand_norm:
            return base_score * 0.3

    if catalog_no:
        cat_clean = _normalize(catalog_no)
        if cat_clean and cat_clean in cand_norm:
            base_score = max(base_score, 0.85)

    if label:
        lbl_clean = _normalize(label)
        if lbl_clean and lbl_clean in cand_norm:
            base_score = min(1.0, base_score + 0.1)

    return base_score


def strict_candidate_match(
    artist: str | None,
    title: str | None,
    duration: int | None,
    entry: dict[str, Any],
    *,
    catalog_no: str | None = None,
    label: str | None = None,
) -> tuple[bool, float, str]:
    """Gate fallback candidates tightly enough to avoid substituting another track."""
    score = score_candidate(artist, title, entry, catalog_no=catalog_no, label=label)
    if not artist or not title:
        return False, score, "missing_reference_metadata"

    candidate_combined = f"{entry.get('title') or ''} {entry.get('uploader') or ''}".strip()
    cand_norm = _normalize(candidate_combined)

    ref_v = extract_version_info(title)
    cand_v = extract_version_info(candidate_combined)

    if ref_v["remixer"]:
        ref_remixer = ref_v["remixer"]
        cand_remixer = cand_v["remixer"]
        if cand_remixer and cand_remixer != ref_remixer:
            return False, score, "remix_mismatch"
        if ref_remixer not in cand_norm:
            return False, score, "remix_mismatch"

    if ref_v["version_type"]:
        ref_vt = ref_v["version_type"]
        cand_vt = cand_v["version_type"]
        if ref_vt in {"dub", "vip", "acapella", "instrumental"}:
            if ref_vt not in cand_norm:
                return False, score, "version_mismatch"
        elif cand_vt and cand_vt != ref_vt:
            if ref_vt not in cand_norm:
                return False, score, "version_mismatch"

    artist_overlap = _token_overlap(artist, candidate_combined)
    if artist_overlap < 0.6:
        return False, score, "artist_mismatch"

    title_overlap = max(
        _token_overlap(title, candidate_combined),
        _token_overlap(_normalize_descriptors(title), _normalize_descriptors(candidate_combined)),
    )
    if title_overlap < 0.75:
        return False, score, "title_mismatch"

    candidate_duration = entry.get("duration")
    if duration is not None:
        if candidate_duration is None:
            return False, score, "duration_missing"
        if abs(float(candidate_duration) - duration) > DURATION_TOLERANCE_SECONDS:
            return False, score, "duration_mismatch"

    minimum_score = 0.75 if duration is not None else 0.8
    if score < minimum_score:
        return False, score, "score_below_threshold"

    return True, score, "accepted"


def youtube_error_allows_fallback(exc: Exception) -> bool:
    detail = str(exc).casefold()
    if any(msg.casefold() in detail for msg in DOWNLOAD_ABORT_MESSAGES):
        return False
    return True


def find_soundcloud_match(
    artist: str | None,
    title: str | None,
    duration: int | None,
    raw_title: str | None = None,
    catalog_no: str | None = None,
    strict: bool = False,
    label: str | None = None,
    *,
    proxy: str | None = None,
) -> str | None:
    """Search SoundCloud for a track matching artist+title/raw_title, gated by duration when known."""
    if not artist and not title and not raw_title and not catalog_no and not label:
        return None

    queries = build_search_queries(artist, title, raw_title=raw_title, catalog_no=catalog_no, label=label)
    if not queries:
        return None

    search_t0 = time.monotonic()
    options = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "ignore_no_formats_error": True,
        "socket_timeout": 15,
        "extractor_args": ytdlp_extractor_args(),
    }
    if proxy:
        options["proxy"] = proxy

    all_entries: dict[str, dict[str, Any]] = {}
    for query in queries:
        query_entries: list[dict[str, Any]] = []
        try:
            with YTDLP_LOCK, yt_dlp.YoutubeDL(options) as ydl:
                result = ydl.extract_info(f"scsearch{SOUNDCLOUD_SEARCH_COUNT}:{query}", download=False)
            for entry in (result or {}).get("entries") or []:
                if not entry or not isinstance(entry, dict):
                    continue
                url = entry.get("webpage_url") or entry.get("url")
                if url and url not in all_entries:
                    all_entries[url] = entry
                    query_entries.append(entry)
        except Exception as exc:
            logger.info("soundcloud search fallita query=%r detail=%r", query, str(exc)[:200])

        # Early exit on high confidence match
        for entry in query_entries:
            cand_duration = entry.get("duration")
            url = entry.get("webpage_url") or entry.get("url")
            if not url:
                continue
            if duration is not None and not strict:
                if cand_duration is None or abs(cand_duration - duration) > 3:
                    continue
            score = score_candidate(artist, title, entry, catalog_no=catalog_no, label=label)
            if strict:
                continue
            if score >= 0.85:
                logger.info(
                    "soundcloud early exit query=%r url=%s score=%.2f duration_diff=%s elapsed=%.1fs",
                    query, url, score,
                    f"{abs(cand_duration - duration)}s" if (duration is not None and cand_duration is not None) else "unknown",
                    time.monotonic() - search_t0,
                )
                return url

    best_url, best_score = None, 0.0
    scored_candidates = []

    for url, entry in all_entries.items():
        cand_title = entry.get("title")
        cand_duration = entry.get("duration")

        if duration is not None:
            if cand_duration is None or abs(cand_duration - duration) > DURATION_TOLERANCE_SECONDS:
                score = score_candidate(artist, title, entry, catalog_no=catalog_no, label=label)
                scored_candidates.append((url, cand_title, cand_duration, score, "duration_mismatch"))
                continue

        score = score_candidate(artist, title, entry, catalog_no=catalog_no, label=label)

        if strict:
            accepted, score, reason = strict_candidate_match(
                artist, title, duration, entry, catalog_no=catalog_no, label=label,
            )
            scored_candidates.append((url, cand_title, cand_duration, score, reason))
            if accepted and score > best_score:
                best_score, best_url = score, url
            continue

        if duration is not None and cand_duration is not None and abs(cand_duration - duration) <= DURATION_CLOSE_TOLERANCE_SECONDS:
            min_threshold = 0.4
        elif duration is not None:
            min_threshold = SIMILARITY_THRESHOLD
        else:
            min_threshold = 0.55

        status = "accepted" if score >= min_threshold else "below_threshold"
        scored_candidates.append((url, cand_title, cand_duration, score, status))

        if score >= min_threshold and score > best_score:
            best_score, best_url = score, url

    if scored_candidates:
        top_candidates = sorted(scored_candidates, key=lambda x: x[3], reverse=True)[:5]
        logger.info(
            "soundcloud candidates queries=%r duration=%s total=%d top=%s chosen=%s (score=%.2f) elapsed=%.1fs",
            queries, duration, len(scored_candidates),
            [(c[1], c[2], round(c[3], 2), c[4]) for c in top_candidates],
            best_url, best_score, time.monotonic() - search_t0,
        )
    else:
        logger.info("soundcloud no candidates found for queries=%r duration=%s elapsed=%.1fs", queries, duration, time.monotonic() - search_t0)

    return best_url


def build_search_queries(
    artist: str | None,
    title: str | None,
    raw_title: str | None = None,
    catalog_no: str | None = None,
    label: str | None = None,
) -> list[str]:
    """Build an ordered list of search queries from most specific/clean to broader fallbacks."""
    queries: list[str] = []
    seen: set[str] = set()

    clean_artist = re.sub(r"\s*-\s*Topic\b", "", artist or "", flags=re.IGNORECASE).strip() or None
    clean_artist = re.sub(r"\s*\((?:Topic|Official)\)\s*", "", clean_artist or "", flags=re.IGNORECASE).strip() or None
    clean_title = strip_noise(title) if title else None
    clean_raw = strip_noise(raw_title) if raw_title else None
    clean_cat = catalog_no.strip() if catalog_no and catalog_no.strip() else None
    clean_lbl = label.strip() if label and label.strip() else None

    full_title = clean_title or title or clean_raw or raw_title

    candidates = [
        f"{clean_artist} {full_title} {clean_cat}".strip() if clean_artist and full_title and clean_cat else None,
        f"{clean_artist} {full_title} {clean_lbl}".strip() if clean_artist and full_title and clean_lbl else None,
        f"{clean_artist} {full_title} {clean_lbl} {clean_cat}".strip() if clean_artist and full_title and clean_lbl and clean_cat else None,
        f"{clean_lbl} {clean_cat} {full_title}".strip() if clean_lbl and clean_cat and full_title else None,
        f"{clean_cat} {full_title}".strip() if clean_cat and full_title else None,
        f"{clean_artist} {clean_title}".strip() if clean_artist and clean_title else None,
        f"{artist} {title}".strip() if artist and title else None,
        f"{clean_artist} {title}".strip() if clean_artist and title else None,
        clean_title.strip() if clean_title else None,
        title.strip() if title else None,
        clean_raw.strip() if clean_raw else None,
        f"{clean_artist} {clean_title} audio".strip() if clean_artist and clean_title else None,
        f"{clean_cat} {clean_title}".strip() if clean_cat and clean_title else None,
        catalog_no.strip() if catalog_no else None,
    ]

    for cand in candidates:
        if cand:
            norm = _normalize(cand)
            if norm and norm not in seen:
                seen.add(norm)
                queries.append(cand)

    return queries


def attempt_download(
    job_dir: Path,
    url: str,
    quality: str,
    settings,
    started: float,
    *,
    proxy: str | None = None,
    force_unauth: bool = False,
) -> dict[str, Any]:
    """Run yt-dlp against a single candidate url, with existing retry/limit behavior. Raises on total failure."""

    def progress(event: dict) -> None:
        if time.monotonic() - started > settings.max_duration_seconds:
            raise yt_dlp.utils.DownloadError("Download duration limit exceeded")
        downloaded = int(event.get("downloaded_bytes") or 0)
        total = int(event.get("total_bytes") or event.get("total_bytes_estimate") or 0)
        if max(downloaded, total) > settings.max_file_bytes:
            raise yt_dlp.utils.DownloadError("Download size limit exceeded")

    def duration_filter(info: dict, *, incomplete: bool):
        duration = int(info.get("duration") or 0)
        if duration > settings.max_duration_seconds:
            return "Media duration limit exceeded"
        return None

    # Upgrade single-result ytsearch queries to ytsearch5 to allow inspecting alternative candidates
    target_url = re.sub(r"^ytsearch[1-4]:", "ytsearch5:", url)
    is_search = bool(re.search(r"^(?:yt|sc)search\d*:", target_url))
    is_yt = is_youtube_url(target_url) or "ytsearch" in target_url
    cookies = None if force_unauth else ytdlp_cookiefile()
    has_cookies = False if force_unauth else bool(cookies)

    try:
        current_extractor_args = ytdlp_extractor_args(has_cookies=has_cookies)
    except TypeError:
        current_extractor_args = ytdlp_extractor_args()

    try:
        ua = ytdlp_user_agent(has_cookies=has_cookies)
    except TypeError:
        ua = ytdlp_user_agent()

    target_codec = "flac" if str(quality).lower() in {"hq", "flac"} else "mp3"
    postprocessor = {
        "key": "FFmpegExtractAudio",
        "preferredcodec": target_codec,
    }
    if target_codec == "mp3":
        postprocessor["preferredquality"] = AUDIO_QUALITY.get(quality, "320")

    if target_codec == "flac":
        format_spec = "bestaudio[acodec=flac]/bestaudio[protocol^=http]/bestaudio/best"
    elif is_yt:
        format_spec = "bestaudio/best"
    else:
        format_spec = "bestaudio[acodec=mp3][protocol^=http]/bestaudio[acodec=mp3]/bestaudio/best"

    options = {
        "format": format_spec,
        "postprocessors": [postprocessor],
        "postprocessor_args": ["-ar", "44100"],
        "outtmpl": str(job_dir / "source.%(ext)s"),
        "quiet": True, "no_warnings": True,
        "noplaylist": not is_search,
        "max_downloads": 1 if is_search else None,
        "ignoreerrors": is_search,
        "max_filesize": settings.max_file_bytes,
        "match_filter": duration_filter,
        "socket_timeout": 30,
        "nocheckcertificate": True,
        "concurrent_fragment_downloads": 4,
        "progress_hooks": [progress],
        "extractor_args": current_extractor_args,
    }
    js_runtimes = ytdlp_js_runtimes()
    if js_runtimes:
        options["js_runtimes"] = js_runtimes
    if cookies:
        options["cookiefile"] = cookies
    if ua:
        options["user_agent"] = ua
    if proxy:
        options["proxy"] = proxy
    src_addr = ytdlp_source_address()
    if src_addr:
        options["source_address"] = src_addr

    if has_cookies:
        # Authenticated cookies (from desktop browser) match desktop 'web' client.
        CLIENT_TIERS = [
            ["web", "web_embedded"],
            ["web_embedded", "web"],
            ["mweb"],
            ["android"],
        ]
    else:
        # Unauthenticated datacenter IPs: with bgutil POT provider, prioritize web + web_embedded
        CLIENT_TIERS = [
            ["web", "web_embedded"],
            ["web_embedded", "web"],
            ["android", "mweb"],
            ["tv", "android"],
        ]

    info = None
    last_extract_error: Exception | None = None
    current_options = dict(options)

    for attempt in range(1, 5):
        # Rotate player clients across attempts
        client_tier = CLIENT_TIERS[attempt - 1] if attempt <= len(CLIENT_TIERS) else CLIENT_TIERS[0]
        extractor_args = dict(options.get("extractor_args") or {})
        if "youtube" in extractor_args:
            yt_args = dict(extractor_args["youtube"])
            yt_args["player_client"] = list(client_tier)
            if has_cookies and "player_skip" in yt_args:
                yt_args["player_skip"] = [s for s in yt_args["player_skip"] if s != "web"]
                if not yt_args["player_skip"]:
                    yt_args.pop("player_skip", None)
            extractor_args["youtube"] = yt_args
        current_options["extractor_args"] = extractor_args

        # yt-dlp refuses to use android client if cookies are present; strip cookies when trying android
        if "android" in client_tier:
            current_options.pop("cookiefile", None)
            current_options.pop("user_agent", None)
            try:
                current_options["extractor_args"] = ytdlp_extractor_args(has_cookies=False)
            except Exception:
                pass

        # When cookies are present, direct connection with Netscape cookies + Node JS solver
        # is reliable and avoids shared proxy rate-limits. Try direct first on attempts 1-2, keep proxy for fallback.
        if has_cookies:
            if attempt <= 2:
                current_options.pop("proxy", None)
            elif proxy:
                current_options["proxy"] = proxy

        # Format fallback on later attempts if rigid format fails
        if attempt >= 3:
            current_options["format"] = "bestaudio/best"

        acquired = YTDLP_LOCK.acquire(timeout=max(1.0, settings.max_duration_seconds - (time.monotonic() - started)))
        if not acquired:
            raise yt_dlp.utils.DownloadError("Download worker lock timeout")
        try:
            with FFMPEG_SEMAPHORE:
                with yt_dlp.YoutubeDL(current_options) as ydl:
                    info = ydl.extract_info(target_url, download=True)
            break
        except Exception as exc:
            last_extract_error = exc
            for leftover in job_dir.iterdir():
                if leftover.is_file():
                    leftover.unlink(missing_ok=True)
            exc_str = str(exc).lower()
            proxy_failure = any(marker in exc_str for marker in (
                "407 proxy authentication", "proxyconnect", "proxy connection",
                "unable to connect to proxy", "tunnel connection failed",
                "timed out", "read timed out", "connection reset by peer",
                "rate-limited", "this content isn't available", "try again later",
                "429", "too many requests",
            ))
            if "proxy" in current_options and proxy_failure:
                logger.warning("Proxy error or rate-limit (%r), dropping proxy for direct fallback", str(exc)[:150])
                current_options.pop("proxy", None)
                continue
            # If cookies were challenged, invalidated, or triggered reload/signature errors, drop cookies immediately and retry unauthenticated
            if "cookiefile" in current_options and any(marker in exc_str for marker in (
                "sign in to confirm", "anti-bot", "no longer valid", "verification required", "page needs to be reloaded", "signature solving failed",
            )):
                logger.warning("Cookie authentication challenged or invalidated (%r), dropping cookiefile for unauthenticated retry", str(exc)[:150])
                current_options.pop("cookiefile", None)
                current_options.pop("user_agent", None)
                try:
                    current_options["extractor_args"] = ytdlp_extractor_args(has_cookies=False)
                except Exception:
                    pass
                continue
            if (
                str(exc) in DOWNLOAD_ABORT_MESSAGES
                or any(marker in exc_str for marker in YOUTUBE_NO_RETRY_MARKERS)
                or attempt == 4
            ):
                raise
            logger.warning("download retrying attempt=%s error=%r", attempt, str(exc)[:150])
            time.sleep(attempt * 0.5)
        finally:
            YTDLP_LOCK.release()
    if info is None:
        raise last_extract_error or RuntimeError("Download failed")
    if isinstance(info, dict) and "entries" in info:
        entries = [e for e in (info.get("entries") or []) if e and isinstance(e, dict)]
        if entries:
            info = entries[0]
        else:
            raise last_extract_error or RuntimeError("No playable search candidates found")
    return info


def _clear_job_dir(job_dir: Path) -> None:
    for leftover in job_dir.iterdir():
        if leftover.is_file():
            leftover.unlink(missing_ok=True)


def _native_source_label(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    if host == "soundcloud.com" or host.endswith(".soundcloud.com"):
        return "soundcloud"
    if "bandcamp.com" in host:
        return "bandcamp"
    if "hearthis.at" in host:
        return "hearthis"
    return "youtube"


def download_multi_source(
    job_dir: Path, job_id: str, native_url: str, artist: str | None, title: str | None,
    duration: int | None, quality: str, settings, started: float, *, proxy: str | None = None,
    raw_title: str | None = None, catalog_no: str | None = None, label: str | None = None,
) -> tuple[dict[str, Any], str]:
    """SoundCloud first (only if it's a confident match), native source (or YouTube search) last."""
    search_queries = build_search_queries(artist, title, raw_title=raw_title, catalog_no=catalog_no, label=label)
    is_search_url = "/search" in native_url or "scsearch" in native_url or "search_query" in native_url

    # 1. Explicit YouTube URL identifies the exact requested recording
    if is_youtube_url(native_url) and not is_search_url:
        try:
            info = attempt_download(job_dir, native_url, quality, settings, started, proxy=proxy)
            logger.info("download source scelta job_id=%s source=youtube (exact url)", job_id)
            return info, "youtube"
        except Exception as exact_error:
            if any(message in str(exact_error) for message in DOWNLOAD_ABORT_MESSAGES):
                raise
            if not youtube_error_allows_fallback(exact_error):
                raise
            _clear_job_dir(job_dir)
            # Try unauthenticated direct fallback with bgutil PO token before SoundCloud
            try:
                info = attempt_download(job_dir, native_url, quality, settings, started, proxy=proxy, force_unauth=True)
                logger.info("download source scelta job_id=%s source=youtube (unauthenticated direct fallback)", job_id)
                return info, "youtube"
            except Exception as unauth_exact_err:
                logger.info("youtube exact unauthenticated attempt failed job_id=%s detail=%r", job_id, str(unauth_exact_err)[:200])
                _clear_job_dir(job_dir)

            # Pre-resolve track metadata via lightweight oEmbed if artist/title/raw_title is missing
            ref_artist, ref_title, ref_raw_title, ref_duration = artist, title, raw_title, duration
            if not ref_title and not ref_raw_title:
                try:
                    from media_core import _oembed, parse_artist_title
                    oe_data = _oembed("https://www.youtube.com/oembed", native_url)
                    if oe_data and oe_data.get("title"):
                        ref_raw_title = oe_data["title"]
                        parsed_a, parsed_t = parse_artist_title(oe_data["title"], fallback_artist=oe_data.get("author_name"))
                        ref_artist = ref_artist or parsed_a
                        ref_title = ref_title or parsed_t
                except Exception:
                    pass

            if not ref_title and not ref_raw_title:
                try:
                    from media_core import resolve_track
                    info_dict = resolve_track(native_url)
                    ref_artist = ref_artist or info_dict.get("artist")
                    ref_title = ref_title or info_dict.get("title")
                    ref_raw_title = ref_raw_title or info_dict.get("raw_title")
                    ref_duration = ref_duration or info_dict.get("duration")
                except Exception:
                    pass

            # Try strict SoundCloud match first with resolved metadata
            match_url = find_soundcloud_match(
                ref_artist, ref_title, ref_duration, raw_title=ref_raw_title, catalog_no=catalog_no, strict=True, label=label, proxy=proxy,
            )
            if match_url:
                try:
                    info = attempt_download(job_dir, match_url, quality, settings, started)
                    accepted, score, reason = strict_candidate_match(
                        ref_artist, ref_title, ref_duration, info, catalog_no=catalog_no, label=label,
                    )
                    if accepted:
                        logger.info("download source scelta job_id=%s source=soundcloud (strict fallback)", job_id)
                        return info, "soundcloud"
                    _clear_job_dir(job_dir)
                except Exception as fallback_error:
                    logger.info("youtube exact strict fallback failed job_id=%s detail=%r", job_id, str(fallback_error)[:200])
                    _clear_job_dir(job_dir)

            # 4. Try non-strict SoundCloud match (searches & scores candidates)
            match_url_nonstrict = find_soundcloud_match(
                ref_artist, ref_title, ref_duration, raw_title=ref_raw_title, catalog_no=catalog_no, strict=False, label=label, proxy=proxy,
            )
            if match_url_nonstrict:
                try:
                    info = attempt_download(job_dir, match_url_nonstrict, quality, settings, started)
                    logger.info("download source scelta job_id=%s source=soundcloud (non-strict fallback url=%s)", job_id, match_url_nonstrict)
                    return info, "soundcloud"
                except Exception as sc_fallback_err:
                    logger.warning("soundcloud non-strict fallback failed job_id=%s detail=%r", job_id, str(sc_fallback_err)[:200])
                    _clear_job_dir(job_dir)

            # 5. Fallback scsearch on primary query
            search_query = f"{ref_artist or ''} {ref_title or ''}".strip()
            if search_query:
                try:
                    info = attempt_download(job_dir, f"scsearch5:{search_query}", quality, settings, started)
                    logger.info("download source scelta job_id=%s source=soundcloud (scsearch fallback query=%r)", job_id, search_query)
                    return info, "soundcloud"
                except Exception as sc_exc:
                    logger.warning("scsearch fallback failed job_id=%s query=%r detail=%r", job_id, search_query, str(sc_exc)[:200])
                    _clear_job_dir(job_dir)
            # 6. Direct unauthenticated Android fallback
            # If authenticated/desktop client tripped bot-check or cookies were rejected by YouTube,
            # retry native YouTube URL using the unauthenticated android client which bypasses bot-check on datacenter IPs.
            try:
                info = attempt_download(job_dir, native_url, quality, settings, started, proxy=proxy, force_unauth=True)
                logger.info("download source scelta job_id=%s source=youtube (unauthenticated android fallback)", job_id)
                return info, "youtube"
            except Exception as unauth_err:
                logger.warning("unauthenticated android fallback failed job_id=%s detail=%r", job_id, str(unauth_err)[:200])
                _clear_job_dir(job_dir)

            raise exact_error

    # 2. SoundCloud match attempt (if not exact YouTube URL)
    match_url = find_soundcloud_match(artist, title, duration, raw_title=raw_title, catalog_no=catalog_no, label=label, proxy=proxy)
    if match_url:
        try:
            info = attempt_download(job_dir, match_url, quality, settings, started)
            logger.info("download source scelta job_id=%s source=soundcloud", job_id)
            return info, "soundcloud"
        except Exception as exc:
            logger.info("download fallback job_id=%s motivo=soundcloud_fallito detail=%r", job_id, str(exc)[:200])
            _clear_job_dir(job_dir)
    else:
        logger.info("download fallback job_id=%s motivo=nessun_match_soundcloud", job_id)

    # 3. Direct native URL attempt (ONLY if native_url is a direct media link, NOT a search webpage)
    lbl = _native_source_label(native_url)

    if not is_search_url:
        try:
            info = attempt_download(job_dir, native_url, quality, settings, started, proxy=proxy)
            logger.info("download source scelta job_id=%s source=%s", job_id, lbl)
            return info, lbl
        except Exception as exc:
            logger.info("download fallback attempting ytsearch cascade after native fail job_id=%s detail=%r", job_id, str(exc)[:200])
            _clear_job_dir(job_dir)

    # 4. YouTube search query cascade (for search URLs or when direct download failed)
    last_search_exc: Exception | None = None
    for q in search_queries:
        try:
            info = attempt_download(job_dir, f"ytsearch5:{q}", quality, settings, started, proxy=proxy)
            logger.info("download source scelta job_id=%s source=youtube (via ytsearch cascade query=%r)", job_id, q)
            return info, "youtube"
        except Exception as exc:
            last_search_exc = exc
            logger.info("ytsearch candidate query failed job_id=%s query=%r detail=%r", job_id, q, str(exc)[:200])
            _clear_job_dir(job_dir)

    raise last_search_exc or RuntimeError("All download candidates failed")
