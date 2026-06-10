"""Profiles: server-stored per-person client settings (opaque JSON blobs)."""

import pytest
from fastapi.testclient import TestClient

from coloom.server.app import create_app
from coloom.store import WeaveStore


@pytest.fixture()
def client():
    store = WeaveStore(":memory:")
    app = create_app(store)
    with TestClient(app) as c:
        yield c
    store.close()


def test_profile_crud_round_trip(client):
    assert client.get("/profiles").json() == []

    settings = {
        "keybindings": {"generate_at_cursor": "Ctrl+Space"},
        "ui": {"sidebarTab": "tree", "sidebarWidth": 320},
        "activeGenerators": [{"kind": "preset", "id": "default"}],
    }
    r = client.put("/profiles/clement", json={"settings": settings})
    assert r.status_code == 200
    assert r.json()["settings"] == settings

    names = [p["name"] for p in client.get("/profiles").json()]
    assert names == ["clement"]

    got = client.get("/profiles/clement").json()
    assert got["settings"] == settings
    assert got["created"] and got["updated"]


def test_profile_upsert_replaces_settings(client):
    client.put("/profiles/p", json={"settings": {"a": 1}})
    client.put("/profiles/p", json={"settings": {"b": 2}})
    assert client.get("/profiles/p").json()["settings"] == {"b": 2}
    assert len(client.get("/profiles").json()) == 1


def test_profile_delete_is_soft_and_resurrectable(client):
    """DELETE hides the profile from the list but keeps its settings; logging
    in with the same name (PUT) brings everything back."""
    client.put("/profiles/gone", json={"settings": {"keep": "me"}})
    assert client.delete("/profiles/gone").status_code == 204

    # hidden from the gate's list…
    assert [p["name"] for p in client.get("/profiles").json()] == []
    # …but the row (and settings) survive, flagged inactive
    got = client.get("/profiles/gone")
    assert got.status_code == 200
    assert got.json()["active"] is False
    assert got.json()["settings"] == {"keep": "me"}
    # deleting again is still fine (idempotent on a soft-deleted row)
    assert client.delete("/profiles/gone").status_code == 204

    # resurrection: a PUT (what login does for an inactive profile) reactivates
    client.put("/profiles/gone", json={"settings": {"keep": "me"}})
    assert [p["name"] for p in client.get("/profiles").json()] == ["gone"]
    assert client.get("/profiles/gone").json()["active"] is True

    # a never-created name still 404s
    assert client.get("/profiles/never-existed").status_code == 404
    assert client.delete("/profiles/never-existed").status_code == 404


def test_profiles_table_migrates_active_column(tmp_path):
    """A database created before the soft-delete column existed gets the
    `active` column added on open, with existing rows defaulting to active."""
    import sqlite3

    db = tmp_path / "old.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE profiles ("
        " name TEXT PRIMARY KEY, settings TEXT NOT NULL DEFAULT '{}',"
        " created TEXT NOT NULL, updated TEXT NOT NULL);"
        "INSERT INTO profiles VALUES ('veteran', '{\"old\": true}', 't0', 't0');"
    )
    conn.commit()
    conn.close()

    store = WeaveStore(db)
    try:
        assert [p["name"] for p in store.list_profiles()] == ["veteran"]
        got = store.get_profile("veteran")
        assert got["active"] is True
        assert got["settings"] == {"old": True}
        store.delete_profile("veteran")
        assert store.list_profiles() == []
        assert store.get_profile("veteran")["active"] is False
    finally:
        store.close()


def test_profile_listing_sorted_and_independent(client):
    client.put("/profiles/zoe", json={"settings": {"x": 1}})
    client.put("/profiles/ada", json={"settings": {"y": 2}})
    assert [p["name"] for p in client.get("/profiles").json()] == ["ada", "zoe"]
    assert client.get("/profiles/ada").json()["settings"] == {"y": 2}
