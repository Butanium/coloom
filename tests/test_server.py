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


def test_add_read_cursor_flow(client):
    wid = make_weave(client)
    root = add(
        client,
        wid,
        "Once",
        move_cursor="clem",
        creator={"type": "human", "label": "clem"},
    )
    child = add(client, wid, " upon", parent=root["id"], move_cursor="clem")

    thread = client.get(f"/weaves/{wid}/cursors/clem/thread").json()
    assert thread["content"] == "Once upon"
    assert thread["path"] == [root["id"], child["id"]]
    assert thread["nodes"][0]["creator"]["label"] == "clem"

    # anyone can move anyone's cursor; moved_by records the mover
    moved = client.put(
        f"/weaves/{wid}/cursors/clem",
        json={"node_id": root["id"], "moved_by": "agent"},
    ).json()
    assert moved["node_id"] == root["id"]
    assert moved["moved_by"] == "agent"
    assert set(client.get(f"/weaves/{wid}/cursors").json()) == {"clem"}

    assert client.delete(f"/weaves/{wid}/cursors/clem").status_code == 204
    assert client.get(f"/weaves/{wid}/cursors").json() == {}
    assert client.get(f"/weaves/{wid}/cursors/clem/thread").status_code == 404

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
    root = add(client, wid, "The loom hummed softly as the weaver", move_cursor="agent")

    resp = client.post(
        f"/weaves/{wid}/gen", json={"cursor": "agent", "move_cursor": True}
    )
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
    # move_cursor moved the agent's cursor to the first generated node
    thread = client.get(f"/weaves/{wid}/cursors/agent/thread").json()
    assert thread["path"] == [root["id"], nodes[0]["id"]]


def test_gen_without_node_or_cursor_is_400(client):
    wid = make_weave(client)
    assert client.post(f"/weaves/{wid}/gen", json={}).status_code == 400


def test_gen_with_missing_cursor_is_404(client):
    wid = make_weave(client)
    add(client, wid, "x")
    assert client.post(f"/weaves/{wid}/gen", json={"cursor": "ghost"}).status_code == 404


def test_gen_move_cursor_without_cursor_is_400(client):
    wid = make_weave(client)
    root = add(client, wid, "x")
    resp = client.post(
        f"/weaves/{wid}/gen", json={"node_id": root["id"], "move_cursor": True}
    )
    assert resp.status_code == 400


def test_gen_unknown_preset_is_400(client):
    wid = make_weave(client)
    root = add(client, wid, "x")
    resp = client.post(
        f"/weaves/{wid}/gen", json={"node_id": root["id"], "preset": "nope"}
    )
    assert resp.status_code == 400


def test_add_node_with_typed_content(client):
    """Counterfactual branches POST full Tokens content, not just text."""
    wid = make_weave(client)
    root = add(client, wid, "x")
    resp = client.post(
        f"/weaves/{wid}/nodes",
        json={
            "parent_id": root["id"],
            "content": {
                "type": "tokens",
                "tokens": [
                    {
                        "text": " maybe",
                        "logprob": -1.5,
                        "token_id": 42,
                        "top_logprobs": [{"text": " maybe", "logprob": -1.5}],
                    }
                ],
            },
            "creator": {"type": "model", "label": "gpt-4-base"},
        },
    )
    assert resp.status_code == 201, resp.text
    node = resp.json()
    assert node["content"]["type"] == "tokens"
    assert node["content"]["tokens"][0]["logprob"] == -1.5
    fetched = client.get(f"/weaves/{wid}/nodes/{node['id']}").json()
    assert fetched["content"]["tokens"][0]["token_id"] == 42


