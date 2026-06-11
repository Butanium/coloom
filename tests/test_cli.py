"""CLI end-to-end tests against a live coloom server (real HTTP, real CLI main)."""

import json

import pytest
from coloom.cli import main


def run(capsys, live_server, *argv) -> dict | list:
    main(["--server", live_server, *argv])
    captured = capsys.readouterr()
    return json.loads(captured.out)


def make_cli_generator(capsys, live_server, profile="cli-tester"):
    """A generator inheriting from the 'fake' builtin template (imported from
    the live server's config at boot), created through the CLI itself."""
    return run(
        capsys, live_server, "generators", "create",
        "--profile", profile, "--name", "fakegen", "--parent", "template:fake",
    )


def test_full_cli_coweave_loop(capsys, live_server):
    # human creates a weave with a root
    created = run(
        capsys,
        live_server,
        "new",
        "--title",
        "story",
        "--text",
        "The loom hummed",
        "--as",
        "clem",
    )
    wid = created["weave"]["id"]
    root_id = created["root"]["id"]
    assert created["root"]["creator"] == {
        "type": "human",
        "label": "clem",
        "color": None,
        "id": None,
    }

    # agent reads the thread at the (default-named) cursor `new` just placed
    thread = run(capsys, live_server, "--weave", wid, "read")
    assert thread["content"] == "The loom hummed"

    # agent reads as plain text
    main(["--server", live_server, "--weave", wid, "read", "--text"])
    assert capsys.readouterr().out == "The loom hummed"

    # agent generates branches from its cursor
    gen = make_cli_generator(capsys, live_server)
    nodes = run(
        capsys, live_server, "--weave", wid,
        "gen", "--move-cursor", "--generator", gen["id"],
    )
    assert len(nodes) == 2
    assert all(n["parents"] == [root_id] for n in nodes)
    assert nodes[0]["content"]["type"] == "tokens"

    # agent adds a manual branch attributed to itself
    branch = run(
        capsys,
        live_server,
        "--weave",
        wid,
        "add",
        "--parent",
        root_id,
        "--text",
        " and the agent wove on",
        "--as",
        "claude",
        "--creator-type",
        "model",
        "--move-cursor",
    )
    assert branch["creator"]["type"] == "model"
    assert branch["creator"]["label"] == "claude"

    thread = run(capsys, live_server, "--weave", wid, "read")
    assert thread["content"] == "The loom hummed and the agent wove on"

    # tree shows all branches with attribution intact
    tree = run(capsys, live_server, "--weave", wid, "read", "--tree")
    assert len(tree["nodes"]) == 4
    creators = {n["creator"]["label"] for n in tree["nodes"].values()}
    # gpt4-base reports itself as gpt-4-0314 in responses; we keep the response label
    assert creators == {"clem", "gpt-4-0314", "claude"}

    # a non-resident agent catches up via the event cursor
    events = run(capsys, live_server, "--weave", wid, "events")
    types = [e["type"] for e in events["events"]]
    assert types[0] == "weave_created"
    assert types.count("node_added") == 4
    cursor = events["cursor"]

    # the human points the agent's cursor back at the root ("look here")
    moved = run(
        capsys, live_server, "--weave", wid, "--cursor", "clem",
        "cursor", "set", root_id, "--name", "default",
    )
    assert moved["moved_by"] == "clem"
    newer = run(capsys, live_server, "--weave", wid, "events", "--since", str(cursor))
    assert [e["type"] for e in newer["events"]] == ["cursor_moved"]
    assert newer["events"][0]["payload"]["moved_by"] == "clem"

    cursors = run(capsys, live_server, "--weave", wid, "cursor", "list")
    assert set(cursors) == {"default"}
    assert cursors["default"]["node_id"] == root_id


def test_cli_bookmark_split_rm(capsys, live_server):
    created = run(capsys, live_server, "new", "--text", "hello world")
    wid = created["weave"]["id"]
    root_id = created["root"]["id"]

    run(capsys, live_server, "--weave", wid, "bookmark", root_id)
    tree = run(capsys, live_server, "--weave", wid, "read", "--tree")
    assert tree["bookmarks"] == [root_id]

    split = run(capsys, live_server, "--weave", wid, "split", root_id, "--at", "5")
    assert split["head"]["content"]["text"] == "hello"

    removed = run(capsys, live_server, "--weave", wid, "rm", split["tail"]["id"])
    assert removed["removed"] == [split["tail"]["id"]]
    assert removed["deleted_node_ids"] == [split["tail"]["id"]]

    # rm is a SOFT delete: restore brings the subtree back
    restored = run(
        capsys, live_server, "--weave", wid, "restore", split["tail"]["id"]
    )
    assert restored["restored_node_ids"] == [split["tail"]["id"]]
    tree = run(capsys, live_server, "--weave", wid, "read", "--tree")
    assert split["tail"]["id"] in tree["nodes"]


def test_cli_merge(capsys, live_server):
    created = run(capsys, live_server, "new", "--text", "hello")
    wid = created["weave"]["id"]
    root_id = created["root"]["id"]
    child = run(
        capsys, live_server, "--weave", wid,
        "add", "--parent", root_id, "--text", " world",
    )

    merged = run(capsys, live_server, "--weave", wid, "merge", child["id"])
    assert merged["merged_node"]["content"]["text"] == "hello world"
    assert merged["deleted_node_ids"] == [root_id, child["id"]]
    tree = run(capsys, live_server, "--weave", wid, "read", "--tree")
    assert set(tree["nodes"]) == {merged["merged_node_id"]}
    assert tree["roots"] == [merged["merged_node_id"]]

    # undoable through the existing restore path
    restored = run(capsys, live_server, "--weave", wid, "restore", root_id)
    assert set(restored["restored_node_ids"]) == {root_id, child["id"]}


