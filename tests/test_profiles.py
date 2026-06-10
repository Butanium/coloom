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


def test_profile_delete_and_404(client):
    client.put("/profiles/gone", json={"settings": {}})
    assert client.delete("/profiles/gone").status_code == 204
    assert client.get("/profiles/gone").status_code == 404
    assert client.delete("/profiles/gone").status_code == 404


def test_profile_listing_sorted_and_independent(client):
    client.put("/profiles/zoe", json={"settings": {"x": 1}})
    client.put("/profiles/ada", json={"settings": {"y": 2}})
    assert [p["name"] for p in client.get("/profiles").json()] == ["ada", "zoe"]
    assert client.get("/profiles/ada").json()["settings"] == {"y": 2}
