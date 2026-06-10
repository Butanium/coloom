"""Setups API tests: model/sampler CRUD, redaction, validation, and gen-with-sampler
end-to-end against the fake completions backend (verifying the merged request body)."""

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
        },
        presets={"default": Preset(endpoint="fake")},
        default_preset="default",
    )
    store = WeaveStore(tmp_path / "setups.sqlite")
    with TestClient(create_app(store, config)) as c:
        c.fake_url = url
        yield c
    store.close()


def make_model(client, **kwargs):
    body = {"name": "m", "base_url": client.fake_url, "model": "gpt-4-base", **kwargs}
    resp = client.post("/setups/models", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def make_sampler(client, model_id, **kwargs):
    body = {"name": "s", "model_setup_id": model_id, **kwargs}
    resp = client.post("/setups/samplers", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def make_weave(client):
    resp = client.post("/weaves", json={"title": "t"})
    assert resp.status_code == 201
    return resp.json()["id"]


def add(client, wid, text, **kwargs):
    resp = client.post(f"/weaves/{wid}/nodes", json={"text": text, **kwargs})
    assert resp.status_code == 201, resp.text
    return resp.json()


# ------------------------------------------------------------ CRUD


def test_setups_crud_roundtrip(client):
    assert client.get("/setups").json() == {"models": [], "samplers": []}
    model = make_model(client, params={"temperature": 0.8})
    sampler = make_sampler(client, model["id"], params={"temperature": 1.2})

    listed = client.get("/setups").json()
    assert [m["id"] for m in listed["models"]] == [model["id"]]
    assert [s["id"] for s in listed["samplers"]] == [sampler["id"]]
    assert listed["models"][0]["params"] == {"temperature": 0.8}
    assert listed["samplers"][0]["model_setup_id"] == model["id"]

    # patch a model: omitted fields stay, given ones change
    patched = client.patch(
        f"/setups/models/{model['id']}", json={"model": "gpt-4.5-base"}
    ).json()
    assert patched["model"] == "gpt-4.5-base"
    assert patched["name"] == "m"  # unchanged
    assert patched["params"] == {"temperature": 0.8}  # unchanged

    # patch a sampler
    patched_s = client.patch(
        f"/setups/samplers/{sampler['id']}", json={"params": {"temperature": 0.4}}
    ).json()
    assert patched_s["params"] == {"temperature": 0.4}

    # delete sampler then model
    assert client.delete(f"/setups/samplers/{sampler['id']}").status_code == 204
    assert client.delete(f"/setups/models/{model['id']}").status_code == 204
    assert client.get("/setups").json() == {"models": [], "samplers": []}


def test_unknown_id_is_404(client):
    assert client.patch("/setups/models/nope", json={"name": "x"}).status_code == 404
    assert client.delete("/setups/models/nope").status_code == 404
    assert client.patch("/setups/samplers/nope", json={"name": "x"}).status_code == 404
    assert client.delete("/setups/samplers/nope").status_code == 404


def test_api_key_redacted_in_responses(client):
    created = make_model(client, api_key="sk-secret-123")
    assert created["api_key"] == "***"  # POST response redacts
    listed = client.get("/setups").json()["models"][0]
    assert listed["api_key"] == "***"  # GET redacts
    patched = client.patch(
        f"/setups/models/{created['id']}", json={"name": "renamed"}
    ).json()
    assert patched["api_key"] == "***"  # PATCH redacts, key preserved


def test_api_key_clear_vs_unchanged(client):
    model = make_model(client, api_key="sk-secret")
    # omitted api_key = unchanged (still redacted)
    assert (
        client.patch(f"/setups/models/{model['id']}", json={"name": "x"}).json()[
            "api_key"
        ]
        == "***"
    )
    # explicit null clears it
    cleared = client.patch(
        f"/setups/models/{model['id']}", json={"api_key": None}
    ).json()
    assert cleared["api_key"] is None


def test_mutual_exclusion_is_400(client):
    resp = client.post(
        "/setups/models",
        json={
            "name": "m",
            "base_url": client.fake_url,
            "model": "x",
            "api_key": "sk-1",
            "api_key_env": "SOME_ENV",
        },
    )
    assert resp.status_code == 400
    # also on PATCH: setting api_key_env while api_key already set
    model = make_model(client, api_key="sk-1")
    resp = client.patch(
        f"/setups/models/{model['id']}", json={"api_key_env": "SOME_ENV"}
    )
    assert resp.status_code == 400


def test_both_keys_null_is_allowed(client):
    model = make_model(client)  # keyless endpoint (llama.cpp etc.)
    assert model["api_key"] is None
    assert model["api_key_env"] is None


def test_sampler_bad_model_reference_is_400(client):
    resp = client.post(
        "/setups/samplers", json={"name": "s", "model_setup_id": "ghost"}
    )
    assert resp.status_code == 400


def test_delete_referenced_model_is_409(client):
    model = make_model(client)
    make_sampler(client, model["id"])
    resp = client.delete(f"/setups/models/{model['id']}")
    assert resp.status_code == 409
    # still present after the blocked delete
    assert len(client.get("/setups").json()["models"]) == 1


# ------------------------------------------------------------ generation


def test_gen_with_sampler_merges_body(client, fake_openai_url):
    _, fake_app = fake_openai_url
    model = make_model(
        client, params={"temperature": 0.8, "max_tokens": 12, "logprobs": 5}
    )
    sampler = make_sampler(
        client,
        model["id"],
        # overrides temperature; adds an arbitrary pass-through flag
        params={"temperature": 1.2, "logit_bias": {"50256": -100}},
    )
    wid = make_weave(client)
    root = add(client, wid, "The loom", move_cursor="agent")

    resp = client.post(
        f"/weaves/{wid}/gen",
        json={"cursor": "agent", "sampler_id": sampler["id"], "params": {"n": 1}},
    )
    assert resp.status_code == 201, resp.text
    body = fake_app.state.received[-1]
    assert body["model"] == "gpt-4-base"
    assert body["temperature"] == 1.2  # sampler beats model setup
    assert body["max_tokens"] == 12  # from model setup
    assert body["logit_bias"] == {"50256": -100}  # arbitrary flag passes through
    assert body["n"] == 1  # request params win over everything
    assert body["prompt"] == "The loom"

    # raw_request stored on the node mirrors the merged body (no api key in it)
    nodes = resp.json()
    raw = nodes[0]["creator"]["raw_request"]
    assert raw["temperature"] == 1.2
    assert raw["logit_bias"] == {"50256": -100}
    assert "api_key" not in raw and "Authorization" not in raw


def test_two_samplers_attribute_different_params(client, fake_openai_url):
    model = make_model(client, params={"temperature": 1.0, "n": 1})
    wild = make_sampler(client, model["id"], name="wild", params={"temperature": 1.2})
    safe = make_sampler(client, model["id"], name="safe", params={"temperature": 0.4})
    wid = make_weave(client)
    root = add(client, wid, "seed", move_cursor="c")

    n_wild = client.post(
        f"/weaves/{wid}/gen", json={"node_id": root["id"], "sampler_id": wild["id"]}
    ).json()
    n_safe = client.post(
        f"/weaves/{wid}/gen", json={"node_id": root["id"], "sampler_id": safe["id"]}
    ).json()
    assert n_wild[0]["creator"]["raw_request"]["temperature"] == 1.2
    assert n_safe[0]["creator"]["raw_request"]["temperature"] == 0.4
    # both samplers' nodes hang off the same root (the captured fixture replays
    # 2 choices regardless of n, so each gen attaches >=1 sibling)
    tree = client.get(f"/weaves/{wid}").json()
    children = set(tree["nodes"][root["id"]]["children"])
    assert {n_wild[0]["id"], n_safe[0]["id"]} <= children


def test_gen_sampler_emits_sampler_name_in_events(client):
    model = make_model(client, params={"n": 1})
    sampler = make_sampler(client, model["id"], name="wild")
    wid = make_weave(client)
    root = add(client, wid, "x")
    resp = client.post(
        f"/weaves/{wid}/gen", json={"node_id": root["id"], "sampler_id": sampler["id"]}
    )
    assert resp.status_code == 201
    events = client.get(f"/events?weave_id={wid}").json()["events"]
    started = next(e for e in events if e["type"] == "gen_started")
    finished = next(e for e in events if e["type"] == "gen_finished")
    assert started["payload"]["sampler"] == "wild"
    assert finished["payload"]["sampler"] == "wild"


def test_gen_unknown_sampler_is_404(client):
    wid = make_weave(client)
    root = add(client, wid, "x")
    resp = client.post(
        f"/weaves/{wid}/gen", json={"node_id": root["id"], "sampler_id": "ghost"}
    )
    assert resp.status_code == 404


def test_sampler_beats_preset(client, fake_openai_url):
    _, fake_app = fake_openai_url
    model = make_model(client, params={"temperature": 0.3, "n": 1})
    sampler = make_sampler(client, model["id"])
    wid = make_weave(client)
    root = add(client, wid, "x")
    # both preset and sampler_id given: sampler wins
    client.post(
        f"/weaves/{wid}/gen",
        json={"node_id": root["id"], "sampler_id": sampler["id"], "preset": "default"},
    )
    assert fake_app.state.received[-1]["temperature"] == 0.3  # from the model setup


def test_gen_resolves_api_key_env_at_request_time(client, fake_openai_url, monkeypatch):
    _, fake_app = fake_openai_url
    monkeypatch.setenv("COLOOM_TEST_SETUP_KEY", "sk-from-env")
    model = make_model(client, api_key_env="COLOOM_TEST_SETUP_KEY", params={"n": 1})
    sampler = make_sampler(client, model["id"])
    wid = make_weave(client)
    root = add(client, wid, "x")
    resp = client.post(
        f"/weaves/{wid}/gen", json={"node_id": root["id"], "sampler_id": sampler["id"]}
    )
    assert resp.status_code == 201, resp.text
    # the key reached the backend as a bearer header but never the request body
    body = fake_app.state.received[-1]
    assert "api_key" not in body and "Authorization" not in body
    raw = resp.json()[0]["creator"]["raw_request"]
    assert "sk-from-env" not in str(raw)
