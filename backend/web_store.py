import hashlib
import sqlite3
import time
from pathlib import Path

# A job counts against queue capacity from the instant it's recognized until
# it's terminal (ready/error) - "queued" no longer exists as a distinct
# status because recognition now happens synchronously before insert.
ACTIVE_STATUSES = ("recognized", "enriching", "downloading")


class WebStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    created_at REAL NOT NULL, expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, status TEXT NOT NULL,
                    source_url TEXT NOT NULL, format TEXT NOT NULL, quality TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL, expires_at REAL NOT NULL,
                    title TEXT, artist TEXT, cover_url TEXT, raw_title TEXT, duration INTEGER,
                    label TEXT, year INTEGER, country TEXT, catalog_no TEXT, style TEXT, discogs_url TEXT,
                    bpm REAL, bpm_confidence REAL, source TEXT,
                    filename TEXT, file_path TEXT, size INTEGER, error TEXT
                );
                CREATE INDEX IF NOT EXISTS jobs_owner_id ON jobs(owner, id);
                CREATE TABLE IF NOT EXISTS login_attempts (
                    client_key TEXT NOT NULL, attempted_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS login_attempts_key_time
                    ON login_attempts(client_key, attempted_at);
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def token_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def create_session(self, token: str, owner: str, ttl: int):
        now = time.time()
        with self.connect() as db:
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (self.token_hash(token), owner, now, now + ttl))

    def session_owner(self, token: str) -> str | None:
        now = time.time()
        with self.connect() as db:
            row = db.execute("SELECT owner FROM sessions WHERE token_hash=? AND expires_at>?", (self.token_hash(token), now)).fetchone()
        return row["owner"] if row else None

    def session_lookup(self, token: str) -> dict | None:
        """Return {owner, expires_at} regardless of expiry, so callers can log why a session is rejected."""
        with self.connect() as db:
            row = db.execute("SELECT owner, expires_at FROM sessions WHERE token_hash=?", (self.token_hash(token),)).fetchone()
        return {"owner": row["owner"], "expires_at": row["expires_at"]} if row else None

    def touch_session(self, token: str, ttl: int) -> None:
        """Slide the session expiry forward on active use, up to ttl from now."""
        with self.connect() as db:
            db.execute("UPDATE sessions SET expires_at=? WHERE token_hash=?", (time.time() + ttl, self.token_hash(token)))

    def delete_session(self, token: str):
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (self.token_hash(token),))

    def create_job(self, job_id: str, owner: str, url: str, fmt: str, quality: str, ttl: int):
        # Placeholder status for test fixtures that seed a job mid-flight;
        # callers set the real status/fields they need via update_job.
        now = time.time()
        with self.connect() as db:
            db.execute("INSERT INTO jobs(id,owner,status,source_url,format,quality,created_at,updated_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)", (job_id, owner, "downloading", url, fmt, quality, now, now, now + ttl))

    def create_job_if_capacity(
        self, job_id: str, owner: str, url: str, fmt: str, quality: str, ttl: int, capacity: int, *,
        title: str | None = None, artist: str | None = None, cover_url: str | None = None,
        raw_title: str | None = None, duration: int | None = None,
    ) -> bool:
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute(f"SELECT COUNT(*) FROM jobs WHERE status IN ({','.join('?' * len(ACTIVE_STATUSES))})", ACTIVE_STATUSES).fetchone()[0]
            if active >= capacity:
                return False
            db.execute(
                "INSERT INTO jobs(id,owner,status,source_url,format,quality,created_at,updated_at,expires_at,title,artist,cover_url,raw_title,duration) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, owner, "recognized", url, fmt, quality, now, now, now + ttl, title, artist, cover_url, raw_title, duration),
            )
        return True

    def update_job(self, job_id: str, **values):
        values["updated_at"] = time.time()
        columns = ",".join(f"{key}=?" for key in values)
        with self.connect() as db:
            db.execute(f"UPDATE jobs SET {columns} WHERE id=?", (*values.values(), job_id))

    def get_job(self, job_id: str, owner: str):
        with self.connect() as db:
            return db.execute("SELECT * FROM jobs WHERE id=? AND owner=?", (job_id, owner)).fetchone()

    def get_job_by_id(self, job_id: str):
        """Owner-agnostic lookup for the trusted background worker (no HTTP request/owner in scope)."""
        with self.connect() as db:
            return db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def active_count(self) -> int:
        with self.connect() as db:
            return db.execute(f"SELECT COUNT(*) FROM jobs WHERE status IN ({','.join('?' * len(ACTIVE_STATUSES))})", ACTIVE_STATUSES).fetchone()[0]

    def expired_artifacts(self):
        with self.connect() as db:
            return db.execute("SELECT id,file_path FROM jobs WHERE expires_at<=?", (time.time(),)).fetchall()

    def interrupt_active_jobs(self, ttl: int) -> int:
        now = time.time()
        with self.connect() as db:
            cursor = db.execute(
                f"UPDATE jobs SET status='error', error='Download interrupted', updated_at=?, expires_at=MIN(expires_at, ?) WHERE status IN ({','.join('?' * len(ACTIVE_STATUSES))})",
                (now, now + ttl, *ACTIVE_STATUSES),
            )
        return cursor.rowcount

    def allow_login_attempt(self, client_key: str, limit: int, window: int) -> bool:
        now = time.time()
        cutoff = now - window
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM login_attempts WHERE attempted_at<=?", (cutoff,))
            count = db.execute("SELECT COUNT(*) FROM login_attempts WHERE client_key=?", (client_key,)).fetchone()[0]
            if count >= limit:
                return False
            db.execute("INSERT INTO login_attempts(client_key,attempted_at) VALUES(?,?)", (client_key, now))
        return True

    def clear_login_attempts(self, client_key: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM login_attempts WHERE client_key=?", (client_key,))

    def delete_expired(self):
        now = time.time()
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
            db.execute("DELETE FROM jobs WHERE expires_at<=?", (now,))
