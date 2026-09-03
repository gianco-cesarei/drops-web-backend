import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def _positive_int(name: str, default: int) -> int:
    value = int(os.environ.get(name, default))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class WebSettings:
    username: str
    password_hash: str
    state_dir: Path
    allowed_origins: tuple[str, ...]
    cookie_secure: bool
    session_ttl_seconds: int
    artifact_ttl_seconds: int
    max_queued: int
    max_concurrent: int
    max_duration_seconds: int
    max_file_bytes: int
    academy_max_upload_bytes: int
    login_rate_limit: int
    login_rate_window_seconds: int
    environment: str = "production"
    allow_missing_origin: bool = False
    session_secret: str = ""
    database_url: str = ""
    file_token_ttl_seconds: int = 300

    def __post_init__(self) -> None:
        if self.environment not in {"production", "development", "test"}:
            raise ValueError("DROPS_WEB_ENV must be production, development, or test")
        if self.environment == "production" and not self.allowed_origins:
            raise ValueError("DROPS_WEB_ALLOWED_ORIGINS is required in production")
        for origin in self.allowed_origins:
            parsed = urlsplit(origin)
            try:
                port_is_valid = parsed.port is None or 1 <= parsed.port <= 65535
            except ValueError:
                port_is_valid = False
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or not parsed.hostname
                or not port_is_valid
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
                or "*" in origin
            ):
                raise ValueError(f"Invalid exact origin: {origin}")
        if self.environment == "production" and not self.session_secret:
            raise ValueError("DROPS_WEB_SESSION_SECRET is required in production")

    @classmethod
    def from_env(cls) -> "WebSettings":
        username = os.environ.get("DROPS_WEB_USERNAME", "").strip()
        password_hash = os.environ.get("DROPS_WEB_PASSWORD_HASH", "").strip()
        if not username or not password_hash:
            raise ValueError("DROPS_WEB_USERNAME and DROPS_WEB_PASSWORD_HASH are required")
        if not password_hash.startswith("$argon2id$"):
            raise ValueError("DROPS_WEB_PASSWORD_HASH must be an Argon2id hash")
        origins = tuple(x.strip() for x in os.environ.get("DROPS_WEB_ALLOWED_ORIGINS", "").split(",") if x.strip())
        state_dir = Path(os.environ.get("DROPS_WEB_STATE_DIR", Path(tempfile.gettempdir()) / "drops-web")).expanduser()
        return cls(
            username=username,
            password_hash=password_hash,
            state_dir=state_dir,
            allowed_origins=origins,
            cookie_secure=_bool("DROPS_WEB_COOKIE_SECURE", True),
            session_ttl_seconds=_positive_int("DROPS_WEB_SESSION_TTL_SECONDS", 86400),
            artifact_ttl_seconds=_positive_int("DROPS_WEB_ARTIFACT_TTL_SECONDS", 600),
            max_queued=_positive_int("DROPS_WEB_MAX_QUEUED", 100),
            max_concurrent=_positive_int("DROPS_WEB_MAX_CONCURRENT", 5),
            max_duration_seconds=_positive_int("DROPS_WEB_MAX_DURATION_SECONDS", 900),
            max_file_bytes=_positive_int("DROPS_WEB_MAX_FILE_BYTES", 100_000_000),
            # Separate cap from max_file_bytes on purpose: that one bounds
            # yt-dlp downloads, this one bounds Academy feedback-track direct
            # uploads (Sezione: DJ Lab / Academy). Same 100MB default, but the
            # two are independently tunable.
            academy_max_upload_bytes=_positive_int("DROPS_ACADEMY_MAX_UPLOAD_BYTES", 100_000_000),
            login_rate_limit=_positive_int("DROPS_WEB_LOGIN_RATE_LIMIT", 5),
            login_rate_window_seconds=_positive_int("DROPS_WEB_LOGIN_RATE_WINDOW_SECONDS", 60),
            environment=os.environ.get("DROPS_WEB_ENV", "production").strip().lower(),
            allow_missing_origin=_bool("DROPS_WEB_ALLOW_MISSING_ORIGIN", False),
            session_secret=os.environ.get("DROPS_WEB_SESSION_SECRET", "").strip(),
            # Supabase Postgres connection string. Empty -> TrackStore falls back
            # to a local SQLite file (dev/test), keeping the cloud catalog code
            # path identical without an external database.
            database_url=os.environ.get("DATABASE_URL", "").strip(),
            # Short-lived HMAC token that authorizes a single download's
            # /file endpoint without a session cookie, so the front can
            # consume audio cross-origin (Web Audio, zip export).
            file_token_ttl_seconds=_positive_int("DROPS_WEB_FILE_TOKEN_TTL_SECONDS", 300),
        )
