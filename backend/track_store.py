"""Durable catalog of the user's cloud tracks, backed by Supabase Postgres.

This is the source of truth for the cloud library (Sezione 3). The audio bytes
live on Cloudflare R2; this table maps a ``track_id`` to its owner and its R2
object key, plus the DJ-relevant metadata (artist / title / genre / bpm).

Backend selection:
* ``DATABASE_URL`` set  -> Supabase Postgres (the production target). The classic
  ``postgres://`` / ``postgresql://`` SQLAlchemy URL is accepted; we normalise it
  to the psycopg2 driver and enable ``pool_pre_ping`` so a connection dropped by
  Supabase's pooler is transparently re-established.
* ``DATABASE_URL`` unset -> a local SQLite file under the state dir. Lets local
  dev and the test suite run with zero external services while exercising the
  exact same SQL/ORM code path.

The schema is created on first use (``create_all``) which is enough for a single
new table; a real migration tool (Alembic) can take over once the schema starts
evolving.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import String, Float, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

logger = logging.getLogger("drops.tracks")


class Base(DeclarativeBase):
    pass


class Track(Base):
    __tablename__ = "tracks"

    track_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    artist: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    genre: Mapped[str | None] = mapped_column(Text, nullable=True)
    r2_key: Mapped[str] = mapped_column(Text, nullable=False)
    bpm: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)

    def public(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
            "user_id": self.user_id,
            "artist": self.artist,
            "title": self.title,
            "genre": self.genre,
            "r2_key": self.r2_key,
            "bpm": self.bpm,
            "created_at": self.created_at,
        }


def _normalize_url(url: str) -> str:
    # SQLAlchemy dropped the bare "postgres://" alias; Supabase hands it out.
    if url.startswith("postgres://"):
        url = "postgresql+psycopg2://" + url[len("postgres://"):]
    elif url.startswith("postgresql://") and "+" not in url.split("://", 1)[0]:
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


def resolve_database_url(state_dir: Path, database_url: str | None = None) -> str:
    url = (database_url if database_url is not None else os.environ.get("DATABASE_URL", "")).strip()
    if url:
        return _normalize_url(url)
    state_dir.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{state_dir / 'tracks.sqlite3'}"


class TrackStore:
    def __init__(self, state_dir: Path, database_url: str | None = None):
        self.url = resolve_database_url(state_dir, database_url)
        self.is_postgres = self.url.startswith("postgresql")
        engine_kwargs: dict[str, Any] = {"future": True}
        if self.is_postgres:
            # Survive Supabase pooler dropping idle connections.
            engine_kwargs["pool_pre_ping"] = True
            engine_kwargs["pool_recycle"] = 1800
        else:
            # A single SQLite file shared across the worker threadpool.
            engine_kwargs["connect_args"] = {"check_same_thread": False}
        self.engine = create_engine(self.url, **engine_kwargs)
        Base.metadata.create_all(self.engine)
        logger.info("track store ready backend=%s", "postgres" if self.is_postgres else "sqlite")

    def create_track(
        self,
        *,
        user_id: str,
        r2_key: str,
        artist: str | None = None,
        title: str | None = None,
        genre: str | None = None,
        bpm: float | None = None,
        track_id: str | None = None,
    ) -> dict[str, Any]:
        track_id = track_id or str(uuid.uuid4())
        row = Track(
            track_id=track_id,
            user_id=user_id,
            artist=artist,
            title=title,
            genre=genre,
            r2_key=r2_key,
            bpm=bpm,
            created_at=time.time(),
        )
        with Session(self.engine) as session:
            session.add(row)
            session.commit()
            session.refresh(row)
            return row.public()

    def get_track(self, track_id: str) -> dict[str, Any] | None:
        with Session(self.engine) as session:
            row = session.get(Track, track_id)
            return row.public() if row else None

    def list_tracks(self, user_id: str, *, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        stmt = (
            select(Track)
            .where(Track.user_id == user_id)
            .order_by(Track.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        with Session(self.engine) as session:
            return [row.public() for row in session.scalars(stmt)]

    def update_bpm(self, track_id: str, bpm: float | None) -> bool:
        with Session(self.engine) as session:
            row = session.get(Track, track_id)
            if row is None:
                return False
            row.bpm = bpm
            session.commit()
            return True

    def delete_track(self, track_id: str, user_id: str | None = None) -> str | None:
        """Delete a track row. Returns the r2_key so the caller can drop the
        object from R2. When user_id is given the delete is scoped to that owner."""
        with Session(self.engine) as session:
            row = session.get(Track, track_id)
            if row is None or (user_id is not None and row.user_id != user_id):
                return None
            key = row.r2_key
            session.delete(row)
            session.commit()
            return key
