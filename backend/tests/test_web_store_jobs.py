from __future__ import annotations

from web_store import WebStore


def test_uploading_counts_against_capacity(tmp_path):
    store = WebStore(tmp_path / "state.sqlite3")
    store.create_job("uploading", "dj", "https://example.com/a", "audio", "320", 3600)
    store.update_job("uploading", status="uploading")

    assert store.active_count() == 1
    assert not store.create_job_if_capacity(
        "next", "dj", "https://example.com/b", "audio", "320", 3600, 1,
    )


def test_restart_recovery_interrupts_uploading(tmp_path):
    store = WebStore(tmp_path / "state.sqlite3")
    store.create_job("uploading", "dj", "https://example.com/a", "audio", "320", 3600)
    store.update_job("uploading", status="uploading")

    assert store.interrupt_active_jobs(3600) == 1
    row = store.get_job_by_id("uploading")
    assert row["status"] == "error"
    assert row["error"] == "Download interrupted"
