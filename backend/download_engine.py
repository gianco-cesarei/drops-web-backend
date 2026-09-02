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

from media_core import YTDLP_LOCK, is_youtube_url, strip_noise, ytdlp_cookiefile, ytdlp_extractor_args

logger = logging.getLogger("drops.download")

AUDIO_QUALITY = {"128": "128", "192": "192", "320": "0", "mp3": "0", "hq": "0"}

# Our own abort messages (progress hook / duration check) - never retryable,
# retrying an oversized/too-long media just repeats the same failure.
DOWNLOAD_ABORT_MESSAGES = {
    "Download duration limit exceeded",
    "Download size limit exceeded",
    "Media duration limit exceeded",
}

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
    norm = _normalize(value)
    return re.sub(r"\b(rework|edit|dub|vip|version|club mix|extended mix|original mix|original)\b", "remix", norm)


def score_candidate(
    artist: str | None,
    title: str | None,
    entry: dict[str, Any],
    *,
    catalog_no: str | None = None,
) -> float:
    candidate_title = str(entry.get("title") or "")
    candidate_uploader = str(entry.get("uploader") or "")
    candidate_combined = f"{candidate_title} {candidate_uploader}".strip()

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
    if catalog_no:
        cat_clean = _normalize(catalog_no)
        if cat_clean and cat_clean in _normalize(candidate_combined):
            scores.append(0.85)
    return max(scores)


def find_soundcloud_match(
    artist: str | None,
    title: str | None,
    duration: int | None,
    raw_title: str | None = None,
    catalog_no: str | None = None,
) -> str | None:
    """Search SoundCloud for a track matching artist+title/raw_title, gated by duration when known."""
    if not artist and not title and not raw_title and not catalog_no:
        return None

    queries: list[str] = []
    seen_q: set[str] = set()
    for candidate_q in [
        f"{artist} {title}".strip() if artist and title else None,
        title.strip() if title else None,
        strip_noise(raw_title) if raw_title else None,
        f"{artist} {title} {catalog_no}".strip() if artist and title and catalog_no else None,
        f"{catalog_no} {title}".strip() if catalog_no and title else None,
        catalog_no.strip() if catalog_no else None,
    ]:
        if candidate_q:
            norm_q = _normalize(candidate_q)
            if norm_q and norm_q not in seen_q:
                seen_q.add(norm_q)
                queries.append(candidate_q)

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
            if duration is not None:
                if cand_duration is None or abs(cand_duration - duration) > 3:
                    continue
            score = score_candidate(artist, title, entry, catalog_no=catalog_no)
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
                score = score_candidate(artist, title, entry, catalog_no=catalog_no)
                scored_candidates.append((url, cand_title, cand_duration, score, "duration_mismatch"))
                continue

        score = score_candidate(artist, title, entry, catalog_no=catalog_no)

        # Threshold rules:
        # If duration close (within ±5s): accept score >= 0.4
        # If duration known (within ±15s): accept score >= 0.5
        # If duration unknown: accept score >= 0.55
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
) -> list[str]:
    """Build an ordered list of search queries from most specific/clean to broader fallbacks."""
    queries: list[str] = []
    seen: set[str] = set()

    clean_artist = re.sub(r"\s*-\s*Topic\b", "", artist or "", flags=re.IGNORECASE).strip() or None
    clean_artist = re.sub(r"\s*\((?:Topic|Official)\)\s*", "", clean_artist or "", flags=re.IGNORECASE).strip() or None
    clean_title = strip_noise(title) if title else None
    clean_raw = strip_noise(raw_title) if raw_title else None

    candidates = [
        f"{clean_artist} {clean_title}".strip() if clean_artist and clean_title else None,
        f"{artist} {title}".strip() if artist and title else None,
        f"{clean_artist} {title}".strip() if clean_artist and title else None,
        clean_title.strip() if clean_title else None,
        title.strip() if title else None,
        clean_raw.strip() if clean_raw else None,
        f"{clean_artist} {clean_title} audio".strip() if clean_artist and clean_title else None,
        f"{catalog_no} {clean_title}".strip() if catalog_no and clean_title else None,
    ]

    for cand in candidates:
        if cand:
            norm = _normalize(cand)
            if norm and norm not in seen:
                seen.add(norm)
                queries.append(cand)

    return queries


