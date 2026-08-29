"""Durable catalog of Academy feedback-track submissions.

A student (the single authenticated owner, same auth model as the rest of
the app) uploads a WAV/MP3 track directly to Cloudflare R2 via a presigned
POST minted by the backend (see r2_storage.generate_presigned_post), then
calls back to mark the submission ready. This table is the source of truth
for that flow's state - the audio bytes themselves live on R2, never here
and never on Supabase (AGENTS.md: "Mai file audio su Supabase").

Deliberately its own module/table rather than folded into track_store.py:
submissions are a distinct domain (pending-upload lifecycle, focus_area,
lesson feedback) from the cloud DJ library, and keeping it separate is what
lets this become its own deployable service later without touching the
library catalog. Mirrors track_store.py's engine-selection logic exactly
(Postgres when DATABASE_URL is set, local SQLite fallback otherwise) via the
shared ``resolve_database_url`` helper - one DB, two tables, two modules.
"""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import BigInteger, Float, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from track_store import resolve_database_url

logger = logging.getLogger("drops.academy")

STATUS_PENDING = "pending"
STATUS_READY = "ready"


class Base(DeclarativeBase):
    pass


class Submission(Base):
    __tablename__ = "academy_submissions"

    submission_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    bpm: Mapped[float | None] = mapped_column(Float, nullable=True)
    bpm_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    bpm_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    genre: Mapped[str | None] = mapped_column(Text, nullable=True)
    focus_area: Mapped[str | None] = mapped_column(Text, nullable=True)
    filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    r2_key: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=STATUS_PENDING)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)

    def public(self) -> dict[str, Any]:
        return {
            "submission_id": self.submission_id,
            "user_id": self.user_id,
            "title": self.title,
            "bpm": self.bpm,
            "bpm_confidence": self.bpm_confidence,
            "bpm_source": self.bpm_source,
            "genre": self.genre,
            "focus_area": self.focus_area,
            "filename": self.filename,
            "content_type": self.content_type,
            "r2_key": self.r2_key,
            "size_bytes": self.size_bytes,
            "status": self.status,
            "created_at": self.created_at,
        }


class AcademyStore:
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
        logger.info("academy store ready backend=%s", "postgres" if is_postgres else "sqlite")

    def create_pending(
        self,
        *,
        user_id: str,
        r2_key: str,
        content_type: str,
        title: str | None = None,
        bpm: float | None = None,
        genre: str | None = None,
        focus_area: str | None = None,
        filename: str | None = None,
        submission_id: str | None = None,
    ) -> dict[str, Any]:
        submission_id = submission_id or str(uuid.uuid4())
        row = Submission(
            submission_id=submission_id,
            user_id=user_id,
            title=title,
            bpm=bpm,
            genre=genre,
            focus_area=focus_area,
            filename=filename,
            content_type=content_type,
            r2_key=r2_key,
            size_bytes=None,
            status=STATUS_PENDING,
            created_at=time.time(),
        )
        with Session(self.engine) as session:
            session.add(row)
            session.commit()
            session.refresh(row)
            return row.public()

    def get(self, submission_id: str) -> dict[str, Any] | None:
        with Session(self.engine) as session:
            row = session.get(Submission, submission_id)
            return row.public() if row else None

    def list_for_user(self, user_id: str, *, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        stmt = (
            select(Submission)
            .where(Submission.user_id == user_id)
            .order_by(Submission.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        with Session(self.engine) as session:
            return [row.public() for row in session.scalars(stmt)]

    def mark_ready(self, submission_id: str, *, size_bytes: int) -> dict[str, Any] | None:
        with Session(self.engine) as session:
            row = session.get(Submission, submission_id)
            if row is None:
                return None
            row.status = STATUS_READY
            row.size_bytes = size_bytes
            session.commit()
            session.refresh(row)
            return row.public()

    def update_bpm(
        self, submission_id: str, *, bpm: float, bpm_confidence: float | None = None, bpm_source: str | None = None,
    ) -> dict[str, Any] | None:
        """Overwrite the row's BPM with an automatically analyzed value,
        replacing whatever the student self-reported at submission time."""
        with Session(self.engine) as session:
            row = session.get(Submission, submission_id)
            if row is None:
                return None
            row.bpm = bpm
            row.bpm_confidence = bpm_confidence
            row.bpm_source = bpm_source
            session.commit()
            session.refresh(row)
            return row.public()

    def delete(self, submission_id: str, user_id: str | None = None) -> str | None:
        """Delete a submission row. Returns its r2_key so the caller can drop
        the object from R2. Scoped to ``user_id`` when given."""
        with Session(self.engine) as session:
            row = session.get(Submission, submission_id)
            if row is None or (user_id is not None and row.user_id != user_id):
                return None
            key = row.r2_key
            session.delete(row)
            session.commit()
            return key
