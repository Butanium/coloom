"""coloom CLI — agent-facing HTTP client to the coloom server.

JSON on stdout, logs on stderr, non-interactive. Designed so an agent can run
`read` → `gen`/`add` → `events --since CURSOR` loops against a live weave.

Server URL:  --server or $COLOOM_SERVER (default http://127.0.0.1:4444).
Weave id:    --weave  or $COLOOM_WEAVE (so a session can be pinned once).
Cursor name: --cursor or $COLOOM_CURSOR (default "default") — your named position
in the weave; `read` and `gen` use it, `cursor set` moves it (or anyone else's).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, NoReturn

import httpx

DEFAULT_SERVER = "http://127.0.0.1:4444"


def fail(message: str, code: int = 1) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(code)


def out(data: Any) -> None:
    print(json.dumps(data, indent=2))


class Client:
    def __init__(self, server: str):
        self.http = httpx.Client(base_url=server, timeout=300.0)

    def request(self, method: str, path: str, **kwargs) -> Any:
        try:
            resp = self.http.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            fail(f"cannot reach server: {e}")
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            fail(f"HTTP {resp.status_code}: {detail}")
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()


def require_weave(args: argparse.Namespace) -> str:
    if args.weave:
        return args.weave
    fail("no weave id: pass --weave or set $COLOOM_WEAVE")


# ------------------------------------------------------------ commands


def cmd_new(client: Client, args: argparse.Namespace) -> None:
    weave = client.request(
        "POST", "/weaves", json={"title": args.title, "description": args.description}
    )
    result: dict[str, Any] = {"weave": weave}
    if args.text is not None:
        result["root"] = client.request(
            "POST",
            f"/weaves/{weave['id']}/nodes",
            json={
                "text": args.text,
                "creator": {"type": "human", "label": args.as_label},
                "move_cursor": args.cursor,
            },
        )
    out(result)


def cmd_list(client: Client, args: argparse.Namespace) -> None:
    out(client.request("GET", "/weaves"))


def cmd_read(client: Client, args: argparse.Namespace) -> None:
    wid = require_weave(args)
    if args.node:
        out(client.request("GET", f"/weaves/{wid}/nodes/{args.node}"))
    elif args.tree:
        out(client.request("GET", f"/weaves/{wid}"))
    else:
        thread = client.request("GET", f"/weaves/{wid}/cursors/{args.cursor}/thread")
        if args.text:
            sys.stdout.write(thread["content"])
        else:
            out(thread)


def cmd_add(client: Client, args: argparse.Namespace) -> None:
    text = sys.stdin.read() if args.stdin else args.text
    if text is None:
        fail("provide --text or --stdin")
    wid = require_weave(args)
    out(
        client.request(
            "POST",
            f"/weaves/{wid}/nodes",
            json={
                "text": text,
                "parent_id": args.parent,
                "creator": {"type": args.creator_type, "label": args.as_label},
                "move_cursor": args.cursor if args.move_cursor else None,
            },
        )
    )


def cmd_cursor(client: Client, args: argparse.Namespace) -> None:
    wid = require_weave(args)
    if args.cursor_command == "list":
        out(client.request("GET", f"/weaves/{wid}/cursors"))
    elif args.cursor_command == "set":
        name = args.name or args.cursor
        out(
            client.request(
                "PUT",
                f"/weaves/{wid}/cursors/{name}",
                json={"node_id": args.node_id, "moved_by": args.cursor},
            )
        )
    else:  # rm
        name = args.name or args.cursor
        client.request("DELETE", f"/weaves/{wid}/cursors/{name}")
        out({"removed": name})


def cmd_gen(client: Client, args: argparse.Namespace) -> None:
    wid = require_weave(args)
    params: dict[str, Any] = {}
    for kv in args.param:
        if "=" not in kv:
            fail(f"--param expects key=value, got {kv!r}")
        key, value = kv.split("=", 1)
        try:
            params[key] = json.loads(value)
        except json.JSONDecodeError:
            params[key] = value
    if args.n is not None:
        params["n"] = args.n
    out(
        client.request(
            "POST",
            f"/weaves/{wid}/gen",
            json={
                "node_id": args.node,
                "cursor": args.cursor,
                "preset": args.preset,
                "params": params,
                "move_cursor": args.move_cursor,
            },
        )
    )


def cmd_events(client: Client, args: argparse.Namespace) -> None:
    query: dict[str, Any] = {"since": args.since}
    if args.weave:
        query["weave_id"] = args.weave
    out(client.request("GET", "/events", params=query))


def cmd_bookmark(client: Client, args: argparse.Namespace) -> None:
    wid = require_weave(args)
    out(
        client.request(
            "PUT",
            f"/weaves/{wid}/nodes/{args.node_id}/bookmark",
            json={"bookmarked": not args.remove},
        )
    )


def cmd_rm(client: Client, args: argparse.Namespace) -> None:
    wid = require_weave(args)
    out(client.request("DELETE", f"/weaves/{wid}/nodes/{args.node_id}"))


def cmd_split(client: Client, args: argparse.Namespace) -> None:
    wid = require_weave(args)
    out(
        client.request(
            "POST", f"/weaves/{wid}/nodes/{args.node_id}/split", json={"at": args.at}
        )
    )


# ------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coloom", description=__doc__)
    parser.add_argument(
        "--server", default=os.environ.get("COLOOM_SERVER", DEFAULT_SERVER)
    )
    parser.add_argument(
        "--weave", default=os.environ.get("COLOOM_WEAVE"), help="weave id"
    )
    parser.add_argument(
        "--cursor",
        default=os.environ.get("COLOOM_CURSOR", "default"),
        help="your cursor name (your named position in the weave)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("new", help="create a weave (optionally with a root node)")
    p.add_argument("--title", default="Untitled weave")
    p.add_argument("--description", default="")
    p.add_argument("--text", help="root node text (your cursor moves there)")
    p.add_argument("--as", dest="as_label", default="human", help="creator label")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("list", help="list weaves")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("read", help="read your cursor's thread (default), tree, or node")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--tree", action="store_true", help="full weave snapshot")
    group.add_argument("--node", help="single node by id")
    p.add_argument("--text", action="store_true", help="plain text instead of JSON")
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("add", help="add a manual branch")
    p.add_argument("--parent", help="parent node id (omit for a new root)")
    p.add_argument("--text")
    p.add_argument("--stdin", action="store_true", help="read text from stdin")
    p.add_argument(
        "--move-cursor", action="store_true", help="move your cursor to the new node"
    )
    p.add_argument("--as", dest="as_label", default="human", help="creator label")
    p.add_argument(
        "--creator-type",
        choices=["human", "model"],
        default="human",
        help="attribution type (an agent writing prose by hand = model)",
    )
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("cursor", help="list / set / rm named cursors")
    cursor_sub = p.add_subparsers(dest="cursor_command", required=True)
    cp = cursor_sub.add_parser("list", help="all cursors in the weave")
    cp = cursor_sub.add_parser(
        "set", help="move a cursor (yours by default; --name for someone else's)"
    )
    cp.add_argument("node_id")
    cp.add_argument("--name", help="cursor to move (default: your own)")
    cp = cursor_sub.add_parser("rm", help="remove a cursor")
    cp.add_argument("--name", help="cursor to remove (default: your own)")
    p.set_defaults(func=cmd_cursor)

    p = sub.add_parser("gen", help="generate branches from a node via the base model")
    p.add_argument("--node", help="parent node id (default: your cursor)")
    p.add_argument("--preset", help="named preset from the server config")
    p.add_argument("-n", type=int, help="number of completions")
    p.add_argument(
        "--move-cursor",
        action="store_true",
        help="move your cursor to the first generated node",
    )
    p.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="K=V",
        help="sampling param override (JSON-parsed value), repeatable",
    )
    p.set_defaults(func=cmd_gen)

    p = sub.add_parser("events", help="poll change events newer than a cursor")
    p.add_argument("--since", type=int, default=0)
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("bookmark", help="bookmark a node")
    p.add_argument("node_id")
    p.add_argument("--remove", action="store_true")
    p.set_defaults(func=cmd_bookmark)

    p = sub.add_parser("rm", help="remove a node and its subtree")
    p.add_argument("node_id")
    p.set_defaults(func=cmd_rm)

    p = sub.add_parser("split", help="split a node (token index / char offset)")
    p.add_argument("node_id")
    p.add_argument("--at", type=int, required=True)
    p.set_defaults(func=cmd_split)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    client = Client(args.server)
    args.func(client, args)


if __name__ == "__main__":
    main()
