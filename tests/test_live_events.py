"""Milestone-5 e2e: two independent clients over real TCP — client B holds a
WebSocket while client A mutates the weave through the CLI; B sees every edit
live, including generated branches."""

import asyncio
import json
import threading

from coloom.cli import main as cli_main
from websockets.asyncio.client import connect


def cli(capsys, live_server, *argv) -> dict | list:
    cli_main(["--server", live_server, *argv])
    return json.loads(capsys.readouterr().out)


async def collect_events(
    ws_url: str, n: int, ready: threading.Event, timeout: float = 15.0
):
    events = []
    async with connect(ws_url) as ws:
        ready.set()
        while len(events) < n:
            events.append(json.loads(await asyncio.wait_for(ws.recv(), timeout)))
    return events


def test_two_clients_see_each_other_live(capsys, live_server):
    created = cli(capsys, live_server, "new", "--title", "live", "--text", "Seed text")
    # the generator is created before B subscribes: its (global) creation event
    # must not show up in B's expected stream below
    generator = cli(
        capsys, live_server, "generators", "create",
        "--profile", "live-tester", "--name", "g", "--parent", "template:fake",
    )
    wid = created["weave"]["id"]
    root_id = created["root"]["id"]
    ws_url = live_server.replace("http://", "ws://") + f"/ws?weave_id={wid}"

    # client B: subscribe and wait for 5 events in a background thread
    ready = threading.Event()
    result: dict = {}

    def subscriber():
        result["events"] = asyncio.run(collect_events(ws_url, 6, ready))

    thread = threading.Thread(target=subscriber)
    thread.start()
    assert ready.wait(timeout=10), "subscriber failed to connect"

    # client A: weave through the CLI (human add + agent gen)
    added = cli(
        capsys,
        live_server,
        "--weave",
        wid,
        "add",
        "--parent",
        root_id,
        "--text",
        " grows",
        "--move-cursor",
    )
    generated = cli(
        capsys, live_server, "--weave", wid, "gen", "--generator", generator["id"]
    )

    thread.join(timeout=20)
    assert not thread.is_alive(), "subscriber did not receive all events in time"
    events = result["events"]

    assert [e["type"] for e in events] == [
        "node_added",  # A's manual branch
        "cursor_moved",  # --move-cursor
        "gen_started",  # presence: a generation is in flight
        "node_added",  # gen choice 0
        "node_added",  # gen choice 1
        "gen_finished",  # presence: generation done
    ]
    node_ids = {e["payload"]["node_id"] for e in events if e["type"] == "node_added"}
    assert added["id"] in node_ids
    assert {g["id"] for g in generated} <= node_ids
    assert all(e["weave_id"] == wid for e in events)

    # B's polled view agrees with what A wrote (server is the single authority)
    events_poll = cli(capsys, live_server, "--weave", wid, "events")
    assert {e["seq"] for e in events} <= {e["seq"] for e in events_poll["events"]}
