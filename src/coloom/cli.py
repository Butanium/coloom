"""coloom CLI — agent-facing HTTP client to the coloom server.

JSON on stdout, logs on stderr, non-interactive. Designed so an agent can run
`read` → `gen`/`add` → `events --since CURSOR` loops against a live weave.

Server URL:  --server or $COLOOM_SERVER (default http://127.0.0.1:4444).
Weave id:    --weave  or $COLOOM_WEAVE (so a session can be pinned once).
Cursor name: --cursor or $COLOOM_CURSOR or the logged-in profile (default
"default") — your named position in the weave; `read` and `gen` use it,
`cursor set` moves it (or anyone else's).
Profile:     `coloom profile login <name>` persists a profile (server-side
settings, same as the web login); --cursor and --as then default to it, so CLI
weaving is attributed exactly like web weaving. $COLOOM_PROFILE overrides.

Node ids accept any unique prefix (resolved against the live snapshot; soft-
deleted nodes need the full id). `gen`/`split`/`read` take --text to print
plain text instead of token JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import quote

import httpx

DEFAULT_SERVER = "http://127.0.0.1:4444"

PROFILE_FILE = (
    Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    / "coloom"
    / "profile"
)


def logged_in_profile() -> str | None:
    """$COLOOM_PROFILE beats the login file; None when not logged in."""
    env = os.environ.get("COLOOM_PROFILE")
    if env:
        return env
    try:
        return PROFILE_FILE.read_text().strip() or None
    except FileNotFoundError:
        return None


def fail(message: str, code: int = 1) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(code)


def out(data: Any) -> None:
    print(json.dumps(data, indent=2))


class Client:
    def __init__(self, server: str, profile: str | None = None):
        # X-Coloom-Profile attributes mutations (events' `by`) to the profile;
        # percent-encoded: header values can't carry non-ASCII ("clément") raw
        headers = {"X-Coloom-Profile": quote(profile)} if profile else {}
        self.http = httpx.Client(base_url=server, timeout=300.0, headers=headers)

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


def resolve_node_ref(client: Client, weave_id: str, ref: str | None) -> str | None:
    """Expand a unique node-id prefix to the full id (one snapshot fetch).
    Full-length ids skip the fetch; a ref matching no live node passes
    through untouched so soft-deleted nodes stay addressable by full id
    (`restore`). Ambiguous prefixes fail loudly."""
    if ref is None or len(ref) >= 32:
        return ref
    weave = client.request("GET", f"/weaves/{weave_id}")
    matches = [nid for nid in weave["nodes"] if nid.startswith(ref)]
    if len(matches) > 1:
        listing = ", ".join(sorted(m[:12] for m in matches))
        fail(f"node prefix {ref!r} is ambiguous: {listing}")
    return matches[0] if matches else ref


def node_text(node: dict) -> str:
    content = node["content"]
    if content["type"] == "snippet":
        return content["text"]
    return "".join(t["text"] for t in content["tokens"])


def print_nodes_text(nodes: list[dict]) -> None:
    """`gen --text` / `split --text` rendering: id header + plain text per
    node. The full token JSON stays one `read --node` away."""
    for node in nodes:
        print(f"=== {node['id']} ===")
        print(node_text(node))


def print_tree_text(weave: dict) -> None:
    """`read --tree --text` rendering: one indented line per node — id prefix,
    flags ([*] bookmark, [@name] cursors), creator, first ~90 chars."""
    cursors_at: dict[str, list[str]] = {}
    for name, cur in weave["cursors"].items():
        cursors_at.setdefault(cur["node_id"], []).append(name)

    def line(node_id: str, depth: int) -> None:
        node = weave["nodes"][node_id]
        flags = "*" if node["bookmarked"] else ""
        flags += "".join(f"@{n}" for n in sorted(cursors_at.get(node_id, [])))
        preview = " ".join(node_text(node).split())
        if len(preview) > 90:
            preview = preview[:90] + "…"
        creator = node["creator"]["label"] or node["creator"]["type"]
        print(f"{'  ' * depth}{node_id[:8]}{flags and ' [' + flags + ']'} ({creator}) {preview}")
        for child in node["children"]:
            line(child, depth + 1)

    for root in weave["roots"]:
        line(root, 0)


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
        node = client.request(
            "GET", f"/weaves/{wid}/nodes/{resolve_node_ref(client, wid, args.node)}"
        )
        if args.text:
            sys.stdout.write(node_text(node))
        else:
            out(node)
    elif args.tree:
        weave = client.request("GET", f"/weaves/{wid}")
        if args.text:
            print_tree_text(weave)
        else:
            out(weave)
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
                "parent_id": resolve_node_ref(client, wid, args.parent),
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
                json={
                    "node_id": resolve_node_ref(client, wid, args.node_id),
                    "moved_by": args.cursor,
                },
            )
        )
    else:  # rm
        name = args.name or args.cursor
        client.request("DELETE", f"/weaves/{wid}/cursors/{name}")
        out({"removed": name})


def resolve_generator_ref(
    client: Client, ref: str | None, profile: str | None
) -> str:
    """Turn --generator (an id or a name within the profile) into a generator id.
    Omitted: usable only when the profile has exactly one generator."""
    gens = (
        client.request("GET", "/generators", params={"profile": profile})
        if profile
        else []
    )
    if ref is None:
        if profile is None:
            fail(
                "pass --generator (an id), or `coloom profile login <name>`"
                " to resolve generators by name"
            )
        if len(gens) == 1:
            return gens[0]["id"]
        names = ", ".join(sorted(g["name"] for g in gens)) or "none"
        fail(
            "pass --generator (id or name); available for"
            f" profile {profile!r}: {names}"
        )
    if any(g["id"] == ref for g in gens):
        return ref
    named = [g for g in gens if g["name"] == ref]
    if len(named) == 1:
        return named[0]["id"]
    if len(named) > 1:
        fail(f"generator name {ref!r} is ambiguous; use an id")
    return ref  # not in the profile's list: assume it's an id


def cmd_gen(client: Client, args: argparse.Namespace) -> None:
    wid = require_weave(args)
    params = _parse_params(args.param)
    if args.n is not None:
        params["n"] = args.n
    generator_id = resolve_generator_ref(client, args.generator, logged_in_profile())
    nodes = client.request(
        "POST",
        f"/weaves/{wid}/gen",
        json={
            "node_id": resolve_node_ref(client, wid, args.node),
            "cursor": args.cursor,
            "generator_id": generator_id,
            "params": params,
            "move_cursor": args.move_cursor,
        },
    )
    if args.text:
        print_nodes_text(nodes)
    else:
        out(nodes)


def cmd_events(client: Client, args: argparse.Namespace) -> None:
    query: dict[str, Any] = {"since": args.since}
    if args.weave:
        query["weave_id"] = args.weave
    out(client.request("GET", "/events", params=query))


def cmd_bookmark(client: Client, args: argparse.Namespace) -> None:
    wid = require_weave(args)
    node_id = resolve_node_ref(client, wid, args.node_id)
    out(
        client.request(
            "PUT",
            f"/weaves/{wid}/nodes/{node_id}/bookmark",
            json={"bookmarked": not args.remove},
        )
    )


def cmd_rm(client: Client, args: argparse.Namespace) -> None:
    wid = require_weave(args)
    node_id = resolve_node_ref(client, wid, args.node_id)
    out(client.request("DELETE", f"/weaves/{wid}/nodes/{node_id}"))


def cmd_restore(client: Client, args: argparse.Namespace) -> None:
    wid = require_weave(args)
    # no prefix resolution: soft-deleted nodes aren't in the live snapshot
    out(client.request("POST", f"/weaves/{wid}/nodes/{args.node_id}/restore"))


def cmd_split(client: Client, args: argparse.Namespace) -> None:
    wid = require_weave(args)
    node_id = resolve_node_ref(client, wid, args.node_id)
    result = client.request(
        "POST", f"/weaves/{wid}/nodes/{node_id}/split", json={"at": args.at}
    )
    if args.text:
        print("--- head ---")
        print_nodes_text([result["head"]])
        print("--- tail ---")
        print_nodes_text([result["tail"]])
    else:
        out(result)


def cmd_merge(client: Client, args: argparse.Namespace) -> None:
    wid = require_weave(args)
    node_id = resolve_node_ref(client, wid, args.node_id)
    out(
        client.request(
            "POST", f"/weaves/{wid}/nodes/{node_id}/merge-with-parent"
        )
    )


def _parse_params(pairs: list[str]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for kv in pairs:
        if "=" not in kv:
            fail(f"--param expects key=value, got {kv!r}")
        key, value = kv.split("=", 1)
        try:
            params[key] = json.loads(value)
        except json.JSONDecodeError:
            params[key] = value
    return params


def cmd_templates(client: Client, args: argparse.Namespace) -> None:
    if args.templates_command == "list":
        out(client.request("GET", "/templates"))
    elif args.templates_command == "create":
        body: dict[str, Any] = {
            "name": args.name,
            "base_url": args.base_url,
            "model": args.model,
            "params": _parse_params(args.param),
        }
        if args.api_key is not None:
            body["api_key"] = args.api_key
        if args.api_key_env is not None:
            body["api_key_env"] = args.api_key_env
        out(client.request("POST", "/templates", json=body))
    elif args.templates_command == "promote":
        body = {"from_generator": args.generator_id}
        if args.name is not None:
            body["name"] = args.name
        out(client.request("POST", "/templates", json=body))
    else:  # rm
        client.request("DELETE", f"/templates/{args.id}")
        out({"removed": args.id})


def _generator_field_body(args: argparse.Namespace) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if args.param:
        body["params"] = _parse_params(args.param)
    for field in ("base_url", "model", "api_key", "api_key_env"):
        value = getattr(args, field)
        if value is not None:
            # the literal "null" clears a field back to inherited (PATCH) /
            # leaves it inherited (POST)
            body[field] = None if value == "null" else value
    return body


def cmd_generators(client: Client, args: argparse.Namespace) -> None:
    profile = args.profile or logged_in_profile()
    if args.generators_command == "list":
        if not profile:
            fail("pass --profile or `coloom profile login <name>`")
        out(client.request("GET", "/generators", params={"profile": profile}))
    elif args.generators_command == "create":
        if not profile:
            fail("pass --profile or `coloom profile login <name>`")
        body = {"profile": profile, "name": args.name, **_generator_field_body(args)}
        if args.parent is not None:
            kind, _, ref = args.parent.partition(":")
            if kind not in ("template", "generator") or not ref:
                fail("--parent expects template:<id-or-name> or generator:<id>")
            if kind == "template":
                templates = client.request("GET", "/templates")
                named = [t for t in templates if t["name"] == ref]
                if len(named) == 1 and not any(t["id"] == ref for t in templates):
                    ref = named[0]["id"]
            body["parent"] = {"kind": kind, "id": ref}
        out(client.request("POST", "/generators", json=body))
    elif args.generators_command == "update":
        body = _generator_field_body(args)
        if args.name is not None:
            body["name"] = args.name
        out(client.request("PATCH", f"/generators/{args.id}", json=body))
    else:  # rm
        client.request("DELETE", f"/generators/{args.id}")
        out({"removed": args.id})


def cmd_profile(client: Client, args: argparse.Namespace) -> None:
    if args.profile_command == "login":
        name = args.name.strip()
        if not name:
            fail("profile name required")
        # GET first — login must never wipe an existing profile's settings
        resp = client.http.get(f"/profiles/{name}")
        if resp.status_code == 404:
            prof = client.request("PUT", f"/profiles/{name}", json={"settings": {}})
        elif resp.status_code >= 400:
            fail(f"HTTP {resp.status_code}: {resp.text}")
        else:
            prof = resp.json()
        PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PROFILE_FILE.write_text(name + "\n")
        out({"logged_in": name, "settings": prof["settings"]})
    elif args.profile_command == "whoami":
        out({"profile": logged_in_profile()})
    elif args.profile_command == "list":
        out(client.request("GET", "/profiles"))
    else:  # logout
        PROFILE_FILE.unlink(missing_ok=True)
        out({"logged_in": None})


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
        default=None,  # resolved in main(): $COLOOM_CURSOR > profile > "default"
        help="your cursor name (default: $COLOOM_CURSOR, else the logged-in profile)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("new", help="create a weave (optionally with a root node)")
    p.add_argument("--title", default="Untitled weave")
    p.add_argument("--description", default="")
    p.add_argument("--text", help="root node text (your cursor moves there)")
    p.add_argument("--as", dest="as_label", default=None, help="creator label (default: the logged-in profile)")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("list", help="list weaves")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("read", help="read your cursor's thread (default), tree, or node")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--tree", action="store_true", help="full weave snapshot")
    group.add_argument("--node", help="single node by id (or unique prefix)")
    p.add_argument(
        "--text",
        action="store_true",
        help="plain text instead of JSON (--tree: indented one-line-per-node outline)",
    )
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("add", help="add a manual branch")
    p.add_argument(
        "--parent", help="parent node id or unique prefix (omit for a new root)"
    )
    p.add_argument("--text")
    p.add_argument("--stdin", action="store_true", help="read text from stdin")
    p.add_argument(
        "--move-cursor", action="store_true", help="move your cursor to the new node"
    )
    p.add_argument("--as", dest="as_label", default=None, help="creator label (default: the logged-in profile)")
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
    cp.add_argument("node_id", help="node id or unique prefix")
    cp.add_argument("--name", help="cursor to move (default: your own)")
    cp = cursor_sub.add_parser("rm", help="remove a cursor")
    cp.add_argument("--name", help="cursor to remove (default: your own)")
    p.set_defaults(func=cmd_cursor)

    p = sub.add_parser("gen", help="generate branches from a node via the base model")
    p.add_argument(
        "--node", help="parent node id or unique prefix (default: your cursor)"
    )
    p.add_argument(
        "--text",
        action="store_true",
        help="print id + text per branch instead of full token JSON",
    )
    p.add_argument(
        "--generator",
        help="generator id, or name within your profile (default: the profile's"
        " only generator, if unique)",
    )
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

    p = sub.add_parser("rm", help="soft-delete a node and its subtree (restorable)")
    p.add_argument("node_id")
    p.set_defaults(func=cmd_rm)

    p = sub.add_parser("restore", help="restore a soft-deleted node + its subtree")
    p.add_argument("node_id")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("split", help="split a node (token index / char offset)")
    p.add_argument("node_id")
    p.add_argument("--at", type=int, required=True)
    p.add_argument(
        "--text",
        action="store_true",
        help="print head/tail id + text instead of full token JSON",
    )
    p.set_defaults(func=cmd_split)

    p = sub.add_parser(
        "merge",
        help="merge a node into its parent (new merged node; absorbed nodes"
        " soft-deleted, restorable)",
    )
    p.add_argument("node_id")
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("templates", help="manage templates (server-global shelf)")
    templates_sub = p.add_subparsers(dest="templates_command", required=True)
    templates_sub.add_parser("list", help="list all templates (builtin + promoted)")
    sp = templates_sub.add_parser("create", help="create a template")
    sp.add_argument("--name", required=True)
    sp.add_argument("--base-url", required=True)
    sp.add_argument("--model", required=True)
    sp.add_argument("--api-key", help="literal bearer key (redacted in responses)")
    sp.add_argument("--api-key-env", help="env var holding the key (mutually excl.)")
    sp.add_argument("--param", action="append", default=[], metavar="K=V")
    sp = templates_sub.add_parser(
        "promote", help="promote a generator's resolved fields into a template"
    )
    sp.add_argument("generator_id")
    sp.add_argument("--name", help="template name (default: the generator's)")
    sp = templates_sub.add_parser("rm", help="delete a template (403 on builtins)")
    sp.add_argument("id")
    p.set_defaults(func=cmd_templates)

    p = sub.add_parser("generators", help="manage your per-profile generators")
    generators_sub = p.add_subparsers(dest="generators_command", required=True)
    gp = generators_sub.add_parser("list", help="list a profile's generators")
    gp.add_argument("--profile", help="default: the logged-in profile")
    gp = generators_sub.add_parser("create", help="create a generator")
    gp.add_argument("--name", required=True)
    gp.add_argument("--profile", help="default: the logged-in profile")
    gp.add_argument(
        "--parent",
        metavar="KIND:REF",
        help="template:<id-or-name> or generator:<id> to inherit from",
    )
    gp.add_argument("--base-url")
    gp.add_argument("--model")
    gp.add_argument("--api-key")
    gp.add_argument("--api-key-env")
    gp.add_argument("--param", action="append", default=[], metavar="K=V")
    gp = generators_sub.add_parser(
        "update", help="patch a generator (field 'null' / param K=null clears"
        " an override back to inherited)"
    )
    gp.add_argument("id")
    gp.add_argument("--name")
    gp.add_argument("--profile", help=argparse.SUPPRESS)  # unused; profile immutable
    gp.add_argument("--base-url")
    gp.add_argument("--model")
    gp.add_argument("--api-key")
    gp.add_argument("--api-key-env")
    gp.add_argument("--param", action="append", default=[], metavar="K=V")
    gp = generators_sub.add_parser("rm", help="delete a generator (children flatten)")
    gp.add_argument("id")
    gp.add_argument("--profile", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_generators)

    p = sub.add_parser(
        "profile", help="login as a profile (--cursor and --as default to it)"
    )
    profile_sub = p.add_subparsers(dest="profile_command", required=True)
    pp = profile_sub.add_parser(
        "login", help="select/create a profile; persisted to ~/.config/coloom/profile"
    )
    pp.add_argument("name")
    profile_sub.add_parser("whoami", help="print the logged-in profile")
    profile_sub.add_parser("list", help="list profiles on the server")
    profile_sub.add_parser("logout", help="forget the logged-in profile")
    p.set_defaults(func=cmd_profile)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    # cursor/creator attribution defaults: explicit flag > env > profile > legacy
    profile = logged_in_profile()
    if args.cursor is None:
        args.cursor = os.environ.get("COLOOM_CURSOR") or profile or "default"
    if getattr(args, "as_label", "absent") is None:
        args.as_label = profile or "human"
    client = Client(args.server, profile=profile)
    args.func(client, args)


if __name__ == "__main__":
    main()
