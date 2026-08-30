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


def attempt_download(job_dir: Path, url: str, quality: str, settings, started: float, *, proxy: str | None = None) -> dict[str, Any]:
    """Run yt-dlp against a single candidate url, with the existing retry/limit behavior. Raises on total failure."""

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

    options = {
        # Prefer a native MP3 stream (SoundCloud serves http_mp3/hls_mp3) so
        # FFmpegExtractAudio remuxes with -c copy instead of re-encoding. On
        # Render's 0.1-vCPU free tier a full AAC->MP3 transcode of one track
        # costs ~50s; copying the already-MP3 stream is near-instant and avoids
        # a second lossy pass. Falls back to bestaudio (e.g. YouTube = Opus/AAC),
        # which still re-encodes because no MP3 source exists there.
        "format": "bestaudio[acodec=mp3][protocol^=http]/bestaudio[acodec=mp3]/bestaudio/best",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": AUDIO_QUALITY[quality]}],
        "outtmpl": str(job_dir / "source.%(ext)s"),
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "max_filesize": settings.max_file_bytes,
        "match_filter": duration_filter,
        "socket_timeout": 15,
        "concurrent_fragment_downloads": 4,
        "progress_hooks": [progress],
        "extractor_args": ytdlp_extractor_args(),
    }
    cookies = ytdlp_cookiefile()
    if cookies:
        options["cookiefile"] = cookies
    if proxy:
        options["proxy"] = proxy

    info = None
    last_extract_error: yt_dlp.utils.DownloadError | None = None
    current_options = dict(options)

    for attempt in range(1, 4):
        try:
            with YTDLP_LOCK, yt_dlp.YoutubeDL(current_options) as ydl:
                info = ydl.extract_info(url, download=True)
            break
        except yt_dlp.utils.DownloadError as exc:
            last_extract_error = exc
            for leftover in job_dir.iterdir():
                if leftover.is_file():
                    leftover.unlink(missing_ok=True)
            exc_str = str(exc).lower()
            # Se il proxy è scaduto (402 Payment Required), non autenticato (407) o fallito (tunnel/proxy error),
            # rimuovi immediatamente il proxy e riprova con connessione diretta!
            if "proxy" in exc_str or "tunnel" in exc_str or "402" in exc_str or "407" in exc_str:
                logger.warning("Proxy error detected (%r), dropping proxy for direct connection fallback", str(exc)[:150])
                current_options.pop("proxy", None)
            if str(exc) in DOWNLOAD_ABORT_MESSAGES or attempt == 3:
                # Se è l'ultimo tentativo ed era fallito con proxy, fai un ultimo tentativo disperato diretto
                if "proxy" in current_options:
                    try:
                        current_options.pop("proxy", None)
                        with YTDLP_LOCK, yt_dlp.YoutubeDL(current_options) as ydl:
                            info = ydl.extract_info(url, download=True)
                        break
                    except Exception:
                        pass
                raise
            logger.warning("download retrying attempt=%s error=%r", attempt, str(exc)[:150])
            time.sleep(1)
    if info is None:
        raise last_extract_error
    return info


def _clear_job_dir(job_dir: Path) -> None:
    for leftover in job_dir.iterdir():
        if leftover.is_file():
            leftover.unlink(missing_ok=True)


def _native_source_label(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return "soundcloud" if host == "soundcloud.com" or host.endswith(".soundcloud.com") else "youtube"


def download_multi_source(
    job_dir: Path, job_id: str, native_url: str, artist: str | None, title: str | None,
    duration: int | None, quality: str, settings, started: float, *, proxy: str | None = None,
    raw_title: str | None = None, catalog_no: str | None = None,
) -> tuple[dict[str, Any], str]:
    """SoundCloud first (only if it's a confident match), native source (or YouTube search) last."""
    # An explicit YouTube URL identifies the exact recording requested by the
    # user. Metadata recognition may label that recording, but must never turn
    # it into a search query or substitute a similarly named SoundCloud track.
    if is_youtube_url(native_url):
        try:
            info = attempt_download(job_dir, native_url, quality, settings, started, proxy=proxy)
            logger.info("download source scelta job_id=%s source=youtube (exact url)", job_id)
            return info, "youtube"
        except Exception as yt_exc:
            logger.warning("direct youtube download failed job_id=%s detail=%r, attempting resilient SoundCloud fallback", job_id, str(yt_exc)[:200])
            _clear_job_dir(job_dir)
            match_url = find_soundcloud_match(artist, title, duration, raw_title=raw_title, catalog_no=catalog_no)
            if match_url:
                try:
                    info = attempt_download(job_dir, match_url, quality, settings, started)
                    logger.info("download source fallback riuscito job_id=%s source=soundcloud", job_id)
                    return info, "soundcloud"
                except Exception:
                    _clear_job_dir(job_dir)
            query_str = f"{artist or ''} {title or raw_title or ''}".strip()
            if query_str:
                try:
                    info = attempt_download(job_dir, f"ytsearch1:{query_str}", quality, settings, started, proxy=proxy)
                    logger.info("download source fallback ytsearch riuscito job_id=%s", job_id)
                    return info, "youtube"
                except Exception:
                    _clear_job_dir(job_dir)
            raise yt_exc

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

    # If native_url is a search URL (e.g. soundcloud search) or generic query, use ytsearch fallback
    is_search_url = "/search" in native_url or "scsearch" in native_url or "search_query" in native_url
    query_str = f"{artist or ''} {title or raw_title or ''}".strip()
    if is_search_url and query_str:
        yt_query_url = f"ytsearch1:{query_str}"
        try:
            info = attempt_download(job_dir, yt_query_url, quality, settings, started, proxy=proxy)
            logger.info("download source scelta job_id=%s source=youtube (via ytsearch)", job_id)
            return info, "youtube"
        except Exception as exc:
            logger.info("download fallback ytsearch fallito job_id=%s detail=%r", job_id, str(exc)[:200])
            _clear_job_dir(job_dir)

    label = _native_source_label(native_url)
    try:
        info = attempt_download(job_dir, native_url, quality, settings, started, proxy=proxy)
        logger.info("download source scelta job_id=%s source=%s", job_id, label)
        return info, label
    except Exception as exc:
        if query_str and not is_search_url:
            logger.info("download fallback attempting ytsearch after native fail job_id=%s", job_id)
            _clear_job_dir(job_dir)
            info = attempt_download(job_dir, f"ytsearch1:{query_str}", quality, settings, started, proxy=proxy)
            logger.info("download source scelta job_id=%s source=youtube", job_id)
            return info, "youtube"
        raise exc
