"""Generation retry: transient upstream failures retried with backoff (each
retry emitting a `gen_retrying` event), non-transient failures surfacing
immediately, and the `retries` override riding the params merge chain.

Driven end-to-end through the real gpt-fake mock (its `fail_times`/`fail_key`/
`fail_status` body params) over real HTTP — own ports, own temp DBs."""

import socket
import threading
import time
import uuid

import coloom.inference as inference
import pytest
import uvicorn
from coloom.config import ColoomConfig, EndpointConfig
from coloom.inference import InferenceError, generate
from coloom.server.app import create_app
from coloom.store import WeaveStore
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    """Shrink the exponential backoff so exhaustion tests stay fast."""
    monkeypatch.setattr(inference, "BACKOFF_BASE", 0.005)
    monkeypatch.setattr(inference, "BACKOFF_CAP", 0.01)


@pytest.fixture(scope="module")
def gpt_fake():
    """The real gpt-fake app (with synthetic-failure mode) over real HTTP."""
    from coloom.fake_openai import build_app

    app = build_app(default_seed=0)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        assert time.time() < deadline, "gpt-fake server failed to start"
        time.sleep(0.01)
    yield f"http://127.0.0.1:{port}/v1", app
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def client(tmp_path, gpt_fake):
    url, _ = gpt_fake
    config = ColoomConfig(
        endpoints={
            "gpt-fake": EndpointConfig(
                base_url=url,
                model="gpt-fake",
                params={"temperature": 1.0, "max_tokens": 8, "logprobs": 3, "n": 1},
            )
        },
        default_preset="gpt-fake",
    )
    store = WeaveStore(tmp_path / "retry.sqlite")
    with TestClient(create_app(store, config)) as c:
        yield c
    store.close()


def setup_weave(client):
    """A profile generator + weave + root node; returns (generator, wid, root)."""
    client.put("/profiles/tester", json={"settings": {}})
    gens = client.get("/generators?profile=tester").json()
    gen = next(g for g in gens if g["name"] == "gpt-fake")
    wid = client.post("/weaves", json={"title": "retry"}).json()["id"]
    root = client.post(
        f"/weaves/{wid}/nodes", json={"text": f"prompt-{uuid.uuid4().hex}"}
    ).json()
    return gen, wid, root


def events_of(client, wid, type_):
    events = client.get(f"/events?weave_id={wid}").json()["events"]
    return [e for e in events if e["type"] == type_]


def upstream_bodies(fake_app, key):
    return [b for b in fake_app.state.received if b.get("fail_key") == key]


def test_transient_then_success_emits_gen_retrying(client, gpt_fake):
    _, fake_app = gpt_fake
    gen, wid, root = setup_weave(client)
    key = uuid.uuid4().hex
    resp = client.post(
        f"/weaves/{wid}/gen",
        json={
            "node_id": root["id"],
            "generator_id": gen["id"],
            "params": {"fail_times": 2, "fail_key": key, "seed": 1},
        },
    )
    assert resp.status_code == 201, resp.text
    assert len(resp.json()) == 1  # nodes created despite the two failures

    retrying = events_of(client, wid, "gen_retrying")
    assert [e["payload"]["attempt"] for e in retrying] == [1, 2]
    assert all(e["payload"]["max"] == 5 for e in retrying)  # server default
    assert all(e["payload"]["generator"] == "gpt-fake" for e in retrying)
    assert all("HTTP 500" in e["payload"]["error"] for e in retrying)

    started = events_of(client, wid, "gen_started")
    finished = events_of(client, wid, "gen_finished")
    assert len(started) == 1 and len(finished) == 1
    assert finished[0]["payload"]["node_ids"]  # success, not error
    assert "error" not in finished[0]["payload"]
    # the whole sequence shares one gen_id and is ordered started < retries < finish
    gen_id = started[0]["payload"]["gen_id"]
    assert all(e["payload"]["gen_id"] == gen_id for e in retrying + finished)
    seqs = [started[0]["seq"], *(e["seq"] for e in retrying), finished[0]["seq"]]
    assert seqs == sorted(seqs)
    # initial attempt + 2 retries hit upstream
    assert len(upstream_bodies(fake_app, key)) == 3


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_transient_statuses_are_retried(client, gpt_fake, status):
    gen, wid, root = setup_weave(client)
    resp = client.post(
        f"/weaves/{wid}/gen",
        json={
            "node_id": root["id"],
            "generator_id": gen["id"],
            "params": {
                "fail_times": 1,
                "fail_status": status,
                "fail_key": uuid.uuid4().hex,
                "seed": 2,
            },
        },
    )
    assert resp.status_code == 201, resp.text
    retrying = events_of(client, wid, "gen_retrying")
    assert len(retrying) == 1
    assert f"HTTP {status}" in retrying[0]["payload"]["error"]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_non_transient_fails_immediately(client, gpt_fake, status):
    _, fake_app = gpt_fake
    gen, wid, root = setup_weave(client)
    key = uuid.uuid4().hex
    resp = client.post(
        f"/weaves/{wid}/gen",
        json={
            "node_id": root["id"],
            "generator_id": gen["id"],
            "params": {"fail_times": 5, "fail_status": status, "fail_key": key},
        },
    )
    assert resp.status_code == 502
    assert f"HTTP {status}" in resp.json()["detail"]
    assert events_of(client, wid, "gen_retrying") == []  # zero retries
    finished = events_of(client, wid, "gen_finished")
    assert len(finished) == 1 and f"HTTP {status}" in finished[0]["payload"]["error"]
    assert len(upstream_bodies(fake_app, key)) == 1  # exactly one upstream call