def test_cli_stdin_add(capsys, live_server, monkeypatch):
    import io

    created = run(capsys, live_server, "new", "--text", "root")
    wid = created["weave"]["id"]
    monkeypatch.setattr("sys.stdin", io.StringIO("from stdin"))
    node = run(capsys, live_server, "--weave", wid, "add", "--stdin")
    assert node["content"]["text"] == "from stdin"


def test_cli_gen_param_override(capsys, live_server, fake_openai_url):
    _, fake_app = fake_openai_url
    gen = make_cli_generator(capsys, live_server)
    created = run(capsys, live_server, "new", "--text", "p")
    wid = created["weave"]["id"]
    run(
        capsys,
        live_server,
        "--weave",
        wid,
        "gen",
        "--generator",
        gen["id"],
        "-n",
        "2",
        "--param",
        "temperature=0.7",
        "--param",
        'stop=["\\n"]',
    )
    sent = fake_app.state.received[-1]
    assert sent["temperature"] == 0.7
    assert sent["n"] == 2
    assert sent["stop"] == ["\n"]


def test_cli_templates_and_generators_flow(capsys, live_server, fake_openai_url):
    url, fake_app = fake_openai_url
    template = run(
        capsys, live_server, "templates", "create",
        "--name", "base", "--base-url", url, "--model", "gpt-4-base",
        "--api-key", "sk-secret", "--param", "temperature=0.8",
    )
    assert template["api_key"] == "***"  # redacted in the CLI response too
    gen = run(
        capsys, live_server, "generators", "create",
        "--profile", "flow", "--name", "wild",
        "--parent", "template:base",  # by name
        "--param", "temperature=1.3",
    )
    assert gen["parent"] == {"kind": "template", "id": template["id"]}
    listed = run(capsys, live_server, "generators", "list", "--profile", "flow")
    assert [g["id"] for g in listed] == [gen["id"]]
    assert listed[0]["resolved"]["params"]["temperature"] == 1.3

    # gen with --generator routes through the resolved chain
    created = run(capsys, live_server, "new", "--text", "seed")
    wid = created["weave"]["id"]
    run(
        capsys, live_server, "--weave", wid,
        "gen", "--node", created["root"]["id"], "--generator", gen["id"],
    )
    assert fake_app.state.received[-1]["temperature"] == 1.3

    # update: param K=null removes the override (falls back to the template's)
    updated = run(
        capsys, live_server, "generators", "update", gen["id"],
        "--param", "temperature=null",
    )
    assert updated["resolved"]["params"]["temperature"] == 0.8

    # promote the generator into a new template
    promoted = run(
        capsys, live_server, "templates", "promote", gen["id"], "--name", "wild-tpl",
    )
    assert promoted["params"]["temperature"] == 0.8
    assert any(
        t["name"] == "wild-tpl" for t in run(capsys, live_server, "templates", "list")
    )

    run(capsys, live_server, "generators", "rm", gen["id"])
    assert run(capsys, live_server, "generators", "list", "--profile", "flow") == []
    run(capsys, live_server, "templates", "rm", template["id"])
    names = {t["name"] for t in run(capsys, live_server, "templates", "list")}
    assert "base" not in names


def test_cli_errors_exit_nonzero(capsys, live_server):
    with pytest.raises(SystemExit) as exc:
        main(["--server", live_server, "read"])  # no weave id
    assert exc.value.code == 1
    assert "no weave id" in capsys.readouterr().err

    with pytest.raises(SystemExit) as exc:
        main(["--server", live_server, "--weave", "nope", "read"])
    assert exc.value.code == 1
    assert "404" in capsys.readouterr().err


def test_profile_login_defaults_cursor_and_creator(capsys, live_server, tmp_path, monkeypatch):
    """`profile login` persists; --cursor and --as then default to the profile,
    so CLI weaving is attributed like web weaving."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("COLOOM_CURSOR", raising=False)
    monkeypatch.delenv("COLOOM_PROFILE", raising=False)
    # PROFILE_FILE is computed at import time — repoint it for this test
    import coloom.cli as cli_mod
    monkeypatch.setattr(
        cli_mod, "PROFILE_FILE", tmp_path / "coloom" / "profile"
    )

    assert run(capsys, live_server, "profile", "whoami") == {"profile": None}
    logged = run(capsys, live_server, "profile", "login", "clem-cli")
    assert logged["logged_in"] == "clem-cli"
    assert run(capsys, live_server, "profile", "whoami") == {"profile": "clem-cli"}
    assert any(
        p["name"] == "clem-cli"
        for p in run(capsys, live_server, "profile", "list")
    )

    # cursor + creator label now default to the profile
    created = run(capsys, live_server, "new", "--title", "p", "--text", "root")
    assert created["root"]["creator"]["label"] == "clem-cli"
    wid = created["weave"]["id"]
    cursors = run(capsys, live_server, "--weave", wid, "cursor", "list")
    assert "clem-cli" in cursors

    # login again must NOT wipe existing settings
    import httpx as _httpx
    _httpx.put(
        f"{live_server}/profiles/clem-cli",
        json={"settings": {"keep": True}},
        timeout=5,
    )
    logged = run(capsys, live_server, "profile", "login", "clem-cli")
    assert logged["settings"] == {"keep": True}

    # explicit flag still beats the profile
    created = run(
        capsys, live_server, "new", "--title", "q", "--text", "r", "--as", "other"
    )
    assert created["root"]["creator"]["label"] == "other"

    run(capsys, live_server, "profile", "logout")
    assert run(capsys, live_server, "profile", "whoami") == {"profile": None}
