"""Unit tests for folder_store.FolderStore (pure CRUD, no FastAPI)."""

from __future__ import annotations

from pathlib import Path

import folder_store


def test_create_folder_defaults_no_tracks(tmp_path: Path):
    store = folder_store.FolderStore(tmp_path / "state")
    row = store.create_folder(user_id="dj", name="Warmup Set")
    assert row["name"] == "Warmup Set"
    assert row["dominant_genre"] is None
    assert row["track_ids"] == []
    assert row["track_count"] == 0
    assert store.get(row["id"]) == row


def test_create_folder_with_initial_tracks(tmp_path: Path):
    store = folder_store.FolderStore(tmp_path / "state")
    row = store.create_folder(
        user_id="dj", name="Peak Time", dominant_genre="Techno", track_ids=["t1", "t2", "t1"],
    )
    assert row["track_ids"] == ["t1", "t2"]  # de-duped, order preserved
    assert row["track_count"] == 2


def test_list_for_user_scoped_and_ordered(tmp_path: Path):
    store = folder_store.FolderStore(tmp_path / "state")
    store.create_folder(user_id="dj", name="A", folder_id="f1")
    store.create_folder(user_id="other", name="B", folder_id="f2")
    store.create_folder(user_id="dj", name="C", folder_id="f3")
    ids = [row["id"] for row in store.list_for_user("dj")]
    assert set(ids) == {"f1", "f3"}
    assert store.list_for_user("nobody") == []


def test_rename_scoped_to_owner(tmp_path: Path):
    store = folder_store.FolderStore(tmp_path / "state")
    store.create_folder(user_id="dj", name="Old Name", folder_id="f1")
    assert store.rename("f1", user_id="someone-else", name="Hijacked") is None
    assert store.get("f1")["name"] == "Old Name"
    updated = store.rename("f1", user_id="dj", name="New Name")
    assert updated["name"] == "New Name"
    assert store.get("f1")["name"] == "New Name"


def test_rename_unknown_folder_returns_none(tmp_path: Path):
    store = folder_store.FolderStore(tmp_path / "state")
    assert store.rename("nope", user_id="dj", name="X") is None


def test_delete_scoped_to_owner_and_cascades_tracks(tmp_path: Path):
    store = folder_store.FolderStore(tmp_path / "state")
    store.create_folder(user_id="dj", name="Set", folder_id="f1", track_ids=["t1", "t2"])
    assert store.delete("f1", user_id="someone-else") is False
    assert store.get("f1") is not None
    assert store.delete("f1", user_id="dj") is True
    assert store.get("f1") is None
    # folder_tracks rows for f1 must be gone too, not just orphaned.
    with folder_store.Session(store.engine) as session:
        remaining = session.query(folder_store.FolderTrack).filter_by(folder_id="f1").all()
        assert remaining == []


def test_delete_unknown_folder_returns_false(tmp_path: Path):
    store = folder_store.FolderStore(tmp_path / "state")
    assert store.delete("nope", user_id="dj") is False


def test_add_track_is_idempotent(tmp_path: Path):
    store = folder_store.FolderStore(tmp_path / "state")
    store.create_folder(user_id="dj", name="Set", folder_id="f1")
    store.add_track("f1", "t1")
    store.add_track("f1", "t1")
    assert store.get("f1")["track_ids"] == ["t1"]


def test_remove_track_is_idempotent(tmp_path: Path):
    store = folder_store.FolderStore(tmp_path / "state")
    store.create_folder(user_id="dj", name="Set", folder_id="f1", track_ids=["t1"])
    store.remove_track("f1", "t1")
    store.remove_track("f1", "t1")  # second call is a no-op, not an error
    assert store.get("f1")["track_ids"] == []
