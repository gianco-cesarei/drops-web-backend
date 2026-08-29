"""Unit tests for academy_store.AcademyStore (pure CRUD, no FastAPI)."""

from __future__ import annotations

from pathlib import Path

import academy_store


def test_create_pending_defaults_to_pending_status(tmp_path: Path):
    store = academy_store.AcademyStore(tmp_path / "state")
    row = store.create_pending(
        user_id="dj", r2_key="academy/dj/sub-1/track.wav", content_type="audio/wav",
        title="My Track", bpm=126.0, genre="Minimal House", focus_area="Mixdown & Low-end",
        filename="track.wav", submission_id="sub-1",
    )
    assert row["status"] == academy_store.STATUS_PENDING
    assert row["size_bytes"] is None
    assert store.get("sub-1") == row


def test_mark_ready_sets_status_and_size(tmp_path: Path):
    store = academy_store.AcademyStore(tmp_path / "state")
    store.create_pending(
        user_id="dj", r2_key="academy/dj/sub-1/track.wav", content_type="audio/wav",
        filename="track.wav", submission_id="sub-1",
    )
    updated = store.mark_ready("sub-1", size_bytes=4_500_000)
    assert updated["status"] == academy_store.STATUS_READY
    assert updated["size_bytes"] == 4_500_000
    assert store.get("sub-1")["status"] == academy_store.STATUS_READY


def test_mark_ready_unknown_id_returns_none(tmp_path: Path):
    store = academy_store.AcademyStore(tmp_path / "state")
    assert store.mark_ready("nope", size_bytes=1) is None


def test_list_for_user_scoped_and_ordered(tmp_path: Path):
    store = academy_store.AcademyStore(tmp_path / "state")
    store.create_pending(user_id="dj", r2_key="k1", content_type="audio/mpeg", submission_id="a")
    store.create_pending(user_id="other", r2_key="k2", content_type="audio/mpeg", submission_id="b")
    store.create_pending(user_id="dj", r2_key="k3", content_type="audio/mpeg", submission_id="c")
    ids = [row["submission_id"] for row in store.list_for_user("dj")]
    assert set(ids) == {"a", "c"}
    assert store.list_for_user("nobody") == []


def test_delete_scoped_to_owner(tmp_path: Path):
    store = academy_store.AcademyStore(tmp_path / "state")
    store.create_pending(user_id="dj", r2_key="academy/dj/sub-1/t.mp3", content_type="audio/mpeg", submission_id="sub-1")
    assert store.delete("sub-1", user_id="someone-else") is None
    assert store.get("sub-1") is not None
    assert store.delete("sub-1", user_id="dj") == "academy/dj/sub-1/t.mp3"
    assert store.get("sub-1") is None
