"""Async Cloudflare R2 upload path, built on aioboto3.

Why this exists alongside ``r2_storage.py``: the sync module is correct and
sufficient for the current single-track download flow (``process_job`` runs
inside a ``ThreadPoolExecutor`` worker, so a blocking ``boto3`` call never
stalls the FastAPI event loop). This module targets the next use case on the
roadmap - batch/playlist promotion (DJ Lab, Discogs crate import, etc.) where
we want to push several finished MP3s to R2 *concurrently* from one coroutine
without burning one ``max_concurrent`` worker thread per file.

Design choices, deliberately mirroring ``r2_storage.py`` so the two modules
stay interchangeable from a caller's point of view:

* Same env vars, same ``R2Error`` exception type (imported, not redefined) -
  callers can catch one exception regardless of which path they used.
* Same object-key layout (``build_object_key`` is reused, not duplicated).
* Best-effort: every function assumes the caller already checked
  ``is_configured()``; call sites in ``r2_storage`` do the same.
* No cached client. aiobotocore clients are meant to live inside an
  ``async with`` block tied to one event loop; caching one across requests
  risks reusing a client whose loop/session has gone away. Upload volume here
  is "a handful of tracks per playlist import", not a hot path, so paying
  client-construction cost per call (or per batch, see ``upload_many``) is a
  fine trade for correctness. If this ever becomes a bottleneck, wrap
  ``_client()`` in a loop-keyed cache the way ``r2_storage._get_client``
  keys off nothing but the process.

Requires the ``aioboto3`` package (see requirements-web.txt).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from r2_storage import (  # noqa: F401 - re-exported for callers that only import this module
    R2Error,
    _endpoint_url,
    bucket_name,
    build_object_key,
    is_configured,
    presign_ttl_seconds,
)

logger = logging.getLogger("drops.r2.async")

DEFAULT_UPLOAD_CONCURRENCY = 4


def _session():
    import aioboto3

    # Session() just holds config; it opens no connections until a client is
    # created, so building a fresh one per call is cheap.
    return aioboto3.Session()


def _client_ctx():
    from botocore.config import Config

    return _session().client(
        "s3",
        endpoint_url=_endpoint_url(),
        aws_access_key_id=_env_or_raise("DROPS_R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_env_or_raise("DROPS_R2_SECRET_ACCESS_KEY"),
        region_name="auto",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def _env_or_raise(name: str) -> str:
    import os

    value = os.environ.get(name, "").strip()
    if not value:
        raise R2Error(f"missing env var {name}")
    return value


async def upload_file_async(local_path: str, key: str, *, content_type: str = "audio/mpeg") -> str:
    """Upload ``local_path`` to ``key`` in the R2 bucket. Returns the key.

    Raises R2Error on failure or when R2 is not configured - same contract as
    ``r2_storage.upload_file``.
    """
    if not is_configured():
        raise R2Error("R2 not configured")
    try:
        async with _client_ctx() as client:
            await client.upload_file(
                Filename=local_path,
                Bucket=bucket_name(),
                Key=key,
                ExtraArgs={"ContentType": content_type},
            )
    except R2Error:
        raise
    except Exception as exc:  # aiobotocore raises many concrete types
        raise R2Error(f"upload failed: {type(exc).__name__}") from exc
    logger.info("r2 async upload ok key=%s", key)
    return key


async def generate_presigned_url_async(key: str, *, expires_in: int | None = None) -> str:
    """Async twin of ``r2_storage.generate_presigned_url``."""
    if not is_configured():
        raise R2Error("R2 not configured")
    ttl = expires_in if expires_in is not None else presign_ttl_seconds()
    ttl = max(60, min(ttl, 7 * 24 * 3600))
    try:
        async with _client_ctx() as client:
            return await client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket_name(), "Key": key},
                ExpiresIn=ttl,
            )
    except R2Error:
        raise
    except Exception as exc:
        raise R2Error(f"presign failed: {type(exc).__name__}") from exc


async def delete_object_async(key: str) -> None:
    """Best-effort async delete (mirrors ``r2_storage.delete_object``)."""
    if not is_configured():
        return
    try:
        async with _client_ctx() as client:
            await client.delete_object(Bucket=bucket_name(), Key=key)
    except Exception as exc:
        logger.info("r2 async delete skip key=%s detail=%r", key, str(exc)[:120])


@dataclass(frozen=True)
class UploadResult:
    """Outcome of one file in a batch upload - never raises, always reports."""

    local_path: str
    key: str
    ok: bool
    error: str | None = None


async def upload_many_async(
    items: list[tuple[str, str]],
    *,
    content_type: str = "audio/mpeg",
    max_concurrency: int = DEFAULT_UPLOAD_CONCURRENCY,
) -> list[UploadResult]:
    """Upload several ``(local_path, key)`` pairs concurrently.

    Bounded by ``max_concurrency`` (default 4) so a large playlist import
    doesn't open unbounded parallel connections to R2. One item failing never
    aborts the rest - each result reports its own ok/error so the caller (e.g.
    a playlist-promotion job) can retry or skip individually, the same
    best-effort spirit as the rest of the R2 integration.
    """
    if not items:
        return []
    semaphore = asyncio.Semaphore(max(1, max_concurrency))

    async def _one(local_path: str, key: str) -> UploadResult:
        async with semaphore:
            try:
                await upload_file_async(local_path, key, content_type=content_type)
                return UploadResult(local_path, key, ok=True)
            except R2Error as exc:
                logger.warning("r2 batch upload failed key=%s detail=%r", key, str(exc)[:120])
                return UploadResult(local_path, key, ok=False, error=str(exc))

    return await asyncio.gather(*(_one(path, key) for path, key in items))