def test_update_weave(client):
    wid = make_weave(client)
    resp = client.patch(
        f"/weaves/{wid}", json={"description": "notes!", "metadata": {"k": "v"}}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "t"  # unchanged
    assert body["description"] == "notes!"
    assert body["metadata"] == {"k": "v"}
    assert client.patch("/weaves/nope", json={"title": "x"}).status_code == 404


def test_gen_emits_presence_events(client):
    wid = make_weave(client)
    root = add(client, wid, "x")
    resp = client.post(
        f"/weaves/{wid}/gen", json={"node_id": root["id"], "cursor": "claude"}
    )
    assert resp.status_code == 201
    events = client.get(f"/events?weave_id={wid}").json()["events"]
    types = [e["type"] for e in events]
    assert "gen_started" in types and "gen_finished" in types
    started = next(e for e in events if e["type"] == "gen_started")
    finished = next(e for e in events if e["type"] == "gen_finished")
    assert started["payload"]["requester"] == "claude"
    assert started["payload"]["node_id"] == root["id"]
    assert len(finished["payload"]["node_ids"]) == 2
    # failure path also closes the indicator
    client.post(
        f"/weaves/{wid}/gen",
        json={"node_id": root["id"], "preset": "dead", "params": {"timeout": 2}},
    )
    events = client.get(f"/events?weave_id={wid}").json()["events"]
    errors = [e for e in events if e["type"] == "gen_finished" and "error" in e["payload"]]
    assert len(errors) == 1


def test_list_presets(client):
    resp = client.get("/presets")
    assert resp.status_code == 200
    body = resp.json()
    assert body["default_preset"] == "default"
    # the "default" preset shadows nothing; raw endpoints are selectable too
    assert set(body["presets"]) == {"default", "fake", "dead", "keyless"}
    assert body["presets"]["default"]["model"] == "gpt-4-base"
    assert body["presets"]["default"]["params"]["n"] == 2  # merged endpoint params


def test_gen_unreachable_endpoint_is_502(client):
    wid = make_weave(client)
    root = add(client, wid, "x")
    resp = client.post(
        f"/weaves/{wid}/gen",
        json={"node_id": root["id"], "preset": "dead", "params": {"timeout": 2}},
    )
    assert resp.status_code == 502
    assert "failed" in resp.json()["detail"]


def test_gen_missing_api_key_env_is_502(client):
    wid = make_weave(client)
    root = add(client, wid, "x")
    resp = client.post(
        f"/weaves/{wid}/gen", json={"node_id": root["id"], "preset": "keyless"}
    )
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
        node = add(client, wid, "hi", move_cursor="clem")
        event = ws.receive_json()
        assert event["type"] == "node_added"
        assert event["payload"]["node_id"] == node["id"]
        cursor_event = ws.receive_json()
        assert cursor_event["type"] == "cursor_moved"
        assert cursor_event["payload"]["name"] == "clem"


def test_websocket_filter_excludes_other_weaves(client):
    wid_a = make_weave(client)
    wid_b = make_weave(client)
    with client.websocket_connect(f"/ws?weave_id={wid_a}") as ws:
        add(client, wid_b, "other")  # should NOT arrive
        add(client, wid_a, "mine")
        event = ws.receive_json()
        assert event["weave_id"] == wid_a
        assert event["type"] == "node_added"


def test_client_header_becomes_event_origin(client):
    """X-Coloom-Client on a mutation is stamped into its events' payloads as
    `origin` (incl. the bundled cursor move); requests without the header (CLI,
    old clients) emit origin-less events."""
    wid = make_weave(client)
    since = client.get("/events").json()["cursor"]

    resp = client.post(
        f"/weaves/{wid}/nodes",
        json={"text": "mine", "parent_id": None, "move_cursor": "clem"},
        headers={"X-Coloom-Client": "tab-42"},
    )
    assert resp.status_code == 201
    events = client.get(f"/events?since={since}").json()["events"]
    assert {e["type"] for e in events} == {"node_added", "cursor_moved"}
    assert all(e["payload"]["origin"] == "tab-42" for e in events)

    add(client, wid, "headerless")  # no header -> no origin key
    later = client.get(f"/events?since={since}").json()["events"]
    headerless = [e for e in later if e["type"] == "node_added"
                  and "origin" not in e["payload"]]
    assert len(headerless) == 1
