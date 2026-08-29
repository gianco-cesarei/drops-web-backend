# Changelog

## [Unreleased]

### Added

- `r2_storage_async.py`: aioboto3-based async R2 upload path (`upload_file_async`, `generate_presigned_url_async`, `delete_object_async`, `upload_many_async` with bounded concurrency) for future batch/playlist promotion flows, alongside the existing sync `r2_storage.py` used by the single-track download job. Same env vars, same `R2Error`, same object-key layout.
- Academy feedback-track submissions: `academy_store.py` (new `academy_submissions` table/catalog) plus three endpoints in `web_app.py` -
  `POST /api/v1/academy/submissions/presign` (presigned-POST direct-to-R2 upload, WAV/MP3 only, capped by `DROPS_ACADEMY_MAX_UPLOAD_BYTES`, default 100MB, enforced by R2's own `content-length-range` policy condition),
  `POST /api/v1/academy/submissions/{id}/complete` (HEAD-verifies the upload actually landed before marking it ready),
  `GET /api/v1/academy/submissions` (owner-scoped list).
- `GET /api/v1/academy/submissions/{id}/stream`: Range-aware audio proxy (206 Partial Content / 416 Range Not Satisfiable) for the DJ Lab preview deck and the global Mini-Player, backed by new `r2_storage.get_object`/`head_object`/`generate_presigned_post` helpers and `R2NotFoundError`/`R2InvalidRangeError`.

## [0.1.0] - 2026-08-23

### Added

- Playlist resolution now identifies mixed YouTube track-and-playlist URLs and returns selected-track context for frontend choice dialogs.

## [0.0.1] - 2026-08-23

### Fixed

- Explicit YouTube URLs now download the requested video directly instead of substituting a similarly named SoundCloud track.
