import logging
import os
import secrets
import shutil
import threading
import time
import json
import uuid
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yt_dlp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

import urllib.request
from bpm_analyzer import analyze_bpm
from download_engine import AUDIO_QUALITY, download_multi_source
from media_core import is_supported_url, resolve_track, safe_filename, tag_audio_file, ytdlp_cookiefile, ytdlp_extractor_args, ytdlp_proxy
from spotify_agent import SpotifyAgentError, WebSpotifyClient
from discogs_agent import DiscogsClient
from bpm_jobs import BpmJobManager
from web_settings import WebSettings
from web_store import WebStore

COOKIE_NAME = "drops_session"
logger = logging.getLogger("drops.web")


class LoginRequest(BaseModel):
    username: str
    password: str


class WebDownloadRequest(BaseModel):
    url: str
    quality: str = "320"
    artist: str | None = None
    title: str | None = None
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
    discogs = DiscogsClient(settings.state_dir)
    spotify = WebSpotifyClient(settings.state_dir, discogs=discogs)
    bpm_jobs = BpmJobManager(settings.state_dir, max_workers=min(2, settings.max_concurrent))
    password_hasher = PasswordHasher()
    session_serializer = URLSafeTimedSerializer(settings.session_secret, salt="drops-web-session")

    def issue_session_token(owner: str) -> str:
        return session_serializer.dumps({"username": owner, "iat": int(time.time())})

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
    app.state.discogs = discogs
    app.state.executor = None

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

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
        ):
            if row[key] is not None:
                result[key] = row[key]
        if row["style"]:
            result["style"] = json.loads(row["style"])
        if row["status"] == "ready":
            result["file_url"] = f"/api/v1/downloads/{row['id']}/file"
        return result

    def process_job(job_id: str, url: str, quality: str) -> None:
        job_dir = jobs_dir / job_id
        job_dir.mkdir(mode=0o700)
        started = time.monotonic()
        row = store.get_job_by_id(job_id)
        artist = row["artist"] if row else None
        title = row["title"] if row else None
        duration = row["duration"] if row else None
        raw_title = row["raw_title"] if row else None

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
            candidates = [path for path in job_dir.iterdir() if path.is_file() and not path.name.endswith((".part", ".ytdl"))]
            if len(candidates) != 1:
                raise RuntimeError("Downloaded artifact missing")
            source_file = candidates[0]
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
                expires_at=time.time() + settings.artifact_ttl_seconds,
            )

            t_ready = time.monotonic()
            logger.info(
                "process_job timings job_id=%s source=%s enrich=%.1fs multi_source=%.1fs finalize=%.1fs total=%.1fs",
                job_id, source, t_after_enrich - started, t_after_dl - t_after_enrich,
                t_ready - t_after_dl, t_ready - started,
            )

            # BPM off the critical path: analyze in a daemon thread, then patch the
            # job row and re-tag the file. Failure never affects the ready download.
            def _bpm_async(path=artifact, jid=job_id, t=final_title, a=artist,
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
            detail = str(exc).replace(url, "[url]").replace(str(job_dir), "[job]").strip() or "Download failed"
            store.update_job(job_id, status="error", error=detail[:300], expires_at=time.time() + settings.artifact_ttl_seconds)
        except Exception as exc:  # worker boundary: detail goes to the server log only, never the API response
            logger.error("download worker failed job_id=%s error_type=%s detail=%r", job_id, type(exc).__name__, str(exc)[:300])
            shutil.rmtree(job_dir, ignore_errors=True)
            store.update_job(job_id, status="error", error="Download failed", expires_at=time.time() + settings.artifact_ttl_seconds)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/v1/auth/login")
    def login(credentials: LoginRequest, request: Request, response: Response):
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
    def spotify_connect(owner: str = Depends(current_owner)):
        return RedirectResponse(spotify_call(spotify.create_authorization), status_code=302)

    @app.get("/api/v1/spotify/callback")
    def spotify_callback(code: str, state: str, owner: str = Depends(current_owner)):
        spotify_call(lambda: spotify.exchange_code(code, state))
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
        recognized = resolve_track(request.url)
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

    @app.post("/api/v1/playlists/resolve")
    def resolve_playlist(
        request: PlaylistResolveRequest,
        owner: str = Depends(current_owner),
        _: None = Depends(require_csrf_origin),
    ):
        if not is_supported_url(request.url):
            raise HTTPException(status_code=400, detail="Unsupported URL")
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
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(request.url, download=False)
        except Exception as exc:
            logger.warning("playlist resolve fallita error=%r", str(exc)[:200])
            raise HTTPException(status_code=400, detail="Playlist non leggibile. Controlla link, privacy e disponibilita.") from exc
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
                "url": entry_url,
                "title": _clean_entry_title(entry, entry_url),
                "uploader": entry.get("uploader") or entry.get("channel") or "",
                "duration": entry.get("duration"),
            })
        if not entries:
            raise HTTPException(status_code=400, detail="Nessun elemento scaricabile trovato")
        return {
            "title": (info or {}).get("title") or entries[0]["title"],
            "entries": entries,
            "count": len(entries),
            "truncated": len(raw_entries) > settings.max_queued,
        }

    @app.get("/api/v1/downloads/{job_id}")
    def get_download(job_id: str, owner: str = Depends(current_owner)):
        row = store.get_job(job_id, owner)
        if not row:
            raise HTTPException(status_code=404, detail="Download not found")
        return public_job(row)

    @app.get("/api/v1/downloads/{job_id}/file")
    def get_file(job_id: str, owner: str = Depends(current_owner)):
        row = store.get_job(job_id, owner)
        if not row or row["status"] != "ready" or not row["file_path"]:
            raise HTTPException(status_code=404, detail="File not found")
        path = Path(row["file_path"]).resolve()
        expected_root = (jobs_dir / job_id).resolve()
        if path.parent != expected_root or not path.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(path, filename=row["filename"], media_type="audio/mpeg")

    @app.post("/api/v1/downloads/zip")
    def export_zip(
        request: Request,
        owner: str = Depends(current_owner),
        _: None = Depends(require_csrf_origin),
    ):
        import io
        import zipfile
        from starlette.responses import StreamingResponse

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

    return app
