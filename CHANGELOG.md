# Changelog

## [Unreleased]

### Added

- `r2_storage_async.py`: aioboto3-based async R2 upload path (`upload_file_async`, `generate_presigned_url_async`, `delete_object_async`, `upload_many_async` with bounded concurrency) for future batch/playlist promotion flows, alongside the existing sync `r2_storage.py` used by the single-track download job. Same env vars, same `R2Error`, same object-key layout.

## [0.1.0] - 2026-08-23

### Added

- Playlist resolution now identifies mixed YouTube track-and-playlist URLs and returns selected-track context for frontend choice dialogs.

## [0.0.1] - 2026-08-23

### Fixed

- Explicit YouTube URLs now download the requested video directly instead of substituting a similarly named SoundCloud track.
