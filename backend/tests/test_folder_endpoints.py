"""Integration tests for GET/POST/PATCH/DELETE /api/v1/folders.

No R2 involved - folders are pure catalog rows - so these use plain
make_client (no fake_r2 fixture needed).
"""

from __future__ import annotations


def test_list_requires_auth(make_client):
    client, _ = make_client()
    client.cookies.clear()
    resp = client.get("/api/v1/folders")
    assert resp.status_code == 401


def test_list_empty_by_default(make_client):
    client, _ = make_client()
    resp = client.get("/api/v1/folders")
    assert resp.status_code == 200
    assert resp.json() == {"folders": []}


def test_list_scoped_to_owner(make_client):
    client, app = make_client()
    app.state.folders.create_folder(user_id="dj", name="Mine", folder_id="f1")
    app.state.folders.create_folder(user_id="other", name="Not mine", folder_id="f2")
    resp = client.get("/api/v1/folders")
    assert resp.status_code == 200
    names = [f["name"] for f in resp.json()["folders"]]
    assert names == ["Mine"]


def test_create_requires_auth(make_client):
    client, _ = make_client()
    client.cookies.clear()
    resp = client.post("/api/v1/folders", json={"name": "Warmup"})
    assert resp.status_code == 401


def test_create_rejects_empty_name(make_client):
    client, _ = make_client()
    resp = client.post("/api/v1/folders", json={"name": "   "})
    assert resp.status_code == 400


def test_create_success(make_client):
    client, app = make_client()
    resp = client.post(
        "/api/v1/folders", json={"name": "Peak Time", "dominant_genre": "Techno", "track_ids": ["t1", "t2"]},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Peak Time"
    assert body["dominant_genre"] == "Techno"
    assert body["track_ids"] == ["t1", "t2"]
    assert body["user_id"] == "dj"
    assert app.state.folders.get(body["id"]) is not None


def test_create_without_track_ids_defaults_empty(make_client):
    client, _ = make_client()
    resp = client.post("/api/v1/folders", json={"name": "Empty Crate"})
    assert resp.status_code == 201
    assert resp.json()["track_ids"] == []


def test_rename_requires_auth(make_client):
    client, app = make_client()
    app.state.folders.create_folder(user_id="dj", name="Old", folder_id="f1")
    client.cookies.clear()
    resp = client.patch("/api/v1/folders/f1", json={"name": "New"})
    assert resp.status_code == 401


def test_rename_success(make_client):
    client, app = make_client()
    app.state.folders.create_folder(user_id="dj", name="Old", folder_id="f1")
    resp = client.patch("/api/v1/folders/f1", json={"name": "New Name"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "New Name"
    assert app.state.folders.get("f1")["name"] == "New Name"


def test_rename_rejects_empty_name(make_client):
    client, app = make_client()
    app.state.folders.create_folder(user_id="dj", name="Old", folder_id="f1")
    resp = client.patch("/api/v1/folders/f1", json={"name": ""})
    assert resp.status_code == 400
    assert app.state.folders.get("f1")["name"] == "Old"


def test_rename_unknown_folder_404(make_client):
    client, _ = make_client()
    resp = client.patch("/api/v1/folders/nonexistent", json={"name": "New"})
    assert resp.status_code == 404


def test_rename_another_owners_folder_404(make_client):
    client, app = make_client()
    app.state.folders.create_folder(user_id="someone-else", name="Not mine", folder_id="f1")
    resp = client.patch("/api/v1/folders/f1", json={"name": "Hijacked"})
    assert resp.status_code == 404
    assert app.state.folders.get("f1")["name"] == "Not mine"


def test_delete_requires_auth(make_client):
    client, app = make_client()
    app.state.folders.create_folder(user_id="dj", name="Gone", folder_id="f1")
    client.cookies.clear()
    resp = client.delete("/api/v1/folders/f1")
    assert resp.status_code == 401


def test_delete_success(make_client):
    client, app = make_client()
    app.state.folders.create_folder(user_id="dj", name="Gone", folder_id="f1")
    resp = client.delete("/api/v1/folders/f1")
    assert resp.status_code == 200
    assert resp.json() == {"status": "deleted", "id": "f1"}
    assert app.state.folders.get("f1") is None


def test_delete_unknown_folder_404(make_client):
    client, _ = make_client()
    resp = client.delete("/api/v1/folders/nonexistent")
    assert resp.status_code == 404


def test_delete_another_owners_folder_404(make_client):
    client, app = make_client()
    app.state.folders.create_folder(user_id="someone-else", name="Not mine", folder_id="f1")
    resp = client.delete("/api/v1/folders/f1")
    assert resp.status_code == 404
    assert app.state.folders.get("f1") is not None
