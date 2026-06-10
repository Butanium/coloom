"""Templates + per-profile generators (docs/generators-api.md): CRUD, redaction,
inheritance resolution, flatten-on-delete, promotion, seeding, global events,
the legacy-setups migration, and gen-with-generator end-to-end against the fake
completions backend. Ports all coverage from the retired /setups suite."""

import sqlite3
from contextlib import contextmanager
from urllib.parse import quote

import pytest
from coloom.config import ColoomConfig, EndpointConfig, Preset
from coloom.server.app import create_app
from coloom.store import WeaveStore
from fastapi.testclient import TestClient


@pytest.fixture
def store(tmp_path):
    store = WeaveStore(tmp_path / "generators.sqlite")
    yield store
    store.close()


@pytest.fixture
def client(store, fake_openai_url):
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
    with TestClient(create_app(store, config)) as c:
        c.fake_url = url
        yield c


def make_template(client, **kwargs):
    body = {
        "name": "tpl",
        "base_url": client.fake_url,
        "model": "gpt-4-base",
        **kwargs,
    }
    resp = client.post("/templates", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def make_generator(client, profile="ada", **kwargs):
    resp = client.post("/generators", json={"profile": profile, "name": "g", **kwargs})
    assert resp.status_code == 201, resp.text
    return resp.json()


def list_generators(client, profile="ada"):
    resp = client.get(f"/generators?profile={profile}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def make_weave(client):
    resp = client.post("/weaves", json={"title": "t"})
    assert resp.status_code == 201
    return resp.json()["id"]


def add(client, wid, text, **kwargs):
    resp = client.post(f"/weaves/{wid}/nodes", json={"text": text, **kwargs})
    assert resp.status_code == 201, resp.text
    return resp.json()


# ------------------------------------------------------------ template CRUD


def test_builtin_templates_imported_from_config(client):
    templates = client.get("/templates").json()
    builtins = [t for t in templates if t["builtin"]]
    assert [t["name"] for t in builtins] == ["default"]
    assert builtins[0]["model"] == "gpt-4-base"
    assert builtins[0]["params"]["n"] == 2  # merged endpoint params


def test_template_crud_roundtrip(client):
    tpl = make_template(client, params={"temperature": 0.8})
    listed = [t for t in client.get("/templates").json() if not t["builtin"]]
    assert [t["id"] for t in listed] == [tpl["id"]]

    patched = client.patch(
        f"/templates/{tpl['id']}", json={"model": "gpt-4.5-base"}
    ).json()
    assert patched["model"] == "gpt-4.5-base"
    assert patched["name"] == "tpl"  # unchanged
    assert patched["params"] == {"temperature": 0.8}  # unchanged

    # params PATCH merges per-key; null removes a key
    patched = client.patch(
        f"/templates/{tpl['id']}",
        json={"params": {"temperature": None, "top_p": 0.9}},
    ).json()
    assert patched["params"] == {"top_p": 0.9}

    assert client.delete(f"/templates/{tpl['id']}").status_code == 204
    assert [t for t in client.get("/templates").json() if not t["builtin"]] == []


def test_builtin_template_is_readonly(client):
    builtin = next(t for t in client.get("/templates").json() if t["builtin"])
    assert (
        client.patch(f"/templates/{builtin['id']}", json={"model": "x"}).status_code
        == 403
    )
    assert client.delete(f"/templates/{builtin['id']}").status_code == 403


def test_template_create_missing_fields_is_400(client):
    resp = client.post("/templates", json={"name": "incomplete"})
    assert resp.status_code == 400
    assert "base_url" in resp.json()["detail"]


def test_unknown_ids_are_404(client):
    assert client.patch("/templates/nope", json={"name": "x"}).status_code == 404
    assert client.delete("/templates/nope").status_code == 404
    assert client.get("/generators/nope").status_code == 404
    assert client.patch("/generators/nope", json={"name": "x"}).status_code == 404
    assert client.delete("/generators/nope").status_code == 404


def test_api_key_redacted_everywhere(client):
    tpl = make_template(client, api_key="sk-secret-123")
    assert tpl["api_key"] == "***"  # POST response redacts
    listed = next(t for t in client.get("/templates").json() if t["id"] == tpl["id"])
    assert listed["api_key"] == "***"
    patched = client.patch(f"/templates/{tpl['id']}", json={"name": "renamed"}).json()
    assert patched["api_key"] == "***"  # PATCH redacts, key preserved

    gen = make_generator(client, parent={"kind": "template", "id": tpl["id"]})
    assert gen["resolved"]["api_key"] == "***"  # resolved view redacts too
    own_key = make_generator(client, api_key="sk-own")
    assert own_key["api_key"] == "***"


def test_api_key_clear_vs_unchanged(client):
    tpl = make_template(client, api_key="sk-secret")
    # omitted api_key = unchanged (still redacted)
    assert (
        client.patch(f"/templates/{tpl['id']}", json={"name": "x"}).json()["api_key"]
        == "***"
    )
    # explicit null clears it
    cleared = client.patch(f"/templates/{tpl['id']}", json={"api_key": None}).json()
    assert cleared["api_key"] is None


def test_mutual_exclusion_is_400(client):
    resp = client.post(
        "/templates",
        json={
            "name": "m",
            "base_url": client.fake_url,
            "model": "x",
            "api_key": "sk-1",
            "api_key_env": "SOME_ENV",
        },
    )
    assert resp.status_code == 400
    tpl = make_template(client, api_key="sk-1")
    assert (
        client.patch(
            f"/templates/{tpl['id']}", json={"api_key_env": "SOME_ENV"}
        ).status_code
        == 400
    )
    # same on generators
    gen = make_generator(client, api_key="sk-1")
    assert (
        client.patch(
            f"/generators/{gen['id']}", json={"api_key_env": "SOME_ENV"}
        ).status_code
        == 400
    )


def test_both_keys_null_is_allowed(client):
    tpl = make_template(client)  # keyless endpoint (llama.cpp etc.)
    assert tpl["api_key"] is None and tpl["api_key_env"] is None


# ------------------------------------------------------------ inheritance


def test_resolution_walks_the_chain(client):
    """template → gen A (overrides temperature) → gen B (overrides max_tokens):
    scalars nearest-set, params merged root→leaf."""
    tpl = make_template(client, params={"temperature": 0.8, "max_tokens": 12})
    a = make_generator(
        client,
        parent={"kind": "template", "id": tpl["id"]},
        params={"temperature": 1.2},
    )
    b = make_generator(
        client,
        parent={"kind": "generator", "id": a["id"]},
        params={"max_tokens": 5},
    )
    assert b["resolved"]["base_url"] == client.fake_url  # from the template
    assert b["resolved"]["model"] == "gpt-4-base"
    assert b["resolved"]["params"] == {"temperature": 1.2, "max_tokens": 5}
    assert b["usable"] is True
    assert b["params"] == {"max_tokens": 5}  # raw overrides untouched


def test_parentless_skeleton_is_unusable(client):
    gen = make_generator(client, params={"temperature": 0.5})
    assert gen["usable"] is False
    assert gen["resolved"]["base_url"] is None


def test_nearest_credential_wins_jointly(client):
    """A child api_key overrides an ancestor api_key_env (credentials are one
    logical field — never both set in a resolved view)."""
    tpl = make_template(client, api_key_env="SOME_ENV")
    gen = make_generator(
        client, parent={"kind": "template", "id": tpl["id"]}, api_key="sk-child"
    )
    assert gen["resolved"]["api_key"] == "***"
    assert gen["resolved"]["api_key_env"] is None


def test_cycle_rejected_on_write(client):
    a = make_generator(client)
    b = make_generator(client, parent={"kind": "generator", "id": a["id"]})
    # a -> b would close the loop
    resp = client.patch(
        f"/generators/{a['id']}", json={"parent": {"kind": "generator", "id": b["id"]}}
    )
    assert resp.status_code == 400
    assert "cycle" in resp.json()["detail"]
    # self-parent is the smallest cycle
    resp = client.patch(
        f"/generators/{a['id']}", json={"parent": {"kind": "generator", "id": a["id"]}}
    )
    assert resp.status_code == 400


def test_cross_profile_parent_rejected(client):
    mine = make_generator(client, profile="ada")
    resp = client.post(
        "/generators",
        json={
            "profile": "zoe",
            "name": "stolen",
            "parent": {"kind": "generator", "id": mine["id"]},
        },
    )
    assert resp.status_code == 400
    assert "profile" in resp.json()["detail"]


def test_bad_parent_reference_is_400(client):
    for parent in (
        {"kind": "template", "id": "ghost"},
        {"kind": "generator", "id": "ghost"},
    ):
        resp = client.post(
            "/generators", json={"profile": "ada", "name": "g", "parent": parent}
        )
        assert resp.status_code == 400


def test_patch_null_clears_override_back_to_inherited(client):
    tpl = make_template(client, params={"temperature": 0.8})
    gen = make_generator(
        client,
        parent={"kind": "template", "id": tpl["id"]},
        model="my-override",
        params={"temperature": 1.5},
    )
    assert gen["resolved"]["model"] == "my-override"
    # scalar null -> inherited again
    patched = client.patch(f"/generators/{gen['id']}", json={"model": None}).json()
    assert patched["model"] is None
    assert patched["resolved"]["model"] == "gpt-4-base"
    # params key null -> override removed
    patched = client.patch(
        f"/generators/{gen['id']}", json={"params": {"temperature": None}}
    ).json()
    assert patched["params"] == {}
    assert patched["resolved"]["params"] == {"temperature": 0.8}


def test_delete_generator_flattens_children(client):
    tpl = make_template(client, params={"temperature": 0.8})
    a = make_generator(
        client,
        parent={"kind": "template", "id": tpl["id"]},
        params={"temperature": 1.2},
    )
    b = make_generator(
        client,
        parent={"kind": "generator", "id": a["id"]},
        params={"max_tokens": 5},
    )
    assert client.delete(f"/generators/{a['id']}").status_code == 204
    flat = client.get(f"/generators/{b['id']}").json()
    assert flat["parent"] is None  # detached…
    assert flat["base_url"] == client.fake_url  # …with resolved fields materialized
    assert flat["params"] == {"temperature": 1.2, "max_tokens": 5}
    assert flat["resolved"]["params"] == {"temperature": 1.2, "max_tokens": 5}


def test_delete_template_flattens_direct_children_only(client):
    tpl = make_template(client, params={"temperature": 0.8})
    a = make_generator(client, parent={"kind": "template", "id": tpl["id"]})
    b = make_generator(client, parent={"kind": "generator", "id": a["id"]})
    assert client.delete(f"/templates/{tpl['id']}").status_code == 204
    a_after = client.get(f"/generators/{a['id']}").json()
    b_after = client.get(f"/generators/{b['id']}").json()
    assert a_after["parent"] is None
    assert a_after["params"] == {"temperature": 0.8}
    assert b_after["parent"] == {"kind": "generator", "id": a["id"]}  # untouched
    assert b_after["resolved"]["params"] == {"temperature": 0.8}  # same behavior


# ------------------------------------------------------------ creation modes


def test_create_from_template_inherit_vs_duplicate(client):
    tpl = make_template(client, params={"temperature": 0.7})
    inherited = client.post(
        "/generators",
        json={
            "from": {"kind": "template", "id": tpl["id"]},
            "mode": "inherit",
            "profile": "ada",
        },
    ).json()
    assert inherited["name"] == "tpl"  # defaults to the source name
    assert inherited["parent"] == {"kind": "template", "id": tpl["id"]}
    assert inherited["params"] == {}  # empty overrides

    duplicated = client.post(
        "/generators",
        json={
            "from": {"kind": "template", "id": tpl["id"]},
            "mode": "duplicate",
            "profile": "ada",
            "name": "standalone",
        },
    ).json()
    assert duplicated["parent"] is None  # template fields copied, no link
    assert duplicated["base_url"] == client.fake_url
    assert duplicated["params"] == {"temperature": 0.7}


def test_create_from_generator_inherit_vs_duplicate(client):
    tpl = make_template(client)
    source = make_generator(
        client,
        parent={"kind": "template", "id": tpl["id"]},
        params={"temperature": 1.3},
    )
    inherited = client.post(
        "/generators",
        json={
            "from": {"kind": "generator", "id": source["id"]},
            "mode": "inherit",
            "profile": "ada",
            "name": "kid",
        },
    ).json()
    assert inherited["parent"] == {"kind": "generator", "id": source["id"]}
    assert inherited["params"] == {}

    duplicated = client.post(
        "/generators",
        json={
            "from": {"kind": "generator", "id": source["id"]},
            "mode": "duplicate",
            "profile": "ada",
        },
    ).json()
    # literal row copy: same parent, same overrides, new id
    assert duplicated["parent"] == {"kind": "template", "id": tpl["id"]}
    assert duplicated["params"] == {"temperature": 1.3}
    assert duplicated["id"] != source["id"]


def test_create_from_requires_mode_and_valid_source(client):
    tpl = make_template(client)
    resp = client.post(
        "/generators",
        json={"from": {"kind": "template", "id": tpl["id"]}, "profile": "ada"},
    )
    assert resp.status_code == 400  # mode missing
    resp = client.post(
        "/generators",
        json={
            "from": {"kind": "generator", "id": "ghost"},
            "mode": "inherit",
            "profile": "ada",
        },
    )
    assert resp.status_code == 400  # unknown source


def test_promote_generator_to_template(client):
    tpl = make_template(client, api_key="sk-secret", params={"temperature": 0.8})
    gen = make_generator(
        client,
        parent={"kind": "template", "id": tpl["id"]},
        params={"temperature": 1.2, "n": 4},
    )
    promoted = client.post(
        "/templates", json={"from_generator": gen["id"], "name": "promoted"}
    )
    assert promoted.status_code == 201, promoted.text
    body = promoted.json()
    assert body["name"] == "promoted"
    assert body["builtin"] is False
    assert body["base_url"] == client.fake_url  # RESOLVED fields materialized
    assert body["params"] == {"temperature": 1.2, "n": 4}
    assert body["api_key"] == "***"  # inherited key carried over, redacted in response

    # an unresolvable generator can't be promoted
    skeleton = make_generator(client)
    resp = client.post("/templates", json={"from_generator": skeleton["id"]})
    assert resp.status_code == 400


# ------------------------------------------------------------ seeding


def test_profile_creation_seeds_builtin_generators(client):
    client.put("/profiles/newbie", json={"settings": {}})
    gens = list_generators(client, "newbie")
    assert [g["name"] for g in gens] == ["default"]  # one per builtin template
    builtin = next(t for t in client.get("/templates").json() if t["builtin"])
    assert gens[0]["parent"] == {"kind": "template", "id": builtin["id"]}
    assert gens[0]["usable"] is True

    # settings saves don't duplicate the seed
    client.put("/profiles/newbie", json={"settings": {"x": 1}})
    assert len(list_generators(client, "newbie")) == 1


def test_deleted_seeded_generator_does_not_resurrect(client):
    client.put("/profiles/min", json={"settings": {}})
    (gen,) = list_generators(client, "min")
    assert client.delete(f"/generators/{gen['id']}").status_code == 204
    client.put("/profiles/min", json={"settings": {"y": 2}})  # re-login / save
    assert list_generators(client, "min") == []


def test_seeding_skips_derived_generator_even_renamed(store, fake_openai_url, tmp_path):
    """A profile whose generator already derives from a builtin template (renamed
    or not) is not re-seeded — checked across server restarts (fresh seeds db)."""
    url, _ = fake_openai_url
    config = ColoomConfig(
        endpoints={"fake": EndpointConfig(base_url=url, model="m")},
        presets={"default": Preset(endpoint="fake")},
    )
    with TestClient(create_app(store, config)) as c:
        c.put("/profiles/renamer", json={"settings": {}})
        (gen,) = c.get("/generators?profile=renamer").json()
        c.patch(f"/generators/{gen['id']}", json={"name": "my-fancy-name"})
    # wipe the seed records to simulate a pre-seeds db; boot must still detect
    # the derived generator instead of duplicating it
    with store._tx() as conn:
        conn.execute("DELETE FROM generator_seeds")
    with TestClient(create_app(store, config)) as c:
        gens = c.get("/generators?profile=renamer").json()
        assert [g["name"] for g in gens] == ["my-fancy-name"]


def test_boot_seeds_new_builtin_templates_for_active_profiles(store, fake_openai_url):
    url, _ = fake_openai_url
    base = {"fake": EndpointConfig(base_url=url, model="m")}
    config1 = ColoomConfig(endpoints=base, presets={"default": Preset(endpoint="fake")})
    with TestClient(create_app(store, config1)) as c:
        c.put("/profiles/vet", json={"settings": {}})
        assert len(c.get("/generators?profile=vet").json()) == 1
    config2 = ColoomConfig(
        endpoints=base,
        presets={
            "default": Preset(endpoint="fake"),
            "wild": Preset(endpoint="fake", params={"temperature": 1.4}),
        },
    )
    with TestClient(create_app(store, config2)) as c:  # "server restart", new yaml
        gens = c.get("/generators?profile=vet").json()
        assert sorted(g["name"] for g in gens) == ["default", "wild"]
        wild = next(g for g in gens if g["name"] == "wild")
        assert wild["resolved"]["params"]["temperature"] == 1.4


# ------------------------------------------------------------ events


def test_template_and_generator_events_carry_by_and_origin(client):
    since = client.get("/events").json()["cursor"]
    # the profile header is percent-encoded UTF-8 (headers can't carry é raw)
    headers = {"X-Coloom-Profile": quote("clément"), "X-Coloom-Client": "tab-7"}
    resp = client.post(
        "/templates",
        json={"name": "evt", "base_url": client.fake_url, "model": "m"},
        headers=headers,
    )
    assert resp.status_code == 201
    tpl = resp.json()
    client.patch(f"/templates/{tpl['id']}", json={"model": "m2"}, headers=headers)
    resp = client.post(
        "/generators",
        json={"profile": "clément", "name": "mine"},
        headers=headers,
    )
    gen = resp.json()
    client.delete(f"/generators/{gen['id']}", headers=headers)

    events = client.get(f"/events?since={since}").json()["events"]
    by_type = {e["type"]: e for e in events}
    assert {
        "template_created",
        "template_updated",
        "generator_created",
        "generator_deleted",
    } <= set(by_type)
    for e in by_type.values():
        assert e["payload"]["by"] == "clément"
        assert e["payload"]["origin"] == "tab-7"
        assert e["weave_id"] == ""  # global scope
    assert by_type["generator_created"]["payload"]["profile"] == "clément"
    assert by_type["template_created"]["payload"]["name"] == "evt"


def test_global_events_reach_weave_scoped_clients(client):
    """Template/generator events are global: weave-filtered WS subscribers and
    weave-filtered GET /events both see them (clients filter by type)."""
    wid = make_weave(client)
    since = client.get("/events").json()["cursor"]
    with client.websocket_connect(f"/ws?weave_id={wid}") as ws:
        make_template(client, name="broadcast-me")
        event = ws.receive_json()
        assert event["type"] == "template_created"
        assert event["payload"]["name"] == "broadcast-me"
    polled = client.get(f"/events?weave_id={wid}&since={since}").json()["events"]
    assert [e["type"] for e in polled] == ["template_created"]


def test_flatten_emits_generator_updated(client):
    tpl = make_template(client)
    a = make_generator(client, parent={"kind": "template", "id": tpl["id"]})
    since = client.get("/events").json()["cursor"]
    client.delete(f"/templates/{tpl['id']}")
    events = client.get(f"/events?since={since}").json()["events"]
    assert [e["type"] for e in events] == ["generator_updated", "template_deleted"]
    assert events[0]["payload"]["id"] == a["id"]


# ------------------------------------------------------------ generation


def seeded_generator(client, profile="weaver"):
    client.put(f"/profiles/{profile}", json={"settings": {}})
    (gen,) = list_generators(client, profile)
    return gen


def test_gen_with_generator_merges_body(client, fake_openai_url):
    _, fake_app = fake_openai_url
    tpl = make_template(
        client, params={"temperature": 0.8, "max_tokens": 12, "logprobs": 5}
    )
    gen = make_generator(
        client,
        parent={"kind": "template", "id": tpl["id"]},
        # overrides temperature; adds an arbitrary pass-through flag
        params={"temperature": 1.2, "logit_bias": {"50256": -100}},
    )
    wid = make_weave(client)
    add(client, wid, "The loom", move_cursor="agent")

    resp = client.post(
        f"/weaves/{wid}/gen",
        json={"cursor": "agent", "generator_id": gen["id"], "params": {"n": 1}},
    )
    assert resp.status_code == 201, resp.text
    body = fake_app.state.received[-1]
    assert body["model"] == "gpt-4-base"
    assert body["temperature"] == 1.2  # generator beats template
    assert body["max_tokens"] == 12  # from the template
    assert body["logit_bias"] == {"50256": -100}  # arbitrary flag passes through
    assert body["n"] == 1  # request params win over everything
    assert body["prompt"] == "The loom"

    # raw_request stored on the node mirrors the merged body (no api key in it)
    raw = resp.json()[0]["creator"]["raw_request"]
    assert raw["temperature"] == 1.2
    assert raw["logit_bias"] == {"50256": -100}
    assert "api_key" not in raw and "Authorization" not in raw


def test_two_generators_attribute_different_params(client):
    tpl = make_template(client, params={"temperature": 1.0, "n": 1})
    parent = {"kind": "template", "id": tpl["id"]}
    wild = make_generator(client, parent=parent, params={"temperature": 1.2})
    safe = make_generator(client, parent=parent, params={"temperature": 0.4})
    wid = make_weave(client)
    root = add(client, wid, "seed")

    n_wild = client.post(
        f"/weaves/{wid}/gen", json={"node_id": root["id"], "generator_id": wild["id"]}
    ).json()
    n_safe = client.post(
        f"/weaves/{wid}/gen", json={"node_id": root["id"], "generator_id": safe["id"]}
    ).json()
    assert n_wild[0]["creator"]["raw_request"]["temperature"] == 1.2
    assert n_safe[0]["creator"]["raw_request"]["temperature"] == 0.4
    # both generators' nodes hang off the same root (multi-active fan-out is
    # client-side: one /gen per active generator)
    tree = client.get(f"/weaves/{wid}").json()
    children = set(tree["nodes"][root["id"]]["children"])
    assert {n_wild[0]["id"], n_safe[0]["id"]} <= children


def test_gen_events_carry_generator_name(client):
    gen = seeded_generator(client)
    wid = make_weave(client)
    root = add(client, wid, "x")
    resp = client.post(
        f"/weaves/{wid}/gen", json={"node_id": root["id"], "generator_id": gen["id"]}
    )
    assert resp.status_code == 201
    events = client.get(f"/events?weave_id={wid}").json()["events"]
    started = next(e for e in events if e["type"] == "gen_started")
    finished = next(e for e in events if e["type"] == "gen_finished")
    assert started["payload"]["generator"] == "default"
    assert finished["payload"]["generator"] == "default"
    assert started["payload"]["generator_id"] == gen["id"]


def test_gen_requires_generator_id(client):
    wid = make_weave(client)
    root = add(client, wid, "x")
    resp = client.post(f"/weaves/{wid}/gen", json={"node_id": root["id"]})
    assert resp.status_code == 400
    assert "generator_id" in resp.json()["detail"]


def test_gen_unknown_generator_is_404(client):
    wid = make_weave(client)
    root = add(client, wid, "x")
    resp = client.post(
        f"/weaves/{wid}/gen", json={"node_id": root["id"], "generator_id": "ghost"}
    )
    assert resp.status_code == 404


def test_gen_unresolvable_generator_is_400(client):
    skeleton = make_generator(client)  # no parent, no base_url/model
    wid = make_weave(client)
    root = add(client, wid, "x")
    resp = client.post(
        f"/weaves/{wid}/gen",
        json={"node_id": root["id"], "generator_id": skeleton["id"]},
    )
    assert resp.status_code == 400


def test_gen_resolves_api_key_env_at_request_time(client, fake_openai_url, monkeypatch):
    _, fake_app = fake_openai_url
    monkeypatch.setenv("COLOOM_TEST_SETUP_KEY", "sk-from-env")
    tpl = make_template(client, api_key_env="COLOOM_TEST_SETUP_KEY", params={"n": 1})
    gen = make_generator(client, parent={"kind": "template", "id": tpl["id"]})
    wid = make_weave(client)
    root = add(client, wid, "x")
    resp = client.post(
        f"/weaves/{wid}/gen", json={"node_id": root["id"], "generator_id": gen["id"]}
    )
    assert resp.status_code == 201, resp.text
    # the key reached the backend as a bearer header but never the request body
    body = fake_app.state.received[-1]
    assert "api_key" not in body and "Authorization" not in body
    raw = resp.json()[0]["creator"]["raw_request"]
    assert "sk-from-env" not in str(raw)


# ------------------------------------------------------------ migration


def test_legacy_setups_migrate_to_templates_and_generators(tmp_path):
    """model_setups → templates; active-profile × sampler_setups → generators
    inheriting from the migrated template. Idempotent across reopens."""
    db = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE model_setups (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, base_url TEXT NOT NULL,
            api_key TEXT, api_key_env TEXT, model TEXT NOT NULL,
            params TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE sampler_setups (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, model_setup_id TEXT NOT NULL,
            params TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE profiles (
            name TEXT PRIMARY KEY, settings TEXT NOT NULL DEFAULT '{}',
            created TEXT NOT NULL, updated TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1);
        INSERT INTO model_setups VALUES
            ('ms1', 'gpt4base', 'https://api.openai.com/v1', 'sk-live', NULL,
             'gpt-4-base', '{"max_tokens": 64}');
        INSERT INTO sampler_setups VALUES
            ('ss1', 'wild', 'ms1', '{"temperature": 1.4}'),
            ('ss2', 'dangling', 'ghost', '{}');
        INSERT INTO profiles VALUES
            ('clément', '{}', 't0', 't0', 1),
            ('uitest-a', '{}', 't0', 't0', 1),
            ('deleted-one', '{}', 't0', 't0', 0);
        """
    )
    conn.commit()
    conn.close()

    for _ in range(2):  # second open must not duplicate anything
        store = WeaveStore(db)
        templates = store.list_templates()
        assert [(t.id, t.name, t.builtin) for t in templates] == [
            ("ms1", "gpt4base", False)
        ]
        assert templates[0].api_key == "sk-live"  # raw in store, redacted at the API
        for profile in ("clément", "uitest-a"):
            gens = store.list_generators(profile)
            assert [g.name for g in gens] == ["wild"], profile
            assert gens[0].parent is not None and gens[0].parent.id == "ms1"
            # old sampler id is stamped so clients can map legacy settings refs
            assert gens[0].migrated_from == "ss1"
            resolved = store.resolve_generator(gens[0].id)
            assert resolved.params == {"max_tokens": 64, "temperature": 1.4}
            assert resolved.usable
        # inactive profiles and dangling samplers are skipped
        assert store.list_generators("deleted-one") == []
        store.close()


# ------------------------------------------------------------ endpoint probe


@contextmanager
def run_http_app(app):
    """Serve a FastAPI app over real HTTP on an ephemeral port."""
    import socket
    import threading
    import time

    import uvicorn

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
        assert time.time() < deadline
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@contextmanager
def models_endpoint(model_ids=("real-model",)):
    """A /v1/models server that records each request's Authorization header."""
    from fastapi import FastAPI, Request

    app = FastAPI()
    auth_seen: list = []

    @app.get("/v1/models")
    async def models(request: Request) -> dict:
        auth_seen.append(request.headers.get("authorization"))
        return {"object": "list", "data": [{"id": m} for m in model_ids]}

    with run_http_app(app) as url:
        yield url, auth_seen


def test_probe_endpoint_against_fake(client):
    """gpt-fake implements /v1/models, so the probe finds it (real HTTP hop;
    the conftest fake server has no /models route — spin the real gpt-fake app)."""
    from coloom.fake_openai import build_app as build_fake

    with run_http_app(build_fake()) as url:
        resp = client.post("/probe-endpoint", json={"base_url": url})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "error": None, "models": ["gpt-fake"]}


def test_probe_by_template_id_uses_stored_key(client):
    """Re-probing an existing template whose literal key the client only sees
    as '***': the server sends the STORED key, never echoes it."""
    with models_endpoint() as (url, auth_seen):
        tpl = make_template(client, base_url=url, api_key="sk-stored-secret")
        assert tpl["api_key"] == "***"  # the client never had the real key
        resp = client.post("/probe-endpoint", json={"template_id": tpl["id"]})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "error": None, "models": ["real-model"]}
        assert auth_seen == ["Bearer sk-stored-secret"]
        assert "sk-stored-secret" not in resp.text


def test_probe_by_generator_id_resolves_inherited_key(client):
    """Generator probe walks the chain like /gen: inherited template key +
    inherited base_url, both invisible to the client."""
    with models_endpoint() as (url, auth_seen):
        tpl = make_template(client, base_url=url, api_key="sk-chain")
        gen = make_generator(client, parent={"kind": "template", "id": tpl["id"]})
        resp = client.post("/probe-endpoint", json={"generator_id": gen["id"]})
        assert resp.json()["ok"] is True
        assert auth_seen == ["Bearer sk-chain"]


def test_probe_explicit_base_url_wins_over_stored(client):
    """'User is editing the URL field right now': the request's base_url is
    probed (here: a dead port), the stored credentials still apply."""
    tpl = make_template(client, base_url="http://127.0.0.1:9", api_key="sk-x")
    resp = client.post(
        "/probe-endpoint",
        json={"template_id": tpl["id"], "base_url": "http://127.0.0.1:1/v1"},
    )
    body = resp.json()
    assert body["ok"] is False
    assert "127.0.0.1:1" in body["error"]  # probed the explicit URL, not the stored one


def test_probe_by_id_validation(client):
    tpl = make_template(client)
    gen = make_generator(client)
    # both ids
    resp = client.post(
        "/probe-endpoint", json={"template_id": tpl["id"], "generator_id": gen["id"]}
    )
    assert resp.status_code == 400
    # id + literal credentials
    resp = client.post(
        "/probe-endpoint", json={"template_id": tpl["id"], "api_key": "sk-1"}
    )
    assert resp.status_code == 400
    # neither id nor base_url
    assert client.post("/probe-endpoint", json={}).status_code == 400
    # unknown id is a bad body reference
    assert (
        client.post("/probe-endpoint", json={"template_id": "ghost"}).status_code
        == 400
    )
    # unresolvable skeleton: operational ok=false, not a 400
    body = client.post("/probe-endpoint", json={"generator_id": gen["id"]}).json()
    assert body["ok"] is False and "no base_url" in body["error"]


def test_probe_endpoint_up_but_no_models_route(client):
    """The conftest fake serves /v1/completions only: /models 404s, which means
    'reachable, no suggestions' (ok=true, empty list)."""
    resp = client.post("/probe-endpoint", json={"base_url": client.fake_url})
    assert resp.json() == {"ok": True, "error": None, "models": []}


def test_probe_endpoint_unreachable(client):
    resp = client.post("/probe-endpoint", json={"base_url": "http://127.0.0.1:1/v1"})
    body = resp.json()
    assert body["ok"] is False
    assert body["models"] == []
    assert "cannot reach" in body["error"]


def test_probe_endpoint_missing_env_key(client):
    resp = client.post(
        "/probe-endpoint",
        json={"base_url": client.fake_url, "api_key_env": "COLOOM_UNSET_PROBE_KEY"},
    )
    body = resp.json()
    assert body["ok"] is False
    assert "COLOOM_UNSET_PROBE_KEY" in body["error"]
    # never echo a literal key in any response
    resp = client.post(
        "/probe-endpoint",
        json={"base_url": "http://127.0.0.1:1/v1", "api_key": "sk-hush"},
    )
    assert "sk-hush" not in resp.text


def test_probe_endpoint_both_keys_is_400(client):
    resp = client.post(
        "/probe-endpoint",
        json={"base_url": client.fake_url, "api_key": "a", "api_key_env": "B"},
    )
    assert resp.status_code == 400
