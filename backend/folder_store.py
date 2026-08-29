"""Durable catalog of the user's library folders ("crates"), backed by
Supabase Postgres when configured, a local SQLite file otherwise.

Two tables:
* ``folders`` - one row per folder (id, owner, name, dominant_genre).
* ``folder_tracks`` - a plain many-to-many join (folder_id, track_id).

``track_id`` deliberately has no SQLAlchemy ForeignKey to track_store.Track:
that table lives under a different DeclarativeBase/engine handle (see
track_store.py, academy_store.py - same pattern), so a real FK constraint
would either force a shared metadata object across otherwise-independent
modules or a raw string FK SQLAlchemy can't validate at the ORM level
anyway. Same loose-coupling trade-off the rest of this codebase already
makes; referential integrity for track_id is the caller's responsibility.

Mirrors track_store.py's engine-selection logic exactly via the shared
``resolve_database_url`` helper - one DB, backend selection driven by
whether DATABASE_URL (Supabase) is set.
"""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import Float, String, Text, create_engine, delete, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from track_store import resolve_database_url

logger = logging.getLogger("drops.folders")


class Base(DeclarativeBase):
    pass


class Folder(Base):
    __tablename__ = "folders"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    dominant_genre: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)


class FolderTrack(Base):
    __tablename__ = "folder_tracks"

    folder_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    track_id: Mapped[str] = mapped_column(String(64), primary_key=True)


class FolderStore:
    def __init__(self, state_dir: Path, database_url: str | None = None):
        self.url = resolve_database_url(state_dir, database_url)
        is_postgres = self.url.startswith("postgresql")
        engine_kwargs: dict[str, Any] = {"future": True}
        if is_postgres:
            engine_kwargs["pool_pre_ping"] = True
            engine_kwargs["pool_recycle"] = 1800
        else:
            engine_kwargs["connect_args"] = {"check_same_thread": False}
        self.engine = create_engine(self.url, **engine_kwargs)
        Base.metadata.create_all(self.engine)
        logger.info("folder store ready backend=%s", "postgres" if is_postgres else "sqlite")

    def _public(self, session: Session, folder: Folder) -> dict[str, Any]:
        track_ids = list(
            session.scalars(
                select(FolderTrack.track_id).where(FolderTrack.folder_id == folder.id)
            )
        )
        return {
            "id": folder.id,
            "user_id": folder.user_id,
            "name": folder.name,
            "dominant_genre": folder.dominant_genre,
            "created_at": folder.created_at,
            "track_ids": track_ids,
            "track_count": len(track_ids),
        }

    def create_folder(
        self,
        *,
        user_id: str,
        name: str,
        dominant_genre: str | None = None,
        track_ids: list[str] | None = None,
        folder_id: str | None = None,
    ) -> dict[str, Any]:
        folder_id = folder_id or str(uuid.uuid4())
        with Session(self.engine) as session:
            folder = Folder(
                id=folder_id, user_id=user_id, name=name,
                dominant_genre=dominant_genre, created_at=time.time(),
            )
            session.add(folder)
            for track_id in dict.fromkeys(track_ids or []):  # de-dupe, keep order
                session.add(FolderTrack(folder_id=folder_id, track_id=track_id))
            session.commit()
            session.refresh(folder)
            return self._public(session, folder)

    def get(self, folder_id: str) -> dict[str, Any] | None:
        with Session(self.engine) as session:
            folder = session.get(Folder, folder_id)
            return self._public(session, folder) if folder else None

    def list_for_user(self, user_id: str, *, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        stmt = (
            select(Folder)
            .where(Folder.user_id == user_id)
            .order_by(Folder.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        with Session(self.engine) as session:
            return [self._public(session, folder) for folder in session.scalars(stmt)]

    def rename(self, folder_id: str, *, user_id: str, name: str) -> dict[str, Any] | None:
        """Rename a folder. Scoped to ``user_id`` - a folder owned by someone
        else is treated as not found, same as the rest of the catalog stores."""
        with Session(self.engine) as session:
            folder = session.get(Folder, folder_id)
            if folder is None or folder.user_id != user_id:
                return None
            folder.name = name
            session.commit()
            session.refresh(folder)
            return self._public(session, folder)

    def delete(self, folder_id: str, *, user_id: str) -> bool:
        """Delete a folder and its folder_tracks membership rows. Returns
        False when the folder doesn't exist or isn't owned by ``user_id``."""
        with Session(self.engine) as session:
            folder = session.get(Folder, folder_id)
            if folder is None or folder.user_id != user_id:
                return False
            session.execute(delete(FolderTrack).where(FolderTrack.folder_id == folder_id))
            session.delete(folder)
            session.commit()
            return True

    def add_track(self, folder_id: str, track_id: str) -> None:
        """Idempotent: adding a track already in the folder is a no-op."""
        with Session(self.engine) as session:
            exists = session.get(FolderTrack, (folder_id, track_id))
            if exists is not None:
                return
            session.add(FolderTrack(folder_id=folder_id, track_id=track_id))
            session.commit()

    def remove_track(self, folder_id: str, track_id: str) -> None:
        with Session(self.engine) as session:
            row = session.get(FolderTrack, (folder_id, track_id))
            if row is None:
                return
            session.delete(row)
            session.commit()