def test_exhaustion_after_max_retries(client, gpt_fake):
    _, fake_app = gpt_fake
    gen, wid, root = setup_weave(client)
    key = uuid.uuid4().hex
    resp = client.post(
        f"/weaves/{wid}/gen",
        json={
            "node_id": root["id"],
            "generator_id": gen["id"],
            "params": {"fail_times": 100, "fail_key": key},
        },
    )
    assert resp.status_code == 502
    assert "HTTP 500" in resp.json()["detail"]
    retrying = events_of(client, wid, "gen_retrying")
    assert [e["payload"]["attempt"] for e in retrying] == [1, 2, 3, 4, 5]
    assert all(e["payload"]["max"] == 5 for e in retrying)
    finished = events_of(client, wid, "gen_finished")
    assert len(finished) == 1 and "HTTP 500" in finished[0]["payload"]["error"]
    assert len(upstream_bodies(fake_app, key)) == 6  # initial + 5 retries


def test_retries_override_via_params(client, gpt_fake):
    _, fake_app = gpt_fake
    gen, wid, root = setup_weave(client)
    key = uuid.uuid4().hex
    resp = client.post(
        f"/weaves/{wid}/gen",
        json={
            "node_id": root["id"],
            "generator_id": gen["id"],
            "params": {"retries": 2, "fail_times": 100, "fail_key": key},
        },
    )
    assert resp.status_code == 502
    retrying = events_of(client, wid, "gen_retrying")
    assert [e["payload"]["attempt"] for e in retrying] == [1, 2]
    assert all(e["payload"]["max"] == 2 for e in retrying)
    assert len(upstream_bodies(fake_app, key)) == 3


def test_retries_zero_disables_retrying(client, gpt_fake):
    _, fake_app = gpt_fake
    gen, wid, root = setup_weave(client)
    key = uuid.uuid4().hex
    resp = client.post(
        f"/weaves/{wid}/gen",
        json={
            "node_id": root["id"],
            "generator_id": gen["id"],
            "params": {"retries": 0, "fail_times": 100, "fail_key": key},
        },
    )
    assert resp.status_code == 502
    assert events_of(client, wid, "gen_retrying") == []
    assert len(upstream_bodies(fake_app, key)) == 1


def test_retries_stripped_from_upstream_body(client, gpt_fake):
    _, fake_app = gpt_fake
    gen, wid, root = setup_weave(client)
    key = uuid.uuid4().hex
    resp = client.post(
        f"/weaves/{wid}/gen",
        json={
            "node_id": root["id"],
            "generator_id": gen["id"],
            "params": {"retries": 3, "fail_times": 1, "fail_key": key, "seed": 4},
        },
    )
    assert resp.status_code == 201, resp.text
    bodies = upstream_bodies(fake_app, key)
    assert len(bodies) == 2  # one failure, one retry
    assert all("retries" not in b for b in bodies)
    # attribution keeps the body actually sent upstream — no internal keys
    assert "retries" not in resp.json()[0]["creator"]["raw_request"]


def test_invalid_retries_value_rejected(client):
    gen, wid, root = setup_weave(client)
    resp = client.post(
        f"/weaves/{wid}/gen",
        json={
            "node_id": root["id"],
            "generator_id": gen["id"],
            "params": {"retries": -1},
        },
    )
    assert resp.status_code == 502
    assert "retries" in resp.json()["detail"]


async def test_transport_error_retried_with_callback():
    """Unit-level: connect failures are transient; on_retry sees each attempt."""
    calls = []

    async def on_retry(attempt, max_retries, error):
        calls.append((attempt, max_retries, error))

    endpoint = EndpointConfig(base_url="http://127.0.0.1:1", model="m")
    with pytest.raises(InferenceError, match="failed"):
        await generate(endpoint, "p", {"retries": 2}, timeout=2.0, on_retry=on_retry)
    assert [(a, m) for a, m, _ in calls] == [(1, 2), (2, 2)]
    assert all("failed" in err for _, _, err in calls)