def attempt_download(job_dir: Path, url: str, quality: str, settings, started: float, *, proxy: str | None = None) -> dict[str, Any]:
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

    options = {
        # Prefer a native MP3 stream (SoundCloud serves http_mp3/hls_mp3) so
        # FFmpegExtractAudio remuxes with -c copy instead of re-encoding.
        "format": "bestaudio[acodec=mp3][protocol^=http]/bestaudio[acodec=mp3]/bestaudio/best",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": AUDIO_QUALITY[quality]}],
        "outtmpl": str(job_dir / "source.%(ext)s"),
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "max_filesize": settings.max_file_bytes,
        "match_filter": duration_filter,
        "socket_timeout": 15,
        "nocheckcertificate": True,
        "concurrent_fragment_downloads": 4,
        "progress_hooks": [progress],
        "extractor_args": ytdlp_extractor_args(),
    }
    cookies = ytdlp_cookiefile()
    if cookies:
        options["cookiefile"] = cookies
    if proxy:
        options["proxy"] = proxy

    CLIENT_TIERS = [
        ["mweb", "android"],
        ["android", "ios"],
        ["tv_embedded", "tv"],
        ["web_embedded", "android"],
    ]

    info = None
    last_extract_error: Exception | None = None
    current_options = dict(options)

    for attempt in range(1, 5):
        client_tier = CLIENT_TIERS[min(attempt - 1, len(CLIENT_TIERS) - 1)]
        ext_args = dict(current_options.get("extractor_args") or {})
        yt_args = dict(ext_args.get("youtube") or {})
        yt_args["player_client"] = client_tier
        ext_args["youtube"] = yt_args
        current_options["extractor_args"] = ext_args

        # Format fallback on later attempts if rigid format fails
        if attempt >= 3:
            current_options["format"] = "bestaudio/best"

        try:
            with YTDLP_LOCK, yt_dlp.YoutubeDL(current_options) as ydl:
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
            ))
            if "proxy" in current_options and proxy_failure:
                logger.warning("Proxy error/bot detected (%r), dropping proxy for direct fallback", str(exc)[:150])
                current_options.pop("proxy", None)
            if str(exc) in DOWNLOAD_ABORT_MESSAGES or attempt == 4:
                raise
            logger.warning("download retrying attempt=%s client_tier=%s error=%r", attempt, client_tier, str(exc)[:150])
            time.sleep(attempt * 0.5)
    if info is None:
        raise last_extract_error or RuntimeError("Download failed")
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
    raw_title: str | None = None, catalog_no: str | None = None,
) -> tuple[dict[str, Any], str]:
    """SoundCloud first (only if it's a confident match), native source (or YouTube search) last."""
    search_queries = build_search_queries(artist, title, raw_title=raw_title, catalog_no=catalog_no)

    # 1. Explicit YouTube URL identifies the exact requested recording
    if is_youtube_url(native_url):
        try:
            info = attempt_download(job_dir, native_url, quality, settings, started, proxy=proxy)
            logger.info("download source scelta job_id=%s source=youtube (exact url)", job_id)
            return info, "youtube"
        except Exception as yt_exc:
            logger.warning("direct youtube download failed job_id=%s detail=%r, attempting resilient SoundCloud/search fallback", job_id, str(yt_exc)[:200])
            _clear_job_dir(job_dir)
            clean_artist = re.sub(r"\s*-\s*Topic\b", "", artist or "", flags=re.IGNORECASE).strip() or None
            match_url = find_soundcloud_match(clean_artist, title, duration, raw_title=raw_title, catalog_no=catalog_no)
            if match_url:
                try:
                    info = attempt_download(job_dir, match_url, quality, settings, started)
                    logger.info("download source fallback riuscito job_id=%s source=soundcloud", job_id)
                    return info, "soundcloud"
                except Exception:
                    _clear_job_dir(job_dir)
            # Fallback search query cascade on YouTube
            for q in search_queries:
                try:
                    info = attempt_download(job_dir, f"ytsearch5:{q}", quality, settings, started, proxy=None)
                    logger.info("download source fallback ytsearch riuscito job_id=%s query=%r", job_id, q)
                    return info, "youtube"
                except Exception:
                    _clear_job_dir(job_dir)
            raise yt_exc

    # 2. SoundCloud match attempt (if not exact YouTube URL)
    match_url = find_soundcloud_match(artist, title, duration, raw_title=raw_title, catalog_no=catalog_no)
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
    is_search_url = "/search" in native_url or "scsearch" in native_url or "search_query" in native_url
    label = _native_source_label(native_url)

    if not is_search_url:
        try:
            info = attempt_download(job_dir, native_url, quality, settings, started, proxy=proxy)
            logger.info("download source scelta job_id=%s source=%s", job_id, label)
            return info, label
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

