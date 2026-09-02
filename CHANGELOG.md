# Changelog

## [Unreleased]

### Added

- Cross-origin audio serving for the Archivio Web Audio graph (3-band EQ + master volume) and the "Scarica cartella" zip export. `GET /api/v1/downloads/{id}/file` now:
  - accepts a short-lived signed token in the query string (`?token=...`, HMAC via itsdangerous, salt `drops-web-file-access`, TTL `DROPS_WEB_FILE_TOKEN_TTL_SECONDS`, default 300s) so it is reachable cookie-less with `crossOrigin="anonymous"`, alongside the existing session-cookie auth;
  - is served with a wildcard `Access-Control-Allow-Origin: *` (new outer `PublicFileCORSMiddleware`, pure-ASGI, applied only to this endpoint; it also answers the `OPTIONS` preflight and strips the credentialed CORS markers a wildcard can't be combined with) on the 200, 206 and OPTIONS responses;
  - supports HTTP Range requests (`206 Partial Content` + `Content-Range`, `416` when unsatisfiable, `Accept-Ranges: bytes` always) for seek/Web-Audio, on both the local-file and R2-fallback branches, with the real audio `Content-Type` (mp3/flac/m4a/mp4/wav/…), `Content-Length` and `Cache-Control: private, max-age=3600`.
- `GET /api/v1/downloads/{id}/file-url`: owner-authenticated helper that mints the signed URL (returns `{url, token, expires_in}`, absolute URL honouring the reverse proxy) for the front to fetch.
- `DROPS_WEB_FILE_TOKEN_TTL_SECONDS` setting (default 300). 15 new tests (`tests/test_file_endpoint.py`): token mint/auth, cookie-less access, expired/mismatched-token rejection, Range 206/suffix/416, HEAD, R2 fallback, wildcard CORS on GET/206/preflight.

### Fixed

- Reso più robusto accesso YouTube: attesa PO-token provider al cold-start, proxy mantenuto sui bot-check, validazione cookie Netscape, dipendenze extractor riproducibili e messaggi utente senza istruzioni interne `yt-dlp`.

### Added

- `r2_storage_async.py`: aioboto3-based async R2 upload path (`upload_file_async`, `generate_presigned_url_async`, `delete_object_async`, `upload_many_async` with bounded concurrency) for future batch/playlist promotion flows, alongside the existing sync `r2_storage.py` used by the single-track download job. Same env vars, same `R2Error`, same object-key layout.
- Academy feedback-track submissions: `academy_store.py` (new `academy_submissions` table/catalog) plus three endpoints in `web_app.py` -
  `POST /api/v1/academy/submissions/presign` (presigned-POST direct-to-R2 upload, WAV/MP3 only, capped by `DROPS_ACADEMY_MAX_UPLOAD_BYTES`, default 100MB, enforced by R2's own `content-length-range` policy condition),
  `POST /api/v1/academy/submissions/{id}/complete` (HEAD-verifies the upload actually landed before marking it ready),
  `GET /api/v1/academy/submissions` (owner-scoped list).
- `GET /api/v1/academy/submissions/{id}/stream`: Range-aware audio proxy (206 Partial Content / 416 Range Not Satisfiable) for the DJ Lab preview deck and the global Mini-Player, backed by new `r2_storage.get_object`/`head_object`/`generate_presigned_post` helpers and `R2NotFoundError`/`R2InvalidRangeError`.
- `POST /api/v1/academy/submissions/{id}/analyze-bpm`: downloads the submission's audio from R2 and runs the existing onset/tempo analyzer (`bpm_analyzer.py`, FFmpeg + NumPy autocorrelation - no new librosa/aubio dependency) on it, overwriting the row's `bpm`/`bpm_confidence`/`bpm_source`. New `bpm_analyzer_async.py` (asyncio.to_thread wrapper + R2 download orchestration) and `r2_storage_async.download_file_async`. 22 new tests, including precision tests against synthetic click-track WAV/MP3 fixtures at known tempos (`tests/test_bpm_analyzer_precision.py`).
- Library folders ("crates"): `folder_store.py` (new `folders` + `folder_tracks` tables, Supabase Postgres when `DATABASE_URL` is set, SQLite fallback otherwise - same selection as `track_store.py`/`academy_store.py`) plus `GET/POST /api/v1/folders`, `PATCH /api/v1/folders/{id}` (rename), `DELETE /api/v1/folders/{id}`, all owner-scoped. 25 new tests.

## [0.1.0] - 2026-08-23

### Added

- Playlist resolution now identifies mixed YouTube track-and-playlist URLs and returns selected-track context for frontend choice dialogs.

## [0.0.1] - 2026-08-23

### Fixed

- Explicit YouTube URLs now download the requested video directly instead of substituting a similarly named SoundCloud track.
