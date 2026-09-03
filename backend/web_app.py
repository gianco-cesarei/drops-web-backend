import io
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

import yt_dlp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel
from starlette.datastructures import MutableHeaders

from academy_store import STATUS_READY as ACADEMY_STATUS_READY, AcademyStore
from bpm_analyzer import BpmAnalysisError, analyze_bpm
from bpm_analyzer_async import analyze_r2_object_bpm_async
from bpm_jobs import BpmJobManager
from discogs_agent import DiscogsClient
from download_engine import AUDIO_QUALITY, attempt_download, download_multi_source
from folder_store import FolderStore
from media_core import (
    is_supported_url,
    is_youtube_url,
    public_ytdlp_error,
    resolve_track,
    resolve_track_oembed,
    safe_filename,
    tag_audio_file,
    ytdlp_cookiefile,
    ytdlp_extractor_args,
    ytdlp_proxy,
)
import r2_storage
from spotify_agent import SpotifyAgentError, WebSpotifyClient
from track_store import TrackStore
from web_settings import WebSettings
from web_store import WebStore

COOKIE_NAME = "drops_session"
logger = logging.getLogger("drops.web")


# --- Cross-origin audio serving helpers (Archivio Web Audio + zip export) -----
# The /file endpoint below must be consumable cross-origin so the front-end can
# run tracks through a Web Audio graph (EQ + master volume) and fetch each one
# to build a .zip. That needs: real Content-Type, byte-range/seek support (206 +
# Content-Range + Accept-Ranges), and CORS headers on every response. CORS is
# handled globally by CORSMiddleware (it wraps 200/206/416/OPTIONS alike and
# exposes Content-Range/Accept-Ranges/Content-Length); these helpers own the
# range + content-type + caching behaviour.

_AUDIO_CONTENT_TYPES = {
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/opus",
}

_BYTES_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _audio_content_type(filename: str | None) -> str:
    """Best-effort audio MIME from the filename extension (default audio/mpeg)."""
    if filename:
        ext = Path(filename).suffix.lower()
        if ext in _AUDIO_CONTENT_TYPES:
            return _AUDIO_CONTENT_TYPES[ext]
    return "audio/mpeg"


def _parse_range(range_header: str | None, file_size: int):
    """Return (start, end) inclusive for a single-range request, or None for a
    full-content response. Raises HTTPException(416) when unsatisfiable."""
    if not range_header:
        return None
    match = _BYTES_RANGE_RE.match(range_header.strip())
    if not match or not (match.group(1) or match.group(2)):
        # Malformed / multi-range: fall back to serving the whole file (200).
        return None
    start_s, end_s = match.groups()
    if start_s:
        start = int(start_s)
        end = int(end_s) if end_s else file_size - 1
    else:  # suffix range: last N bytes
        start = max(0, file_size - int(end_s))
        end = file_size - 1
    if start >= file_size or start > end:
        raise HTTPException(
            status_code=416,
            detail="Range not satisfiable",
            headers={"Content-Range": f"bytes */{file_size}", "Accept-Ranges": "bytes"},
        )
    return start, min(end, file_size - 1)


