"""End-to-end smoke against live gpt4-base: starts a real coloom-server,
runs the agent co-weave loop through the CLI (new → gen → add → read → events),
and verifies a WebSocket client sees the edits live.

Run: uv run scripts/small-smokes/smoke_coweave_e2e.py
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from websockets.asyncio.client import connect

REPO = Path(__file__).resolve().parents[2]


def load_env() -> None:
    if "OPENAI_API_KEY" not in os.environ:
        for line in (REPO / ".env").read_text().splitlines():
            if line.startswith("OPENAI_API_KEY="):
                os.environ["OPENAI_API_KEY"] = line.split("=", 1)[1].strip()


def cli(server: str, *args: str) -> dict | list:
    out = subprocess.run(
        [sys.executable, "-m", "coloom.cli", "--server", server, *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


async def main() -> None:
    load_env()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = f"http://127.0.0.1:{port}"
    db = Path("/tmp/coloom_smoke.sqlite")
    db.unlink(missing_ok=True)

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "coloom.server",
            "--db",
            str(db),
            "--config",
            str(REPO / "coloom.example.yaml"),
            "--port",
            str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.time() + 15
        while True:
            try:
                httpx.get(f"{server}/weaves").raise_for_status()
                break
            except httpx.HTTPError:
                assert proc.poll() is None, "server died on startup"
                assert time.time() < deadline, "server did not come up"
                time.sleep(0.2)

        created = cli(
            server,
            "new",
            "--title",
            "smoke",
            "--text",
            "The two of them sat at the loom, human and machine,",
            "--as",
            "clem",
        )
        wid = created["weave"]["id"]
        print(f"weave {wid} created")

        # generators replaced presets: make one inheriting the builtin
        # "default" template (imported from the yaml at boot)
        generator = cli(
            server, "generators", "create",
            "--profile", "smoke", "--name", "smoke-gen", "--parent", "template:default",
        )
        print(f"generator {generator['id']} created (template:default)")

        ws_url = f"ws://127.0.0.1:{port}/ws?weave_id={wid}"
        async with connect(ws_url) as ws:
            gen_nodes = cli(
                server, "--weave", wid, "gen", "-n", "2", "--move-cursor",
                "--generator", generator["id"],
            )
            assert len(gen_nodes) == 2
            assert all(n["content"]["type"] == "tokens" for n in gen_nodes)
            tok = gen_nodes[0]["content"]["tokens"][0]
            assert tok["logprob"] is not None and tok["top_logprobs"]
            print(
                "gen ok:",
                repr("".join(t["text"] for t in gen_nodes[0]["content"]["tokens"])),
            )

            events = []
            while len(events) < 3:
                events.append(json.loads(await asyncio.wait_for(ws.recv(), 10)))
            # choice 0 is added + cursor moved in one transaction, then choice 1
            assert [e["type"] for e in events] == [
                "node_added",
                "cursor_moved",
                "node_added",
            ]
            print("websocket saw the generation live")

        cli(
            server,
            "--weave",
            wid,
            "add",
            "--parent",
            gen_nodes[0]["id"],
            "--text",
            " (and so it goes)",
            "--as",
            "claude",
            "--creator-type",
            "model",
            "--move-cursor",
        )
        thread = cli(server, "--weave", wid, "read")
        assert thread["content"].endswith(" (and so it goes)")
        print("cursor thread:", repr(thread["content"]))

        polled = cli(server, "--weave", wid, "events")
        assert [e["type"] for e in polled["events"]].count("node_added") == 4
        print(f"event log: {len(polled['events'])} events, cursor={polled['cursor']}")
        print("SMOKE OK")
    finally:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    asyncio.run(main())
