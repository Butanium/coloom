"""Server API tests: REST + generation (against the fake completions server) + WS."""

import pytest
from coloom.config import ColoomConfig, EndpointConfig, Preset
from coloom.server.app import create_app
from coloom.store import WeaveStore
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, fake_openai_url):
    url, _ = fake_openai_url
    config = ColoomConfig(
        endpoints={
            "fake": EndpointConfig(
                base_url=url,
                model="gpt-4-base",
                params={"temperature": 1.0, "max_tokens": 24, "logprobs": 5, "n": 2},
            ),
            "dead": EndpointConfig(base_url="http://127.0.0.1:1", model="x"),
            "keyless": EndpointConfig(
                base_url=url, model="x", api_key_env="COLOOM_TEST_UNSET_KEY"
            ),
        },
        presets={"default": Preset(endpoint="fake")},
        default_preset="default",
    )
    store = WeaveStore(tmp_path / "server.sqlite")
    with TestClient(create_app(store, config)) as c:
        yield c
    store.close()


def make_weave(client, **kwargs) -> str:
    resp = client.post("/weaves", json={"title": "t", **kwargs})
    assert resp.status_code == 201
    return resp.json()["id"]


def add(client, wid, text, parent=None, **kwargs):
    resp = client.post(
        f"/weaves/{wid}/nodes", json={"text": text, "parent_id": parent, **kwargs}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_weave_crud(client):
    wid = make_weave(client)
    assert client.get(f"/weaves/{wid}").json()["title"] == "t"
    assert len(client.get("/weaves").json()) == 1
    assert client.delete(f"/weaves/{wid}").status_code == 204
    assert client.get(f"/weaves/{wid}").status_code == 404


def test_add_read_active_flow(client):
    wid = make_weave(client)
    root = add(
        client, wid, "Once", set_active=True, creator={"type": "human", "label": "clem"}
    )
    child = add(client, wid, " upon", parent=root["id"], set_active=True)

    active = client.get(f"/weaves/{wid}/active").json()
    assert active["content"] == "Once upon"
    assert active["path"] == [root["id"], child["id"]]
    assert active["nodes"][0]["creator"]["label"] == "clem"

    assert client.put(f"/weaves/{wid}/active", json={"node_id": root["id"]}).json() == {
        "path": [root["id"]]
    }
    tree = client.get(f"/weaves/{wid}").json()
    assert tree["roots"] == [root["id"]]
    assert tree["nodes"][root["id"]]["children"] == [child["id"]]


def test_split_and_bookmark_and_remove(client):
    wid = make_weave(client)
    root = add(client, wid, "hello world")
    resp = client.post(f"/weaves/{wid}/nodes/{root['id']}/split", json={"at": 5})
    assert resp.status_code == 200
    assert resp.json()["head"]["content"]["text"] == "hello"

    assert (
        client.put(
            f"/weaves/{wid}/nodes/{root['id']}/bookmark", json={"bookmarked": True}
        ).status_code
        == 200
    )
    assert client.get(f"/weaves/{wid}").json()["bookmarks"] == [root["id"]]

    removed = client.delete(f"/weaves/{wid}/nodes/{root['id']}").json()["removed"]
    assert len(removed) == 2  # head + tail cascade


def test_split_bad_offset_is_400(client):
    wid = make_weave(client)
    root = add(client, wid, "ab")
    assert (
        client.post(
            f"/weaves/{wid}/nodes/{root['id']}/split", json={"at": 0}
        ).status_code
        == 400
    )


def test_gen_creates_token_children(client, fake_openai_url):
    _, fake_app = fake_openai_url
    wid = make_weave(client)
    root = add(client, wid, "The loom hummed softly as the weaver", set_active=True)

    resp = client.post(f"/weaves/{wid}/gen", json={"set_active": True})
    assert resp.status_code == 201, resp.text
    nodes = resp.json()
    assert len(nodes) == 2  # fixture has n=2 choices
    for node in nodes:
        assert node["parents"] == [root["id"]]
        assert node["content"]["type"] == "tokens"
        assert node["content"]["tokens"][0]["logprob"] is not None
        assert node["creator"]["type"] == "model"
    # prompt was the thread content up to the parent node
    assert (
        fake_app.state.received[-1]["prompt"] == "The loom hummed softly as the weaver"
    )
    # set_active made the first generated node the tip
    active = client.get(f"/weaves/{wid}/active").json()
    assert active["path"] == [root["id"], nodes[0]["id"]]


def test_gen_without_active_path_is_400(client):
    wid = make_weave(client)
    assert client.post(f"/weaves/{wid}/gen", json={}).status_code == 400


def test_gen_unknown_preset_is_400(client):
    wid = make_weave(client)
    add(client, wid, "x", set_active=True)
    resp = client.post(f"/weaves/{wid}/gen", json={"preset": "nope"})
    assert resp.status_code == 400


def test_gen_unreachable_endpoint_is_502(client):
    wid = make_weave(client)
    add(client, wid, "x", set_active=True)
    resp = client.post(
        f"/weaves/{wid}/gen", json={"preset": "dead", "params": {"timeout": 2}}
    )
    assert resp.status_code == 502
    assert "failed" in resp.json()["detail"]


def test_gen_missing_api_key_env_is_502(client):
    wid = make_weave(client)
    add(client, wid, "x", set_active=True)
    resp = client.post(f"/weaves/{wid}/gen", json={"preset": "keyless"})
    assert resp.status_code == 502
    assert "COLOOM_TEST_UNSET_KEY" in resp.json()["detail"]


async def test_slow_subscriber_dropped_without_blocking_others(tmp_path, monkeypatch):
    # WS transports buffer in tests (no TCP backpressure), so exercise the hub
    # directly: a subscriber that never drains overflows its bounded queue and
    # is dropped; the healthy subscriber still receives everything.
    from coloom.models import Snippet
    from coloom.server.app import EventHub

    monkeypatch.setattr(EventHub, "QUEUE_SIZE", 2)
    store = WeaveStore(tmp_path / "slow.sqlite")
    hub = EventHub(store)
    stalled = hub.subscribe(None)
    healthy = hub.subscribe(None)

    wid = store.create_weave().id
    received = []

    def drain(queue):
        while not queue.empty():
            received.append(queue.get_nowait())

    ids = []
    for i in range(5):  # healthy drains after each push; stalled never does
        ids.append(store.add_node(wid, Snippet(text=f"n{i}")).id)
        await hub.push_new()
        drain(healthy)

    assert stalled not in hub.subscribers  # overflowed at the 3rd event -> dropped
    assert healthy in hub.subscribers
    assert [
        e["payload"]["node_id"] for e in received if e["type"] == "node_added"
    ] == ids
    store.close()


def test_events_polling_cursor(client):
    wid = make_weave(client)
    resp = client.get("/events").json()
    cursor = resp["cursor"]
    assert [e["type"] for e in resp["events"]] == ["weave_created"]

    add(client, wid, "x")
    newer = client.get(f"/events?since={cursor}").json()
    assert [e["type"] for e in newer["events"]] == ["node_added"]
    assert newer["cursor"] > cursor
    # cursor is stable when nothing new
    assert client.get(f"/events?since={newer['cursor']}").json()["events"] == []


def test_websocket_broadcast(client):
    wid = make_weave(client)
    with client.websocket_connect(f"/ws?weave_id={wid}") as ws:
        node = add(client, wid, "hi", set_active=True)
        event = ws.receive_json()
        assert event["type"] == "node_added"
        assert event["payload"]["node_id"] == node["id"]
        assert ws.receive_json()["type"] == "active_changed"


def test_websocket_filter_excludes_other_weaves(client):
    wid_a = make_weave(client)
    wid_b = make_weave(client)
    with client.websocket_connect(f"/ws?weave_id={wid_a}") as ws:
        add(client, wid_b, "other")  # should NOT arrive
        add(client, wid_a, "mine")
        event = ws.receive_json()
        assert event["weave_id"] == wid_a
        assert event["type"] == "node_added"