def _serve_local_file(path: Path, filename: str, content_type: str,
                      range_header: str | None, is_head: bool,
                      cache_control: str = "private, max-age=3600"):
    file_size = path.stat().st_size
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": cache_control,
        "Content-Disposition": f'inline; filename="{filename}"',
    }
    span = _parse_range(range_header, file_size)
    if span is None:
        start, end, status_code = 0, file_size - 1, 200
    else:
        start, end = span
        status_code = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    length = end - start + 1
    headers["Content-Length"] = str(length)
    if is_head:
        return Response(status_code=status_code, media_type=content_type, headers=headers)

    def iter_file(chunk_size: int = 64 * 1024):
        with open(path, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(chunk_size, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(iter_file(), status_code=status_code,
                             media_type=content_type, headers=headers)


def _serve_r2_file(r2_key: str, filename: str, content_type: str,
                   range_header: str | None, is_head: bool,
                   cache_control: str = "private, max-age=3600"):
    base_headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": cache_control,
        "Content-Disposition": f'inline; filename="{filename}"',
    }
    if is_head:
        try:
            meta = r2_storage.head_object(r2_key)
        except r2_storage.R2Error:
            raise HTTPException(status_code=404, detail="File not found")
        headers = {**base_headers, "Content-Length": str(meta.get("ContentLength", 0))}
        return Response(status_code=200, media_type=content_type, headers=headers)
    try:
        obj = r2_storage.get_object(r2_key, range_header=range_header)
    except r2_storage.R2InvalidRangeError as exc:
        headers = {"Accept-Ranges": "bytes"}
        if exc.total_size is not None:
            headers["Content-Range"] = f"bytes */{exc.total_size}"
        raise HTTPException(status_code=416, detail="Range not satisfiable", headers=headers) from exc
    except r2_storage.R2NotFoundError as exc:
        raise HTTPException(status_code=404, detail="File not found") from exc
    except r2_storage.R2Error as exc:
        raise HTTPException(status_code=502, detail="Could not stream file") from exc

    def iter_body(body, chunk_size: int = 64 * 1024):
        try:
            while True:
                chunk = body.read(chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            body.close()

    headers = {**base_headers, "Content-Length": str(obj["ContentLength"])}
    status_code = 200
    content_range = obj.get("ContentRange")
    if content_range:
        headers["Content-Range"] = content_range
        status_code = 206
    return StreamingResponse(iter_body(obj["Body"]), status_code=status_code,
                             media_type=content_type, headers=headers)


# Files that were downloaded and are sitting on the server must be consumable
# cross-origin from any front-end origin: single-file download, the "Scarica
# cartella" zip (a fetch() per track), and the Web Audio EQ/volume graph all
# read the bytes with crossOrigin="anonymous". Route 1 (signed token, no
# cookie) lets us answer with a plain wildcard. The global CORSMiddleware is
# credentialed (it echoes the exact origin), which can't be combined with a
# token-less public file, so this outer middleware force-sets a wildcard ONLY
# on the /file endpoint and strips the credentialed markers there.
_PUBLIC_FILE_RE = re.compile(r"^/api/v1/downloads/[^/]+/file$")
_PUBLIC_CORS_EXPOSE = "Content-Range, Accept-Ranges, Content-Length, Content-Type, Content-Disposition"


class PublicFileCORSMiddleware:
    """Pure-ASGI so it streams the audio body untouched (no buffering)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not _PUBLIC_FILE_RE.match(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        if scope.get("method") == "OPTIONS":
            # Answer the preflight ourselves with a wildcard (outermost layer,
            # so this runs before the credentialed CORSMiddleware).
            await send({
                "type": "http.response.start",
                "status": 204,
                "headers": [
                    (b"access-control-allow-origin", b"*"),
                    (b"access-control-allow-methods", b"GET, HEAD, OPTIONS"),
                    (b"access-control-allow-headers", b"Range, Content-Type"),
                    (b"access-control-max-age", b"600"),
                    (b"content-length", b"0"),
                ],
            })
            await send({"type": "http.response.body", "body": b""})
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message["headers"])
                headers["access-control-allow-origin"] = "*"
                headers["access-control-allow-methods"] = "GET, HEAD, OPTIONS"
                headers["access-control-allow-headers"] = "Range, Content-Type"
                headers["access-control-expose-headers"] = _PUBLIC_CORS_EXPOSE
                headers["access-control-max-age"] = "600"
                # A wildcard origin is invalid alongside credentials; drop the
                # credentialed markers the inner CORSMiddleware may have added.
                if "access-control-allow-credentials" in headers:
                    del headers["access-control-allow-credentials"]
                if "vary" in headers:
                    del headers["vary"]
            await send(message)

        await self.app(scope, receive, send_wrapper)


class LoginRequest(BaseModel):
    username: str
    password: str


class WebDownloadRequest(BaseModel):
    url: str
    quality: str = "320"
    artist: str | None = None
    title: str | None = None
    cover_url: str | None = None


class YoutubeDirectRequest(BaseModel):
    url: str
    # Optional metadata overrides; when omitted they are recognized from the
    # video via resolve_track / Discogs enrichment exactly like the main flow.
    artist: str | None = None
    title: str | None = None
    genre: str | None = None
    cover_url: str | None = None


class PlaylistResolveRequest(BaseModel):
    url: str


class DiscogsEnrichRequest(BaseModel):
    artist: str
    title: str
    isrc: str | None = None
    catalog_no: str | None = None
    barcode: str | None = None


class BpmComputeRequest(BaseModel):
    track_key: str | None = None
    artist: str
    title: str
    isrc: str | None = None
    source_url: str | None = None


# Academy feedback-track submissions accept the same WAV/MP3 mime types the
# frontend's file picker already validates client-side (AcademyHub.tsx) - the
# backend re-checks it since the client-side check is only a UX nicety.
ACADEMY_ALLOWED_CONTENT_TYPES = {"audio/wav", "audio/x-wav", "audio/mpeg"}


class AcademySubmissionPresignRequest(BaseModel):
    filename: str
    content_type: str
    size_bytes: int
    title: str | None = None
    bpm: float | None = None
    genre: str | None = None
    focus_area: str | None = None


class FolderCreateRequest(BaseModel):
    name: str
    dominant_genre: str | None = None
    track_ids: list[str] | None = None


class FolderRenameRequest(BaseModel):
    name: str


def _youtube_url_context(value: str) -> dict[str, str | None]:
    """Describe whether a YouTube URL identifies a track, playlist, or both."""
    if not is_youtube_url(value):
        return {
            "url_type": None,
            "playlist_id": None,
            "selected_track_id": None,
            "selected_track_url": None,
        }
    parsed = urllib.parse.urlsplit(value.strip())
    query = urllib.parse.parse_qs(parsed.query)
    playlist_id = (query.get("list") or [None])[0]
    selected_track_id = (query.get("v") or [None])[0]
    host = (parsed.hostname or "").lower().rstrip(".")
    path_parts = [part for part in parsed.path.split("/") if part]
    if not selected_track_id and host == "youtu.be" and path_parts:
        selected_track_id = path_parts[0]
    if not selected_track_id and len(path_parts) >= 2 and path_parts[0] in {"shorts", "embed"}:
        selected_track_id = path_parts[1]
    if playlist_id and selected_track_id:
        url_type = "track_in_playlist"
    elif playlist_id:
        url_type = "playlist"
    else:
        url_type = "track"
    selected_track_url = (
        f"https://www.youtube.com/watch?v={urllib.parse.quote(selected_track_id, safe='-_')}"
        if selected_track_id else None
    )
    return {
        "url_type": url_type,
        "playlist_id": playlist_id,
        "selected_track_id": selected_track_id,
        "selected_track_url": selected_track_url,
    }


def _playlist_entry_url(entry: dict, original_url: str) -> str | None:
    for key in ("webpage_url", "original_url", "url"):
        value = entry.get(key)
        if isinstance(value, str) and is_supported_url(value):
            return value
    extractor = str(entry.get("extractor_key") or entry.get("ie_key") or "").lower()
    video_id = entry.get("id")
    if video_id and "youtube" in extractor:
        return f"https://www.youtube.com/watch?v={video_id}"
    if isinstance(video_id, str) and video_id.startswith("http"):
        return video_id
    return original_url if is_supported_url(original_url) else None


def _clean_entry_title(entry: dict, entry_url: str | None) -> str:
    title = entry.get("title")
    if title and str(title).strip() and str(title).strip().lower() not in ("none", "null", ""):
        return str(title).strip()
    if entry_url:
        path = urllib.parse.urlsplit(entry_url).path.strip("/").split("/")[-1]
        if path:
            return path.replace("-", " ").replace("_", " ").title()
    return "Senza titolo"


def create_app(settings: WebSettings | None = None) -> FastAPI:
    settings = settings or WebSettings.from_env()
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    jobs_dir = settings.state_dir / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    store = WebStore(settings.state_dir / "web.sqlite3")
    # Durable cloud catalog (Supabase Postgres in prod, SQLite fallback in dev).
    tracks = TrackStore(settings.state_dir, settings.database_url)
    academy = AcademyStore(settings.state_dir, settings.database_url)
    folders = FolderStore(settings.state_dir, settings.database_url)
    discogs = DiscogsClient(settings.state_dir)
    spotify = WebSpotifyClient(settings.state_dir, discogs=discogs)
    bpm_jobs = BpmJobManager(settings.state_dir, max_workers=min(2, settings.max_concurrent))
    password_hasher = PasswordHasher()
    session_serializer = URLSafeTimedSerializer(settings.session_secret, salt="drops-web-session")

    def issue_session_token(owner: str) -> str:
        return session_serializer.dumps({"username": owner, "iat": int(time.time())})

    # Separate salt so a file-access token can never be replayed as a
    # session cookie (or vice-versa) even though both are signed with the
    # same secret. Short TTL (settings.file_token_ttl_seconds, ~5 min).
    file_token_serializer = URLSafeTimedSerializer(settings.session_secret, salt="drops-web-file-access")

    def issue_file_token(job_id: str, owner: str) -> str:
        return file_token_serializer.dumps({"job_id": job_id, "owner": owner})

    _last_cleanup = 0.0

    def cleanup(force: bool = False) -> None:
        nonlocal _last_cleanup
        now = time.time()
        if not force and now - _last_cleanup < 10:
            return
        _last_cleanup = now
        for row in store.expired_artifacts():
            job_dir = (jobs_dir / str(row["id"])).resolve()
            if job_dir.parent == jobs_dir.resolve():
                shutil.rmtree(job_dir, ignore_errors=True)
            else:
                logger.error("refused unsafe cleanup record")
        store.delete_expired()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        discogs.log_startup_status()
        if os.environ.get("DROPS_YTDLP_COOKIES", "").strip():
            cookiefile = ytdlp_cookiefile()
            if cookiefile:
                # Age is a proxy for freshness, not proof of validity - YouTube
                # sessions can expire well before this looks old. A cookiefile
                # older than a couple of weeks is worth re-exporting.
                age_days = (time.time() - os.path.getmtime(cookiefile)) / 86400
                logger.info("yt-dlp startup: cookiefile trovato (eta' %.1f giorni)", age_days)
            else:
                logger.info("yt-dlp startup: cookiefile configurato ma illeggibile, ignorato")
        else:
            logger.info("yt-dlp startup: DROPS_YTDLP_COOKIES non configurato, download senza cookie")
        store.interrupt_active_jobs(settings.artifact_ttl_seconds)
        cleanup()
        executor = ThreadPoolExecutor(max_workers=settings.max_concurrent, thread_name_prefix="drops-web")
        app.state.executor = executor
        bpm_jobs.bind_executor(executor)
        try:
            yield
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
            cleanup()

    app = FastAPI(title="Drops Web API", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.tracks = tracks
    app.state.academy = academy
    app.state.folders = folders
    app.state.discogs = discogs
    app.state.executor = None

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_credentials=True,
        # HEAD added for the Academy streaming endpoint's range-probe request;
        # Range/Content-Range so the DJ Lab deck and the global Mini-Player can
        # seek via fetch() across origins and read those response headers.
        # PATCH/DELETE added for folder rename and delete.
        allow_methods=["GET", "POST", "HEAD", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "Range"],
        expose_headers=["Content-Range", "Accept-Ranges", "Content-Length"],
    )

    # Outermost layer: forces a wildcard CORS on the public /file endpoint
    # (must be added AFTER CORSMiddleware to wrap it).
    app.add_middleware(PublicFileCORSMiddleware)

    def require_csrf_origin(request: Request) -> None:
        origin = request.headers.get("origin")
        if origin is None:
            if settings.allow_missing_origin:
                return
            raise HTTPException(status_code=403, detail="Origin required")
        if origin not in settings.allowed_origins:
            raise HTTPException(status_code=403, detail="Origin not allowed")

    def verify_session(drops_session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> str:
        # Sessions are a signed, stateless token (no disk/DB lookup): Render's
        # free tier wipes DROPS_WEB_STATE_DIR (an ephemeral /tmp) on every
        # redeploy, which used to invalidate every session instantly.
        if not drops_session:
            cleanup(force=True)
            logger.info("auth rejected reason=cookie_missing")
            raise HTTPException(status_code=401, detail="Authentication required")
        try:
            payload = session_serializer.loads(drops_session, max_age=settings.session_ttl_seconds)
        except SignatureExpired:
            cleanup(force=True)
            logger.info("auth rejected reason=cookie_expired")
            raise HTTPException(status_code=401, detail="Invalid or expired session")
        except BadSignature:
            cleanup(force=True)
            logger.info("auth rejected reason=cookie_invalid")
            raise HTTPException(status_code=401, detail="Invalid or expired session")
        owner = payload.get("username")
        if not owner:
            cleanup(force=True)
            logger.info("auth rejected reason=cookie_invalid")
            raise HTTPException(status_code=401, detail="Invalid or expired session")
        cleanup(force=True)
        return owner

    def current_owner(response: Response, owner: str = Depends(verify_session)) -> str:
        # Sliding session: reissue with a fresh timestamp on every authenticated
        # call so an active user never gets logged out mid-session, only after
        # real inactivity.
        response.set_cookie(
            COOKIE_NAME, issue_session_token(owner), max_age=settings.session_ttl_seconds,
            httponly=True, secure=settings.cookie_secure, samesite="lax", path="/",
        )
        return owner

    def public_job(row) -> dict:
        result = {
            "id": row["id"],
            "status": row["status"],
            "format": row["format"],
            "quality": row["quality"],
            "created_at": row["created_at"],
        }
        for key in (
            "title", "artist", "cover_url", "raw_title", "duration",
            "label", "year", "country", "catalog_no", "discogs_url",
            "bpm", "bpm_confidence", "source",
            "filename", "size", "error",
            "track_id", "r2_key",
        ):
            if row[key] is not None:
                result[key] = row[key]
        if row["style"]:
            result["style"] = json.loads(row["style"])
        if row["status"] == "ready":
            # Local file endpoint only when the artifact is still on disk. The
            # cloud (youtube-direct) flow deletes the local file after uploading
            # to R2, so it exposes a stream_url instead of a file_url.
            if row["file_path"]:
                result["file_url"] = f"/api/v1/downloads/{row['id']}/file"
            if row["track_id"]:
                result["stream_url"] = f"/api/stream/{row['track_id']}"
        return result

    def promote_to_cloud(owner, artifact, genre_str, artist, title):
        """Upload a finished MP3 to R2 and write its durable catalog row.

        Best-effort by design: returns (track_id, r2_key) on success, or
        (None, None) when R2 is not configured or the upload / DB write fails -
        in which case the caller keeps serving the local file and simply skips
        cloud promotion, so a transient R2/Supabase outage never fails a
        download. Shared by both the multi-source flow and youtube-direct.
        """
        if not r2_storage.is_configured():
            return None, None
        try:
            r2_key = r2_storage.build_object_key(owner, genre_str, artist, title)
            r2_storage.upload_file(str(artifact), r2_key)
        except r2_storage.R2Error as exc:
            logger.warning("r2 upload failed, keeping local owner=%s detail=%r", owner, str(exc)[:120])
            return None, None
        try:
            track = tracks.create_track(
                user_id=owner, r2_key=r2_key, artist=artist,
                title=title, genre=genre_str, bpm=None,
            )
        except Exception as exc:
            logger.warning("track catalog insert failed detail=%r", str(exc)[:150])
            # Avoid an orphan object in R2 with no catalog row pointing at it.
            r2_storage.delete_object(r2_key)
            return None, None
        return track["track_id"], r2_key

    def process_job(job_id: str, url: str, quality: str) -> None:
        job_dir = jobs_dir / job_id
        job_dir.mkdir(mode=0o700)
        started = time.monotonic()
        row = store.get_job_by_id(job_id)
        owner = row["owner"] if row else None
        artist = row["artist"] if row else None
        title = row["title"] if row else None
        duration = row["duration"] if row else None
        raw_title = row["raw_title"] if row else None

        if not (artist and title):
            try:
                bg_rec = resolve_track(url)
                if bg_rec:
                    artist = artist or bg_rec.get("artist")
                    title = title or bg_rec.get("title")
                    raw_title = raw_title or bg_rec.get("raw_title")
                    duration = duration or bg_rec.get("duration")
                    cover_url = (row["cover_url"] if (row and "cover_url" in row.keys() and row["cover_url"]) else None) or bg_rec.get("cover_url")
                    store.update_job(
                        job_id,
                        artist=artist,
                        title=title,
                        raw_title=raw_title,
                        duration=duration,
                        cover_url=cover_url,
                    )
            except Exception as exc:
                logger.info("background resolve_track skip job_id=%s detail=%r", job_id, str(exc)[:200])

        catalog_no = None
        enrichment = None
        if artist and title:
            store.update_job(job_id, status="enriching")
            try:
                enrichment = discogs.enrich(artist, title)
            except Exception as exc:
                logger.info("discogs enrich skip job_id=%s detail=%r", job_id, str(exc)[:200])
                enrichment = None
            if enrichment:
                catalog_no = enrichment.get("catalog_no")
                update = {
                    "label": enrichment.get("label"),
                    "year": enrichment.get("year"),
                    "country": enrichment.get("country"),
                    "catalog_no": catalog_no,
                    "style": json.dumps(enrichment.get("styles") or []),
                    "discogs_url": enrichment.get("discogs_url"),
                }
                if enrichment.get("cover_url"):
                    update["cover_url"] = enrichment["cover_url"]
                store.update_job(job_id, **update)

        t_after_enrich = time.monotonic()
        try:
            store.update_job(job_id, status="downloading")
            info, source = download_multi_source(
                job_dir, job_id, url, artist, title, duration, quality, settings, started,
                proxy=ytdlp_proxy(), raw_title=raw_title, catalog_no=catalog_no,
            )
            t_after_dl = time.monotonic()
            if int(info.get("duration") or 0) > settings.max_duration_seconds:
                raise yt_dlp.utils.DownloadError("Media duration limit exceeded")
            audio_candidates = [
                path for path in job_dir.iterdir()
                if path.is_file() and not path.name.endswith((".part", ".ytdl")) and path.suffix.lower() in {".mp3", ".m4a", ".flac", ".wav", ".aac", ".ogg", ".webm", ".opus", ".mp4"}
            ]
            if not audio_candidates:
                audio_candidates = [path for path in job_dir.iterdir() if path.is_file() and not path.name.endswith((".part", ".ytdl"))]
            if not audio_candidates:
                raise RuntimeError("Downloaded artifact missing")
            source_file = sorted(audio_candidates, key=lambda p: (p.suffix.lower() == ".mp3", p.stat().st_size), reverse=True)[0]
            if source_file.stat().st_size > settings.max_file_bytes:
                raise yt_dlp.utils.DownloadError("Download size limit exceeded")
            filename = safe_filename(str(info.get("title") or title or "audio"), "mp3")
            artifact = job_dir / filename
            if source_file != artifact:
                source_file.replace(artifact)
            # Cover art + ID3 tags are written now (without BPM) so the file is
            # immediately downloadable; BPM is analyzed off the critical path below.
            cover_data = None
            row_cover = row["cover_url"] if (row is not None and "cover_url" in row.keys()) else None
            cover_url_to_fetch = (enrichment.get("cover_url") if enrichment else None) or row_cover
            if cover_url_to_fetch:
                try:
                    req = urllib.request.Request(cover_url_to_fetch, headers={"User-Agent": "Drops/1.0"})
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        cover_data = resp.read()
                except Exception as c_exc:
                    logger.info("cover download skip detail=%r", str(c_exc)[:100])

            genre_str = None
            if enrichment and enrichment.get("styles"):
                genre_str = ", ".join(enrichment["styles"][:3])

            final_title = str(info.get("title") or title or "audio")[:200]
            tag_label = enrichment.get("label") if enrichment else None
            tag_year = enrichment.get("year") if enrichment else None

            try:
                tag_audio_file(
                    artifact,
                    title=final_title,
                    artist=artist,
                    label=tag_label,
                    year=tag_year,
                    genre=genre_str,
                    bpm=None,
                    cover_data=cover_data,
                )
            except Exception as tag_exc:
                logger.info("id3 tagging skip detail=%r", str(tag_exc)[:100])

            # Cloud durability (Sezione 3): promote to R2 + Supabase catalog so
            # the track survives Render's ephemeral disk being wiped on restart -
            # important for downloads coming from Spotify/SoundCloud likes. Unlike
            # youtube-direct we KEEP the local file so /file and the zip export
            # keep working for the rest of the session; the TTL cleanup removes it
            # later. R2 object carries base tags (no BPM); the DB is the source of
            # truth for BPM.
            track_id, r2_key = promote_to_cloud(owner, artifact, genre_str, artist, final_title)

            # Mark ready immediately: the card lands in "scaricati" and the file is
            # downloadable without waiting for BPM analysis.
            store.update_job(
                job_id,
                status="ready",
                title=final_title,
                filename=filename,
                file_path=str(artifact),
                size=artifact.stat().st_size,
                source=source,
                duration=int(info.get("duration") or 0) or None,
                track_id=track_id,
                r2_key=r2_key,
                expires_at=time.time() + settings.artifact_ttl_seconds,
            )

            t_ready = time.monotonic()
            logger.info(
                "process_job timings job_id=%s source=%s enrich=%.1fs multi_source=%.1fs finalize=%.1fs total=%.1fs",
                job_id, source, t_after_enrich - started, t_after_dl - t_after_enrich,
                t_ready - t_after_dl, t_ready - started,
            )

            # BPM off the critical path: analyze in a daemon thread, then patch the
            # job row, the cloud catalog row, and re-tag the local file. Failure
            # never affects the ready download.
            def _bpm_async(path=artifact, jid=job_id, tid=track_id, t=final_title, a=artist,
                           lbl=tag_label, yr=tag_year, g=genre_str, cov=cover_data):
                try:
                    result = analyze_bpm(path, max_seconds=120)
                except Exception as exc:
                    logger.info("bpm skip job_id=%s detail=%r", jid, str(exc)[:200])
                    return
                try:
                    store.update_job(jid, bpm=result["bpm"], bpm_confidence=result.get("bpm_confidence"))
                except Exception as up_exc:
                    logger.info("bpm update skip job_id=%s detail=%r", jid, str(up_exc)[:100])
                if tid:
                    try:
                        tracks.update_bpm(tid, result["bpm"])
                    except Exception as db_exc:
                        logger.info("bpm track update skip track_id=%s detail=%r", tid, str(db_exc)[:100])
                try:
                    tag_audio_file(path, title=t, artist=a, label=lbl, year=yr, genre=g, bpm=result["bpm"], cover_data=cov)
                except Exception as tag_exc:
                    logger.info("bpm retag skip job_id=%s detail=%r", jid, str(tag_exc)[:100])

            threading.Thread(target=_bpm_async, name=f"bpm-{job_id}", daemon=True).start()
        except yt_dlp.utils.DownloadError as exc:
            # DownloadError carries yt-dlp's own diagnosis (e.g. YouTube's bot
            # check, geo-block, age gate) - surface it so the UI shows *why*
            # without digging through logs, but scrub anything that could
            # embed our request url or local paths before it leaves the worker.
            logger.error("download worker failed job_id=%s error_type=DownloadError", job_id)
            shutil.rmtree(job_dir, ignore_errors=True)
            store.update_job(job_id, status="error", error=public_ytdlp_error(exc), expires_at=time.time() + settings.artifact_ttl_seconds)
        except Exception as exc:  # worker boundary: detail goes to the server log only, never the API response
            logger.error("download worker failed job_id=%s error_type=%s detail=%r", job_id, type(exc).__name__, str(exc)[:300])
            shutil.rmtree(job_dir, ignore_errors=True)
            store.update_job(job_id, status="error", error="Download failed", expires_at=time.time() + settings.artifact_ttl_seconds)

    def process_youtube_direct(job_id: str, url: str, owner: str, req_genre: str | None) -> None:
        """YouTube-only direct download (Sezione 1, Task 1.1) with cloud storage.

        Differs from process_job in two ways:
        * Audio is pulled *straight from the given YouTube video* at MP3 320kbps -
          no SoundCloud-first substitution - because the whole point is grabbing
          the exclusive/bootleg that only exists on that video.
        * On success the file is uploaded to Cloudflare R2, a durable `tracks`
          row is written to Supabase, and the local temp file is dropped so
          Render's disk never fills. The track is then streamed via presigned
          URLs (/api/stream/{track_id}) instead of the local /file endpoint.

        When R2 is not configured (local dev / tests) it degrades to the classic
        behaviour: the file stays on disk and is served by the local endpoint,
        so nothing about this path requires R2 credentials to exercise.
        """
        job_dir = jobs_dir / job_id
        job_dir.mkdir(mode=0o700)
        started = time.monotonic()
        row = store.get_job_by_id(job_id)
        artist = row["artist"] if row else None
        title = row["title"] if row else None

        # Best-effort Discogs enrichment for label/year/genre + cover, same as
        # the main flow. Genre feeds both the R2 key and the catalog row.
        enrichment = None
        if artist and title:
            store.update_job(job_id, status="enriching")
            try:
                enrichment = discogs.enrich(artist, title)
            except Exception as exc:
                logger.info("discogs enrich skip job_id=%s detail=%r", job_id, str(exc)[:200])
                enrichment = None
            if enrichment:
                update = {
                    "label": enrichment.get("label"),
                    "year": enrichment.get("year"),
                    "country": enrichment.get("country"),
                    "catalog_no": enrichment.get("catalog_no"),
                    "style": json.dumps(enrichment.get("styles") or []),
                    "discogs_url": enrichment.get("discogs_url"),
                }
                if enrichment.get("cover_url"):
                    update["cover_url"] = enrichment["cover_url"]
                store.update_job(job_id, **update)

        styles = (enrichment.get("styles") if enrichment else None) or []
        genre_str = (req_genre.strip() if req_genre and req_genre.strip() else None) or (
            ", ".join(styles[:3]) if styles else None
        )

        try:
            store.update_job(job_id, status="downloading")
            try:
                info = attempt_download(job_dir, url, "320", settings, started, proxy=ytdlp_proxy())
            except Exception as direct_exc:
                logger.warning("process_youtube_direct attempt_download failed: %r, falling back to download_multi_source", str(direct_exc)[:200])
                for leftover in job_dir.iterdir():
                    if leftover.is_file():
                        leftover.unlink(missing_ok=True)
                info, _src = download_multi_source(job_dir, job_id, url, artist, title, None, "320", settings, started, proxy=ytdlp_proxy())
            if int(info.get("duration") or 0) > settings.max_duration_seconds:
                raise yt_dlp.utils.DownloadError("Media duration limit exceeded")
            candidates = [p for p in job_dir.iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
            if not candidates:
                raise RuntimeError("Downloaded artifact missing")
            source_file = candidates[0]
            if source_file.stat().st_size > settings.max_file_bytes:
                raise yt_dlp.utils.DownloadError("Download size limit exceeded")

            final_title = str(info.get("title") or title or "audio")[:200]
            filename = safe_filename(final_title, "mp3")
            artifact = job_dir / filename
            if source_file != artifact:
                source_file.replace(artifact)

            # Cover art + base ID3 tags now (BPM added later off the critical path).
            cover_data = None
            row_cover = row["cover_url"] if (row is not None and "cover_url" in row.keys()) else None
            cover_url_to_fetch = (enrichment.get("cover_url") if enrichment else None) or row_cover
            if cover_url_to_fetch:
                try:
                    req = urllib.request.Request(cover_url_to_fetch, headers={"User-Agent": "Drops/1.0"})
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        cover_data = resp.read()
                except Exception as c_exc:
                    logger.info("cover download skip detail=%r", str(c_exc)[:100])

            tag_label = enrichment.get("label") if enrichment else None
            tag_year = enrichment.get("year") if enrichment else None
            try:
                tag_audio_file(
                    artifact, title=final_title, artist=artist, label=tag_label,
                    year=tag_year, genre=genre_str, bpm=None, cover_data=cover_data,
                )
            except Exception as tag_exc:
                logger.info("id3 tagging skip detail=%r", str(tag_exc)[:100])

            duration_val = int(info.get("duration") or 0) or None
            size_val = artifact.stat().st_size

            if r2_storage.is_configured():
                store.update_job(job_id, status="uploading")
            track_id, r2_key = promote_to_cloud(owner, artifact, genre_str, artist, final_title)
            if track_id:
                # Cloud is the source of truth: no local file endpoint, and the
                # local temp file is dropped after BPM (see _bpm_and_cleanup).
                store.update_job(
                    job_id, status="ready", title=final_title, filename=filename,
                    file_path=None, size=size_val, source="youtube", duration=duration_val,
                    track_id=track_id, r2_key=r2_key,
                    expires_at=time.time() + settings.artifact_ttl_seconds,
                )
                logger.info("youtube-direct ready (cloud) job_id=%s track_id=%s", job_id, track_id)
            else:
                # R2 disabled or the upload/catalog write failed: fall back to the
                # classic behaviour - keep the file local and downloadable, no
                # catalog row. The download still succeeds either way.
                store.update_job(
                    job_id, status="ready", title=final_title, filename=filename,
                    file_path=str(artifact), size=size_val, source="youtube", duration=duration_val,
                    expires_at=time.time() + settings.artifact_ttl_seconds,
                )
                logger.info("youtube-direct ready (local fallback) job_id=%s", job_id)

            # BPM off the critical path. It reads the still-local file (no wasted
            # R2 re-download), writes the value to the catalog DB - the source of
            # truth for filtering/playback per the roadmap - and only then drops
            # the local temp file. The audio inside R2 is left untouched: the
            # desktop app writes the definitive BPM tag at USB-export time.
            def _bpm_and_cleanup(path=artifact, jid=job_id, tid=track_id, cloud=bool(track_id and r2_key)):
                try:
                    result = analyze_bpm(path, max_seconds=120)
                    bpm_value = result["bpm"]
                except Exception as exc:
                    logger.info("bpm skip job_id=%s detail=%r", jid, str(exc)[:200])
                    bpm_value = None
                if bpm_value is not None:
                    try:
                        store.update_job(jid, bpm=bpm_value, bpm_confidence=result.get("bpm_confidence"))
                    except Exception as up_exc:
                        logger.info("bpm job update skip job_id=%s detail=%r", jid, str(up_exc)[:100])
                    if tid:
                        try:
                            tracks.update_bpm(tid, bpm_value)
                        except Exception as db_exc:
                            logger.info("bpm track update skip track_id=%s detail=%r", tid, str(db_exc)[:100])
                if cloud:
                    # Cloud is the source of truth; free Render's disk now.
                    shutil.rmtree(job_dir, ignore_errors=True)

            threading.Thread(target=_bpm_and_cleanup, name=f"ytd-bpm-{job_id}", daemon=True).start()
        except yt_dlp.utils.DownloadError as exc:
            logger.error("youtube-direct worker failed job_id=%s error_type=DownloadError", job_id)
            shutil.rmtree(job_dir, ignore_errors=True)
            store.update_job(job_id, status="error", error=public_ytdlp_error(exc), expires_at=time.time() + settings.artifact_ttl_seconds)
        except Exception as exc:  # worker boundary: detail to server log only
            logger.error("youtube-direct worker failed job_id=%s error_type=%s detail=%r", job_id, type(exc).__name__, str(exc)[:300])
            shutil.rmtree(job_dir, ignore_errors=True)
            store.update_job(job_id, status="error", error="Download failed", expires_at=time.time() + settings.artifact_ttl_seconds)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/v1/auth/login")
    def login(credentials: LoginRequest, request: Request, response: Response):
        client_key = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for")
        if client_key:
            client_key = client_key.split(",")[0].strip()
        else:
            client_key = request.client.host if request.client else "unknown"
        if not store.allow_login_attempt(client_key, settings.login_rate_limit, settings.login_rate_window_seconds):
            raise HTTPException(status_code=429, detail="Too many login attempts")
        valid = secrets.compare_digest(credentials.username, settings.username)
        try:
            valid = password_hasher.verify(settings.password_hash, credentials.password) and valid
        except (VerifyMismatchError, InvalidHashError):
            valid = False
        if not valid:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        store.clear_login_attempts(client_key)
        token = issue_session_token(settings.username)
        response.set_cookie(COOKIE_NAME, token, max_age=settings.session_ttl_seconds, httponly=True, secure=settings.cookie_secure, samesite="lax", path="/")
        return {"username": settings.username}

    @app.get("/api/v1/auth/me")
    def me(owner: str = Depends(current_owner)):
        return {"username": owner}

    def spotify_call(operation):
        try:
            return operation()
        except SpotifyAgentError as exc:
            logger.warning("spotify request failed error_type=%s", type(exc).__name__)
            raise HTTPException(status_code=502, detail="Spotify request failed") from exc

    @app.get("/api/v1/spotify/status")
    def spotify_status(owner: str = Depends(current_owner)):
        return spotify_call(spotify.status)

    @app.post("/api/v1/discogs/enrich")
    def discogs_enrich(request: DiscogsEnrichRequest, owner: str = Depends(current_owner)):
        # Discogs client is best-effort by design: no token/downstream failure is null.
        return discogs.enrich(request.artist, request.title, request.isrc, request.catalog_no, request.barcode)

    @app.post("/api/v1/bpm/compute", status_code=202)
    def bpm_compute(request: BpmComputeRequest, owner: str = Depends(current_owner)):
        if not request.artist.strip() or not request.title.strip():
            raise HTTPException(status_code=422, detail="Artist and title required")
        try:
            return bpm_jobs.submit(track_key=request.track_key, artist=request.artist, title=request.title, isrc=request.isrc, source_url=request.source_url)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail="BPM worker unavailable") from exc

    @app.get("/api/v1/bpm/job/{job_id}")
    def bpm_job(job_id: str, owner: str = Depends(current_owner)):
        result = bpm_jobs.get(job_id)
        if result is None:
            raise HTTPException(status_code=404, detail="BPM job not found")
        return result

    @app.get("/api/v1/spotify/connect")
    def spotify_connect(request: Request, owner: str = Depends(current_owner)):
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
        if "workers.dev" in host or "drops" in host:
            r_uri = f"https://{host.split(':')[0]}/api/v1/spotify/callback"
        else:
            r_uri = os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8000/spotify/callback").strip()
        return RedirectResponse(spotify_call(lambda: spotify.create_authorization(r_uri)), status_code=302)

    @app.get("/api/v1/spotify/callback")
    def spotify_callback(request: Request, code: str, state: str, owner: str = Depends(current_owner)):
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
        if "workers.dev" in host or "drops" in host:
            r_uri = f"https://{host.split(':')[0]}/api/v1/spotify/callback"
        else:
            r_uri = os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8000/spotify/callback").strip()
        spotify_call(lambda: spotify.exchange_code(code, state, r_uri))
        return RedirectResponse("/app/spotify", status_code=302)

    @app.get("/api/v1/spotify/liked")
    def spotify_liked(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0), owner: str = Depends(current_owner)):
        return spotify_call(lambda: spotify.liked(limit, offset))

    @app.get("/api/v1/spotify/playlists")
    def spotify_playlists(owner: str = Depends(current_owner)):
        return spotify_call(spotify.playlists)

    @app.get("/api/v1/spotify/playlists/{playlist_id}/tracks")
    def spotify_playlist_tracks(playlist_id: str, owner: str = Depends(current_owner)):
        return spotify_call(lambda: spotify.playlist_tracks(playlist_id))

    @app.post("/api/v1/auth/logout", status_code=204)
    def logout(
        response: Response,
        _: None = Depends(require_csrf_origin),
        owner: str = Depends(verify_session),
    ):
        response.delete_cookie(COOKIE_NAME, httponly=True, secure=settings.cookie_secure, samesite="lax", path="/")

    @app.post("/api/v1/downloads", status_code=202)
    def start_download(
        request: WebDownloadRequest,
        owner: str = Depends(current_owner),
        _: None = Depends(require_csrf_origin),
    ):
        if not is_supported_url(request.url):
            raise HTTPException(status_code=400, detail="Unsupported URL")
        if request.quality not in AUDIO_QUALITY:
            raise HTTPException(status_code=400, detail="Invalid quality")
        job_id = str(uuid.uuid4())
        recognized = {}
        if request.title and request.artist:
            # Fast-path: title and artist provided in payload -> skip resolve_track in endpoint
            pass
        else:
            # Ultra-fast oEmbed check only; slow yt-dlp resolution deferred to background process_job
            try:
                recognized = resolve_track_oembed(request.url) or {}
            except Exception as e:
                logger.warning("resolve_track_oembed failed for %s: %s", request.url, e)
                recognized = {}
        accepted = store.create_job_if_capacity(
            job_id,
            owner,
            request.url,
            "audio",
            request.quality,
            settings.max_duration_seconds + settings.artifact_ttl_seconds,
            settings.max_queued + settings.max_concurrent,
            title=request.title or recognized.get("title"),
            artist=request.artist or recognized.get("artist"),
            cover_url=request.cover_url or recognized.get("cover_url"),
            raw_title=recognized.get("raw_title"),
            duration=recognized.get("duration"),
        )
        if not accepted:
            raise HTTPException(status_code=429, detail="Download queue full")
        app.state.executor.submit(process_job, job_id, request.url, request.quality)
        return public_job(store.get_job(job_id, owner))

    # --- Sezione 1 · Task 1.1: YouTube-only direct download -----------------
    @app.post("/api/download/youtube-direct", status_code=202)
    def youtube_direct(
        request: YoutubeDirectRequest,
        owner: str = Depends(current_owner),
        _: None = Depends(require_csrf_origin),
    ):
        """Extract audio (MP3 320kbps) directly from a YouTube video containing
        an exclusive track/set, asynchronously. Returns a task_id to poll for
        status; on success the track lands in the user's R2 cloud library."""
        if not is_youtube_url(request.url):
            raise HTTPException(status_code=400, detail="URL must be a YouTube link")
        job_id = str(uuid.uuid4())
        recognized = resolve_track(request.url)
        accepted = store.create_job_if_capacity(
            job_id,
            owner,
            request.url,
            "audio",
            "320",
            settings.max_duration_seconds + settings.artifact_ttl_seconds,
            settings.max_queued + settings.max_concurrent,
            title=request.title or recognized.get("title"),
            artist=request.artist or recognized.get("artist"),
            cover_url=request.cover_url or recognized.get("cover_url"),
            raw_title=recognized.get("raw_title"),
            duration=recognized.get("duration"),
        )
        if not accepted:
            raise HTTPException(status_code=429, detail="Download queue full")
        app.state.executor.submit(process_youtube_direct, job_id, request.url, owner, request.genre)
        job = public_job(store.get_job(job_id, owner))
        # Expose the polling id under the roadmap's "task_id" name too.
        job["task_id"] = job_id
        return job

    @app.get("/api/download/youtube-direct/{task_id}")
    def youtube_direct_status(task_id: str, owner: str = Depends(current_owner)):
        row = store.get_job(task_id, owner)
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        job = public_job(row)
        job["task_id"] = row["id"]
        return job

    @app.post("/api/v1/playlists/resolve")
    def resolve_playlist(
        request: PlaylistResolveRequest,
        owner: str = Depends(current_owner),
        _: None = Depends(require_csrf_origin),
    ):
        if not is_supported_url(request.url):
            raise HTTPException(status_code=400, detail="Unsupported URL")
        url_context = _youtube_url_context(request.url)
        options = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": "in_playlist",
            "playlistend": settings.max_queued + 1,
            "extractor_args": ytdlp_extractor_args(),
        }
        cookiefile = ytdlp_cookiefile()
        if cookiefile:
            options["cookiefile"] = cookiefile
        proxy = ytdlp_proxy()
        if proxy:
            options["proxy"] = proxy
        target_url = url_context.get("selected_track_url") if url_context.get("url_type") == "track" and url_context.get("selected_track_url") else request.url
        info = None
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(target_url, download=False)
        except Exception as exc:
            logger.warning("Playlist resolve initial attempt error=%r, retrying direct without proxy", str(exc)[:150])
            direct_opts = {k: v for k, v in options.items() if k != "proxy"}
            try:
                with yt_dlp.YoutubeDL(direct_opts) as ydl:
                    info = ydl.extract_info(target_url, download=False)
            except Exception as direct_exc:
                if url_context.get("selected_track_url") and target_url != url_context["selected_track_url"]:
                    try:
                        fallback_url = url_context["selected_track_url"]
                        with yt_dlp.YoutubeDL({**direct_opts, "extract_flat": False, "noplaylist": True}) as ydl:
                            info = ydl.extract_info(fallback_url, download=False)
                        url_context["url_type"] = "track"
                        url_context["playlist_id"] = None
                    except Exception:
                        logger.warning("playlist resolve direct fallback failed error=%r", str(direct_exc)[:200])
                        raise HTTPException(status_code=400, detail="Playlist non leggibile. Controlla link, privacy e disponibilita.") from direct_exc
                else:
                    logger.warning("playlist resolve direct attempt failed error=%r", str(direct_exc)[:200])
                    raise HTTPException(status_code=400, detail="Playlist non leggibile. Controlla link, privacy e disponibilita.") from direct_exc
        raw_entries = list((info or {}).get("entries") or [])
        if not raw_entries:
            raw_entries = [info or {}]
        entries = []
        for entry in raw_entries[: settings.max_queued]:
            if not entry:
                continue
            entry_url = _playlist_entry_url(entry, request.url)
            if not entry_url:
                continue
            entries.append({
                "id": entry.get("id"),
                "url": entry_url,
                "title": _clean_entry_title(entry, entry_url),
                "uploader": entry.get("uploader") or entry.get("channel") or "",
                "duration": entry.get("duration"),
            })
        if not entries:
            raise HTTPException(status_code=400, detail="Nessun elemento scaricabile trovato")
        selected_track = next(
            (entry for entry in entries if entry.get("id") == url_context["selected_track_id"]),
            None,
        )
        return {
            "title": (info or {}).get("title") or entries[0]["title"],
            "entries": entries,
            "count": len(entries),
            "truncated": len(raw_entries) > settings.max_queued,
            **url_context,
            "selected_track": selected_track,
        }

    @app.get("/api/v1/downloads")
    def list_downloads(
        limit: int = Query(default=100, ge=1, le=500),
        owner: str = Depends(current_owner),
    ):
        """Returns all historical downloads for the authenticated user."""
        rows = store.list_jobs(owner, limit=limit)
        return {"downloads": [public_job(row) for row in rows]}

    @app.post("/api/v1/downloads/clear")
    def clear_catalog(owner: str = Depends(current_owner)):
        """Resets and wipes all cloud catalog data, jobs, folders, and server files."""
        try:
            store.clear_all_jobs()
        except Exception as e:
            logger.warning("Error clearing jobs store: %s", e)
        try:
            folders.clear_all_folders()
        except Exception as e:
            logger.warning("Error clearing folder store: %s", e)
        try:
            tracks.clear_all_tracks()
        except Exception as e:
            logger.warning("Error clearing track store: %s", e)
        if jobs_dir.exists():
            for item in jobs_dir.iterdir():
                try:
                    if item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                    elif item.is_file():
                        item.unlink(missing_ok=True)
                except Exception:
                    pass
        return {"status": "ok", "message": "Catalogo cloud azzerato con successo"}

    @app.get("/api/v1/downloads/{job_id}")
    def get_download(job_id: str, owner: str = Depends(current_owner)):
        row = store.get_job(job_id, owner)
        if not row:
            raise HTTPException(status_code=404, detail="Download not found")
        return public_job(row)

    @app.get("/api/v1/downloads/{job_id}/file-url")
    def get_file_url(job_id: str, request: Request, owner: str = Depends(current_owner)):
        """Mint a short-lived signed URL for a download's /file endpoint so the
        front-end can consume the audio cross-origin (Web Audio EQ/volume and
        the "download whole folder" zip export) with crossOrigin="anonymous" -
        i.e. WITHOUT sending the session cookie. Ownership is checked here, so
        the token can only ever point at a file the caller already owns."""
        row = store.get_job(job_id, owner)
        if not row:
            raise HTTPException(status_code=404, detail="Download not found")
        token = issue_file_token(job_id, owner)
        # Absolute URL honouring the reverse proxy (Render / Cloudflare) so the
        # link is usable from the front origin, not just same-origin.
        scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
        base = f"{scheme}://{host}"
        return {
            "url": f"{base}/api/v1/downloads/{job_id}/file?token={token}",
            "token": token,
            "expires_in": settings.file_token_ttl_seconds,
        }

    @app.api_route("/api/v1/downloads/{job_id}/file", methods=["GET", "HEAD"])
    def get_file(
        job_id: str,
        request: Request,
        response: Response,
        token: str | None = Query(default=None),
        drops_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    ):
        # Auth: a valid short-lived signed token (cross-origin, cookie-less) OR
        # the normal session cookie. The token path lets an <audio> element with
        # crossOrigin="anonymous" and the zip-export fetch() reach the bytes
        # without credentials; CORS is added by the global middleware.
        refresh_cookie = False
        if token:
            try:
                payload = file_token_serializer.loads(token, max_age=settings.file_token_ttl_seconds)
            except SignatureExpired as exc:
                raise HTTPException(status_code=401, detail="File link expired") from exc
            except BadSignature as exc:
                raise HTTPException(status_code=401, detail="Invalid file link") from exc
            if payload.get("job_id") != job_id:
                # Token minted for a different file must never unlock this one.
                raise HTTPException(status_code=403, detail="Token does not match file")
            owner = payload.get("owner")
            if not owner:
                raise HTTPException(status_code=401, detail="Invalid file link")
        else:
            owner = verify_session(drops_session)
            refresh_cookie = True

        row = store.get_job(job_id, owner)
        if not row:
            raise HTTPException(status_code=404, detail="Download not found")

        is_head = request.method == "HEAD"
        range_header = request.headers.get("range")
        if range_header and not range_header.startswith("bytes="):
            range_header = None
        filename = row["filename"] or f"{job_id}.mp3"
        content_type = _audio_content_type(filename)

        result = None
        # 1. Local file on disk
        if row["file_path"]:
            path = Path(row["file_path"]).resolve()
            expected_root = (jobs_dir / job_id).resolve()
            if path.parent == expected_root and path.is_file():
                result = _serve_local_file(path, filename, content_type, range_header, is_head)
        # 2. Cloudflare R2 backup
        if result is None and row["r2_key"] and r2_storage.is_configured():
            try:
                result = _serve_r2_file(row["r2_key"], filename, content_type, range_header, is_head)
            except HTTPException:
                raise
            except Exception:
                result = None
        if result is None:
            raise HTTPException(status_code=404, detail="File temporaneo scaduto sul server. Rilancia il download dalla sorgente.")

        if refresh_cookie:
            # Sliding session refresh, matching current_owner (the cookie path
            # returns a Response directly, so set it on the outgoing response).
            result.set_cookie(
                COOKIE_NAME, issue_session_token(owner), max_age=settings.session_ttl_seconds,
                httponly=True, secure=settings.cookie_secure, samesite="lax", path="/",
            )
        return result

    # --- Sezione 3 · Task 3.1/3.2: cloud library + private streaming --------
    @app.get("/api/tracks")
    def list_tracks(
        limit: int = Query(200, ge=1, le=500),
        offset: int = Query(0, ge=0),
        owner: str = Depends(current_owner),
    ):
        """The authenticated user's cloud library (Supabase-backed catalog)."""
        return {"tracks": tracks.list_tracks(owner, limit=limit, offset=offset)}

    @app.get("/api/stream/{track_id}")
    def stream_track(track_id: str, owner: str = Depends(current_owner)):
        """Return a short-lived presigned R2 URL to stream a track the caller
        owns. Ownership is enforced against the catalog before any URL is
        minted, so one user can never mint a link for another user's file."""
        track = tracks.get_track(track_id)
        if not track:
            raise HTTPException(status_code=404, detail="Track not found")
        if track["user_id"] != owner:
            # 404 (not 403) so we never confirm the existence of another user's
            # track to someone who doesn't own it.
            raise HTTPException(status_code=404, detail="Track not found")
        if not r2_storage.is_configured():
            raise HTTPException(status_code=503, detail="Cloud storage not configured")
        try:
            ttl = r2_storage.presign_ttl_seconds()
            url = r2_storage.generate_presigned_url(track["r2_key"], expires_in=ttl)
        except r2_storage.R2Error as exc:
            logger.warning("presign failed track_id=%s detail=%r", track_id, str(exc)[:120])
            raise HTTPException(status_code=502, detail="Could not generate stream URL") from exc
        return {
            "track_id": track_id,
            "url": url,
            "expires_in": ttl,
            "title": track["title"],
            "artist": track["artist"],
            "genre": track["genre"],
            "bpm": track["bpm"],
        }

    @app.post("/api/v1/downloads/zip")
    def export_zip(
        request: Request,
        owner: str = Depends(current_owner),
        _: None = Depends(require_csrf_origin),
    ):
        # Get list of job_ids from body (or all ready jobs if empty)
        try:
            body = request.json() if hasattr(request, "json") else {}
        except Exception:
            body = {}
        job_ids = body.get("job_ids") if isinstance(body, dict) else None

        ready_jobs = store.list_jobs(owner)
        if job_ids:
            ready_jobs = [j for j in ready_jobs if j["id"] in job_ids and j["status"] == "ready" and j["file_path"]]
        else:
            ready_jobs = [j for j in ready_jobs if j["status"] == "ready" and j["file_path"]]

        if not ready_jobs:
            raise HTTPException(status_code=400, detail="Nessun file pronto da esportare")

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            m3u_lines = ["#EXTM3U"]
            for job in ready_jobs:
                p = Path(job["file_path"]).resolve()
                if p.is_file():
                    fname = job.get("filename") or p.name
                    zip_file.write(p, arcname=fname)
                    artist = job.get("artist") or ""
                    title = job.get("title") or fname
                    m3u_lines.append(f"#EXTINF:-1,{f'{artist} - ' if artist else ''}{title}")
                    m3u_lines.append(fname)
            zip_file.writestr("playlist.m3u8", "\n".join(m3u_lines).encode("utf-8"))

        zip_buffer.seek(0)
        filename = f"drops-export-{time.strftime('%Y%m%d-%H%M')}.zip"
        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/v1/content/extract-cover")
    def extract_cover(url: str, owner: str = Depends(current_owner)):
        """Extract og:image or cover artwork from a source reference URL."""
        if not url.startswith("http://") and not url.startswith("https://"):
            raise HTTPException(status_code=400, detail="URL non valido")
            
        try:
            req = urllib.request.Request(
                url, 
                headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                content = response.read()
                html = content.decode('utf-8', errors='ignore')
                
                # Cerca og:image
                match = re.search(r'<meta\s+[^>]*property=["\']og:image["\']\s+[^>]*content=["\']([^"\']+)["\']', html, re.IGNORECASE)
                if not match:
                    match = re.search(r'<meta\s+[^>]*content=["\']([^"\']+)["\']\s+[^>]*property=["\']og:image["\']', html, re.IGNORECASE)
                if not match:
                    match = re.search(r'<meta\s+[^>]*name=["\']twitter:image["\']\s+[^>]*content=["\']([^"\']+)["\']', html, re.IGNORECASE)
                if not match:
                    match = re.search(r'<meta\s+[^>]*content=["\']([^"\']+)["\']\s+[^>]*name=["\']twitter:image["\']', html, re.IGNORECASE)
                    
                if match:
                    img_url = match.group(1).strip()
                    if img_url.startswith('//'):
                        img_url = 'https:' + img_url
                    elif img_url.startswith('/'):
                        parsed = urllib.parse.urlparse(url)
                        img_url = f"{parsed.scheme}://{parsed.netloc}{img_url}"
                    return {"image_url": img_url}
                    
                # Fallback img tags
                img_matches = re.findall(r'<img\s+[^>]*src=["\']([^"\']+)["\']', html, re.IGNORECASE)
                for img in img_matches:
                    if 'logo' not in img.lower() and ('http' in img or img.startswith('/')):
                        if img.startswith('//'):
                            img = 'https:' + img
                        elif img.startswith('/'):
                            parsed = urllib.parse.urlparse(url)
                            img = f"{parsed.scheme}://{parsed.netloc}{img}"
                        return {"image_url": img}
                        
                raise HTTPException(status_code=404, detail="Nessuna immagine di copertina trovata nei metadati della pagina")
        except Exception as e:
            logger.error("Failed to extract cover from url=%s detail=%r", url, str(e))
            raise HTTPException(status_code=502, detail=f"Errore nel recupero della pagina: {str(e)}")

    @app.post("/api/v1/content/save")
    def save_article(article: dict, owner: str = Depends(current_owner)):
        """Save or update an article directly inside content.json on the local disk."""
        art_id = article.get("id")
        if not art_id:
            raise HTTPException(status_code=400, detail="ID articolo mancante")
            
        json_path = Path(__file__).resolve().parent.parent.parent / "drops-web-frontend" / "src" / "data" / "content.json"
        
        try:
            if json_path.is_file():
                with open(json_path, "r", encoding="utf-8") as f:
                    articles = json.load(f)
            else:
                articles = []
                
            found = False
            for idx, art in enumerate(articles):
                if art["id"] == art_id:
                    articles[idx] = article
                    found = True
                    break
                    
            if not found:
                articles.append(article)
                
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(articles, f, indent=2, ensure_ascii=False)
                
            return {"status": "success", "article": article}
        except Exception as e:
            logger.error("Failed to save article id=%s detail=%r", art_id, str(e))
            raise HTTPException(status_code=500, detail=f"Errore durante il salvataggio sul disco: {str(e)}")

    @app.delete("/api/v1/content/delete/{article_id}")
    def delete_article(article_id: str, owner: str = Depends(current_owner)):
        """Delete an article from content.json on the local disk."""
        import json
        
        json_path = Path(__file__).resolve().parent.parent.parent / "drops-web-frontend" / "src" / "data" / "content.json"
        
        try:
            if json_path.is_file():
                with open(json_path, "r", encoding="utf-8") as f:
                    articles = json.load(f)
            else:
                raise HTTPException(status_code=404, detail="File content.json non trovato")
                
            updated_articles = [art for art in articles if art["id"] != article_id]
            if len(updated_articles) == len(articles):
                raise HTTPException(status_code=404, detail="Articolo non trovato")
                
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(updated_articles, f, indent=2, ensure_ascii=False)
                
            return {"status": "success"}
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to delete article id=%s detail=%r", article_id, str(e))
            raise HTTPException(status_code=500, detail=f"Errore durante l'eliminazione: {str(e)}")

    # --- Academy: feedback-track submissions (R2-backed) -------------------

    @app.post("/api/v1/academy/submissions/presign")
    def academy_presign_upload(
        request: AcademySubmissionPresignRequest,
        owner: str = Depends(current_owner),
        _: None = Depends(require_csrf_origin),
    ):
        """Mint a presigned POST so the browser uploads the track straight to
        R2 (never through this backend), capped at academy_max_upload_bytes
        via R2's own content-length-range policy condition."""
        if not r2_storage.is_configured():
            raise HTTPException(status_code=503, detail="Cloud storage not configured")
        if request.content_type not in ACADEMY_ALLOWED_CONTENT_TYPES:
            raise HTTPException(status_code=400, detail="Formato non supportato. Usa WAV o MP3.")
        if request.size_bytes <= 0 or request.size_bytes > settings.academy_max_upload_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File troppo grande. Limite: {settings.academy_max_upload_bytes // (1024 * 1024)} MB.",
            )
        submission_id = str(uuid.uuid4())
        key = r2_storage.build_academy_object_key(owner, submission_id, request.filename)
        try:
            post = r2_storage.generate_presigned_post(
                key, content_type=request.content_type, max_bytes=settings.academy_max_upload_bytes,
            )
        except r2_storage.R2Error as exc:
            logger.warning("academy presign failed owner=%s detail=%r", owner, str(exc)[:120])
            raise HTTPException(status_code=502, detail="Could not prepare upload") from exc
        academy.create_pending(
            user_id=owner, r2_key=key, content_type=request.content_type,
            title=request.title, bpm=request.bpm, genre=request.genre,
            focus_area=request.focus_area, filename=request.filename,
            submission_id=submission_id,
        )
        return {
            "submission_id": submission_id,
            "upload_url": post["url"],
            "upload_fields": post["fields"],
            "key": key,
            "max_bytes": settings.academy_max_upload_bytes,
        }

    @app.post("/api/v1/academy/submissions/{submission_id}/complete")
    def academy_complete_upload(
        submission_id: str, owner: str = Depends(current_owner), _: None = Depends(require_csrf_origin),
    ):
        """Called after the browser's direct-to-R2 POST succeeds. Verifies the
        object actually landed (HEAD) before trusting the client and flipping
        the row to ready - a forged call with no real upload just 409s."""
        submission = academy.get(submission_id)
        if not submission or submission["user_id"] != owner:
            raise HTTPException(status_code=404, detail="Submission not found")
        if submission["status"] == ACADEMY_STATUS_READY:
            return submission
        try:
            meta = r2_storage.head_object(submission["r2_key"])
        except r2_storage.R2NotFoundError:
            raise HTTPException(status_code=409, detail="Upload not found yet on storage, retry after it finishes")
        except r2_storage.R2Error as exc:
            raise HTTPException(status_code=502, detail="Could not verify upload") from exc
        actual_size = int(meta.get("ContentLength") or 0)
        if actual_size <= 0 or actual_size > settings.academy_max_upload_bytes:
            r2_storage.delete_object(submission["r2_key"])
            raise HTTPException(status_code=400, detail="Uploaded file failed size validation")
        updated = academy.mark_ready(submission_id, size_bytes=actual_size)
        return updated

    @app.get("/api/v1/academy/submissions")
    def academy_list_submissions(
        limit: int = Query(200, ge=1, le=500),
        offset: int = Query(0, ge=0),
        owner: str = Depends(current_owner),
    ):
        return {"submissions": academy.list_for_user(owner, limit=limit, offset=offset)}

    def _stream_academy_object(submission: dict, range_header: str | None):
        try:
            obj = r2_storage.get_object(submission["r2_key"], range_header=range_header)
        except r2_storage.R2InvalidRangeError as exc:
            headers = {"Content-Range": f"bytes */{exc.total_size}"} if exc.total_size is not None else {}
            raise HTTPException(status_code=416, detail="Range not satisfiable", headers=headers) from exc
        except r2_storage.R2NotFoundError as exc:
            raise HTTPException(status_code=404, detail="Track not found") from exc
        except r2_storage.R2Error as exc:
            raise HTTPException(status_code=502, detail="Could not stream track") from exc

        def iter_body(body, chunk_size: int = 64 * 1024):
            try:
                while True:
                    chunk = body.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
            finally:
                body.close()

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(obj["ContentLength"]),
        }
        content_range = obj.get("ContentRange")
        status_code = 200
        if content_range:
            headers["Content-Range"] = content_range
            status_code = 206
        return StreamingResponse(
            iter_body(obj["Body"]),
            status_code=status_code,
            media_type=submission["content_type"] or obj.get("ContentType") or "application/octet-stream",
            headers=headers,
        )

    @app.get("/api/v1/academy/submissions/{submission_id}/stream")
    def academy_stream_submission(
        submission_id: str, request: Request, owner: str = Depends(current_owner),
    ):
        """Range-aware audio proxy for the Academy submission player (DJ Lab
        preview deck and the global Mini-Player). Unlike /api/stream (personal
        library, redirected to a presigned R2 GET url) this streams the bytes
        through the backend, so seeking works via ordinary same-origin Range
        requests without needing R2 bucket-level CORS configured."""
        submission = academy.get(submission_id)
        if not submission or submission["user_id"] != owner:
            raise HTTPException(status_code=404, detail="Submission not found")
        if submission["status"] != ACADEMY_STATUS_READY:
            raise HTTPException(status_code=409, detail="Submission upload not complete yet")
        range_header = request.headers.get("range")
        if range_header and not range_header.startswith("bytes="):
            range_header = None
        return _stream_academy_object(submission, range_header)

    _ACADEMY_CONTENT_TYPE_SUFFIX = {"audio/wav": ".wav", "audio/x-wav": ".wav", "audio/mpeg": ".mp3"}

    @app.post("/api/v1/academy/submissions/{submission_id}/analyze-bpm")
    async def academy_analyze_bpm(
        submission_id: str, owner: str = Depends(current_owner), _: None = Depends(require_csrf_origin),
    ):
        """Downloads the submission's audio from R2 and runs the local
        onset/tempo BPM analyzer (bpm_analyzer.py) on it, overwriting the
        row's bpm/bpm_confidence with the measured value - the automatic
        counterpart to the bpm the student self-reports in the submission
        form."""
        submission = academy.get(submission_id)
        if not submission or submission["user_id"] != owner:
            raise HTTPException(status_code=404, detail="Submission not found")
        if submission["status"] != ACADEMY_STATUS_READY:
            raise HTTPException(status_code=409, detail="Submission upload not complete yet")
        if not r2_storage.is_configured():
            raise HTTPException(status_code=503, detail="Cloud storage not configured")
        suffix = _ACADEMY_CONTENT_TYPE_SUFFIX.get(submission["content_type"], ".mp3")
        try:
            result = await analyze_r2_object_bpm_async(submission["r2_key"], suffix=suffix)
        except r2_storage.R2NotFoundError as exc:
            raise HTTPException(status_code=404, detail="Audio file not found on storage") from exc
        except r2_storage.R2Error as exc:
            logger.warning("academy bpm download failed submission_id=%s detail=%r", submission_id, str(exc)[:120])
            raise HTTPException(status_code=502, detail="Could not fetch audio for analysis") from exc
        except BpmAnalysisError as exc:
            logger.info("academy bpm analysis failed submission_id=%s detail=%r", submission_id, str(exc)[:150])
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return academy.update_bpm(
            submission_id, bpm=result["bpm"],
            bpm_confidence=result.get("bpm_confidence"), bpm_source=result.get("bpm_source"),
        )

    # --- Folders (library crates) -------------------------------------------

    @app.get("/api/v1/folders")
    def list_folders(
        limit: int = Query(200, ge=1, le=500),
        offset: int = Query(0, ge=0),
        owner: str = Depends(current_owner),
    ):
        return {"folders": folders.list_for_user(owner, limit=limit, offset=offset)}

    @app.post("/api/v1/folders", status_code=201)
    def create_folder(
        request: FolderCreateRequest,
        owner: str = Depends(current_owner),
        _: None = Depends(require_csrf_origin),
    ):
        name = request.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Nome cartella richiesto")
        return folders.create_folder(
            user_id=owner, name=name, dominant_genre=request.dominant_genre,
            track_ids=request.track_ids,
        )

    @app.patch("/api/v1/folders/{folder_id}")
    def rename_folder(
        folder_id: str,
        request: FolderRenameRequest,
        owner: str = Depends(current_owner),
        _: None = Depends(require_csrf_origin),
    ):
        name = request.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Nome cartella richiesto")
        updated = folders.rename(folder_id, user_id=owner, name=name)
        if updated is None:
            raise HTTPException(status_code=404, detail="Cartella non trovata")
        return updated

    @app.delete("/api/v1/folders/{folder_id}")
    def delete_folder(
        folder_id: str,
        owner: str = Depends(current_owner),
        _: None = Depends(require_csrf_origin),
    ):
        deleted = folders.delete(folder_id, user_id=owner)
        if not deleted:
            raise HTTPException(status_code=404, detail="Cartella non trovata")
        return {"status": "deleted", "id": folder_id}

    return app
