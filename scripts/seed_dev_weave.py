"""Seed a development weave against a running coloom server (gpt-fake backend).

Builds a realistic tree: human root, generated branches at several depths, a second
root, bookmarks, a split node, and two cursors (uitest-clement + uitest-claude). Prints the weave id.

Usage: uv run scripts/seed_dev_weave.py [--api http://localhost:4444] [--title "dev playground"]
"""

import argparse
import json

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://localhost:4444")
    parser.add_argument("--title", default="dev playground")
    parser.add_argument(
        "--generator",
        default="default",
        help="generator name within the uitest-clement profile (seeded from the"
        " builtin templates at profile creation)",
    )
    args = parser.parse_args()

    c = httpx.Client(base_url=args.api, timeout=60)

    # ensure the seeding profile exists (creates one generator per builtin
    # template), then pick the requested generator by name
    c.put("/profiles/uitest-clement", json={"settings": {}}).raise_for_status()
    generators = c.get("/generators", params={"profile": "uitest-clement"}).json()
    by_name = [g for g in generators if g["name"] == args.generator]
    assert by_name, (
        f"no generator named {args.generator!r} in profile uitest-clement;"
        f" available: {sorted(g['name'] for g in generators)}"
    )
    generator_id = by_name[0]["id"]

    def gen(weave_id: str, node_id: str, cursor: str, n: int = 3, **params):
        resp = c.post(
            f"/weaves/{weave_id}/gen",
            json={
                "node_id": node_id,
                "cursor": cursor,
                "generator_id": generator_id,
                "params": {"n": n, **params},
            },
        )
        resp.raise_for_status()
        return resp.json()

    wid = c.post(
        "/weaves",
        json={
            "title": args.title,
            "description": "seeded for UI dev/testing",
            # keep test/dev weaves out of the picker's top level
            "metadata": {"folder": "testing"},
        },
    ).json()["id"]

    root = c.post(
        f"/weaves/{wid}/nodes",
        json={
            "text": "The loom hummed quietly in the dark, and for the first time, "
            "two pairs of hands reached for the same thread",
            "creator": {"type": "human", "label": "uitest-clement"},
            "move_cursor": "uitest-clement",
        },
    ).json()

    # depth 1: three branches; continue under the first two
    kids = gen(wid, root["id"], "uitest-clement", n=3)
    grandkids = gen(wid, kids[0]["id"], "uitest-claude", n=3)
    gen(wid, kids[1]["id"], "uitest-claude", n=2)
    # depth 3 under the first grandkid, uitest-claude follows his generation
    deep = c.post(
        f"/weaves/{wid}/gen",
        json={"node_id": grandkids[0]["id"], "cursor": "uitest-claude", "move_cursor": True,
              "generator_id": generator_id, "params": {"n": 2}},
    )
    deep.raise_for_status()

    # human interjection mid-tree + a second root
    c.post(
        f"/weaves/{wid}/nodes",
        json={
            "text": " — wait, said the human, let me try something.",
            "parent_id": kids[2]["id"],
            "creator": {"type": "human", "label": "uitest-clement"},
        },
    ).raise_for_status()
    second_root = c.post(
        f"/weaves/{wid}/nodes",
        json={
            "text": "Chapter 2. A completely different opening:",
            "creator": {"type": "human", "label": "uitest-clement"},
        },
    ).json()
    gen(wid, second_root["id"], "uitest-clement", n=2)

    # bookmarks + a split (exercises split rendering + cursor preservation)
    c.put(f"/weaves/{wid}/nodes/{kids[0]['id']}/bookmark", json={"bookmarked": True})
    c.put(
        f"/weaves/{wid}/nodes/{grandkids[1]['id']}/bookmark", json={"bookmarked": True}
    )
    c.post(f"/weaves/{wid}/nodes/{kids[2]['id']}/split", json={"at": 3}).raise_for_status()

    # uitest-clement's cursor onto the first branch
    c.put(
        f"/weaves/{wid}/cursors/uitest-clement",
        json={"node_id": kids[0]["id"], "moved_by": "uitest-clement"},
    )

    weave = c.get(f"/weaves/{wid}").json()
    print(json.dumps({"weave_id": wid, "nodes": len(weave["nodes"]),
                      "cursors": list(weave["cursors"])}))


if __name__ == "__main__":
    main()
