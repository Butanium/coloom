"""CLI end-to-end tests against a live coloom server (real HTTP, real CLI main)."""

import json

import pytest
from coloom.cli import main


def run(capsys, live_server, *argv) -> dict | list:
    main(["--server", live_server, *argv])
    captured = capsys.readouterr()
    return json.loads(captured.out)


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

    # agent reads the active thread
    active = run(capsys, live_server, "--weave", wid, "read")
    assert active["content"] == "The loom hummed"

    # agent reads as plain text
    main(["--server", live_server, "--weave", wid, "read", "--text"])
    assert capsys.readouterr().out == "The loom hummed"

    # agent generates branches from the active tip
    nodes = run(capsys, live_server, "--weave", wid, "gen", "--set-active")
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
        "--set-active",
    )
    assert branch["creator"]["type"] == "model"
    assert branch["creator"]["label"] == "claude"

    active = run(capsys, live_server, "--weave", wid, "read")
    assert active["content"] == "The loom hummed and the agent wove on"

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

    run(capsys, live_server, "--weave", wid, "set-active", root_id)
    newer = run(capsys, live_server, "--weave", wid, "events", "--since", str(cursor))
    assert [e["type"] for e in newer["events"]] == ["active_changed"]


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


def test_cli_stdin_add(capsys, live_server, monkeypatch):
    import io

    created = run(capsys, live_server, "new", "--text", "root")
    wid = created["weave"]["id"]
    monkeypatch.setattr("sys.stdin", io.StringIO("from stdin"))
    node = run(capsys, live_server, "--weave", wid, "add", "--stdin")
    assert node["content"]["text"] == "from stdin"


def test_cli_gen_param_override(capsys, live_server, fake_openai_url):
    _, fake_app = fake_openai_url
    created = run(capsys, live_server, "new", "--text", "p")
    wid = created["weave"]["id"]
    run(
        capsys,
        live_server,
        "--weave",
        wid,
        "gen",
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


def test_cli_errors_exit_nonzero(capsys, live_server):
    with pytest.raises(SystemExit) as exc:
        main(["--server", live_server, "read"])  # no weave id
    assert exc.value.code == 1
    assert "no weave id" in capsys.readouterr().err

    with pytest.raises(SystemExit) as exc:
        main(["--server", live_server, "--weave", "nope", "read"])
    assert exc.value.code == 1
    assert "404" in capsys.readouterr().err
