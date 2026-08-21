"""Cloudflare R2 object storage for the user's private cloud music library.

R2 speaks the S3 API, so we drive it with boto3's S3 client pointed at the
account-specific R2 endpoint. Egress from R2 is $0, which is why the whole
library lives here instead of on Supabase (see AGENTS.md: "Mai file audio su
Supabase").

Everything here is *best-effort*: if the R2 env vars are not configured the
module reports ``is_configured() == False`` and the download flow keeps working
exactly as before (file stays downloadable locally, just not promoted to the
cloud). This keeps local dev and the existing single-download flow green
without any R2 credentials.

Env vars (all required to enable R2):
    DROPS_R2_ACCOUNT_ID       Cloudflare account id (builds the endpoint url)
    DROPS_R2_ACCESS_KEY_ID    R2 API token access key id
    DROPS_R2_SECRET_ACCESS_KEY R2 API token secret
    DROPS_R2_BUCKET           bucket name (e.g. "drops-library")
Optional:
    DROPS_R2_ENDPOINT_URL     override the derived endpoint (S3-compatible)
    DROPS_R2_PRESIGN_TTL_SECONDS  default presigned-url lifetime (default 3600)
"""

from __future__ import annotations

import logging
import os
import re
import threading
import unicodedata

logger = logging.getLogger("drops.r2")

DEFAULT_PRESIGN_TTL_SECONDS = 3600


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def presign_ttl_seconds() -> int:
    raw = _env("DROPS_R2_PRESIGN_TTL_SECONDS")
    if not raw:
        return DEFAULT_PRESIGN_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_PRESIGN_TTL_SECONDS
    # S3 SigV4 caps presigned-url expiry at 7 days; clamp defensively.
    return max(60, min(value, 7 * 24 * 3600))


def is_configured() -> bool:
    """True only when every credential needed to reach R2 is present."""
    return all(
        _env(name)
        for name in (
            "DROPS_R2_ACCESS_KEY_ID",
            "DROPS_R2_SECRET_ACCESS_KEY",
            "DROPS_R2_BUCKET",
        )
    ) and bool(_endpoint_url())


def _endpoint_url() -> str:
    explicit = _env("DROPS_R2_ENDPOINT_URL")
    if explicit:
        return explicit
    account_id = _env("DROPS_R2_ACCOUNT_ID")
    if account_id:
        return f"https://{account_id}.r2.cloudflarestorage.com"
    return ""


def bucket_name() -> str:
    return _env("DROPS_R2_BUCKET")


# --- key building -----------------------------------------------------------

# Punctuation we explicitly allow through in a key segment. Everything else that
# is not a Unicode letter/number/space (control chars, slashes, emoji, quotes
# that break shells/URLs) is dropped. Accented artist names (Rødhåd, Fjaak…)
# survive because we test Unicode letter categories, not an ASCII whitelist.
_KEY_ALLOWED_PUNCT = set(" _.()[]&+'-")


def _slug_segment(value: str | None, fallback: str) -> str:
    """Normalize one path segment (genre/artist/title) for an object key.

    Keeps it human-readable (spaces, accents and common punctuation survive) but
    strips slashes, control chars and anything that would break an S3 key or a
    DJ's filesystem when the file is later exported to a USB stick.
    """
    text = unicodedata.normalize("NFKC", (value or "").strip())
    text = text.replace("/", "-").replace("\\", "-")
    kept = []
    for ch in text:
        if ch in _KEY_ALLOWED_PUNCT:
            kept.append(ch)
            continue
        category = unicodedata.category(ch)
        # L* = letters (any script), N* = numbers, Mn/Mc = combining marks.
        if category[0] in ("L", "N") or category in ("Mn", "Mc"):
            kept.append(ch)
    text = re.sub(r"\s{2,}", " ", "".join(kept)).strip()
    return text[:120] or fallback


def build_object_key(user_id: str, genre: str | None, artist: str | None, title: str | None) -> str:
    """Compose the R2 object key: ``{user_id}/{genere}/{artista} - {titolo}.mp3``.

    Matches the layout mandated by the roadmap (Sezione 3, Task 3.1). Missing
    metadata degrades to safe placeholders instead of producing a broken key.
    """
    user_seg = _slug_segment(user_id, "user")
    genre_seg = _slug_segment(genre, "Unknown")
    artist_seg = _slug_segment(artist, "Unknown Artist")
    title_seg = _slug_segment(title, "Unknown Title")
    return f"{user_seg}/{genre_seg}/{artist_seg} - {title_seg}.mp3"


# --- client -----------------------------------------------------------------

# boto3 clients are thread-safe for calls but we build lazily and cache one so
# the download workers don't each pay client-construction cost.
_client = None
_client_lock = threading.Lock()


def _get_client():
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:
            return _client
        import boto3
        from botocore.config import Config

        _client = boto3.client(
            "s3",
            endpoint_url=_endpoint_url(),
            aws_access_key_id=_env("DROPS_R2_ACCESS_KEY_ID"),
            aws_secret_access_key=_env("DROPS_R2_SECRET_ACCESS_KEY"),
            # R2 ignores regions but SigV4 requires one; "auto" is Cloudflare's
            # documented value. virtual-addressing is not supported by R2, so
            # force path-style to avoid bucket-in-hostname requests.
            region_name="auto",
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
        return _client


class R2Error(RuntimeError):
    """Raised when an R2 operation fails while R2 is configured."""


def upload_file(local_path: str, key: str, *, content_type: str = "audio/mpeg") -> str:
    """Upload ``local_path`` to ``key`` in the R2 bucket. Returns the key.

    Raises R2Error on failure (callers treat upload as best-effort and keep the
    local file if this raises).
    """
    if not is_configured():
        raise R2Error("R2 not configured")
    client = _get_client()
    try:
        client.upload_file(
            Filename=local_path,
            Bucket=bucket_name(),
            Key=key,
            ExtraArgs={"ContentType": content_type},
        )
    except Exception as exc:  # botocore raises many concrete types
        raise R2Error(f"upload failed: {type(exc).__name__}") from exc
    logger.info("r2 upload ok key=%s", key)
    return key


def generate_presigned_url(key: str, *, expires_in: int | None = None) -> str:
    """Return a time-limited GET url for ``key`` (default TTL from env, ~1h)."""
    if not is_configured():
        raise R2Error("R2 not configured")
    ttl = expires_in if expires_in is not None else presign_ttl_seconds()
    ttl = max(60, min(ttl, 7 * 24 * 3600))
    client = _get_client()
    try:
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket_name(), "Key": key},
            ExpiresIn=ttl,
        )
    except Exception as exc:
        raise R2Error(f"presign failed: {type(exc).__name__}") from exc


def delete_object(key: str) -> None:
    """Best-effort delete of an object (used when a track record is removed)."""
    if not is_configured():
        return
    client = _get_client()
    try:
        client.delete_object(Bucket=bucket_name(), Key=key)
    except Exception as exc:
        logger.info("r2 delete skip key=%s detail=%r", key, str(exc)[:120])


def reset_client_for_tests() -> None:
    """Drop the cached client so tests can swap env/credentials."""
    global _client
    with _client_lock:
        _client = None
