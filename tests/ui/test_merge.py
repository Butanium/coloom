"""Merge-with-parent UI (item 10): context-menu entry + rebindable Ctrl+M, undo
via the toast button / Ctrl+Z following the BINDING rules of docs/events-api.md
"Merge with parent": restore brings the absorbed originals back, but child
migration is an edge edit — the merged node is deleted on undo ONLY when it
took no children.

Seed shape (conftest `weave`): root1 "loom hummed…" with 3 children; kids[1]
has 2 leaf children. uitest-clement's cursor sits on kids[0].
"""

import time

import pytest

IDENTITY = "uitest-clement"


@pytest.fixture()
def blank_weave(api):
    r = api.post("/weaves", json={"title": "ui-test merge weave"})
    r.raise_for_status()
    wid = r.json()["id"]
    yield wid
    api.delete(f"/weaves/{wid}")


def get_weave(api, wid):
    r = api.get(f"/weaves/{wid}")
    r.raise_for_status()
    return r.json()


def node_text(node):
    c = node["content"]
    return c["text"] if c["type"] == "snippet" else "".join(t["text"] for t in c["tokens"])


def wait_until(page, predicate, deadline_s=8.0, interval_ms=200):
    end = time.monotonic() + deadline_s
    val = predicate()
    while not val and time.monotonic() < end:
        page.wait_for_timeout(interval_ms)
        val = predicate()
    return val


def merged_node_of(w, p_id, n_id):
    return next(
        (
            n
            for n in w["nodes"].values()
            if n["metadata"].get("merged_from") == [p_id, n_id]
        ),
        None,
    )


def add_node(api, wid, text, parent, move_cursor=None):
    r = api.post(
        f"/weaves/{wid}/nodes",
        json={
            "text": text,
            "parent_id": parent,
            "creator": {"type": "human", "label": IDENTITY},
            "move_cursor": move_cursor,
        },
    )
    r.raise_for_status()
    return r.json()["id"]


def test_context_menu_merge_sibling_case_and_undo_keeps_merged_copy(
    weave, page_as, api
):
    """Merge a node whose parent has OTHER children (sibling case): a merged
    copy appears, the absorbed node is soft-deleted, its children migrate.
    Undo restores the node but KEEPS the merged copy (it carries the migrated
    children — deleting it would cascade onto them)."""
    page = page_as(IDENTITY, weave)
    w = get_weave(api, weave)
    root1 = next(r for r in w["roots"] if "loom hummed" in node_text(w["nodes"][r]))
    kids = w["nodes"][root1]["children"]
    victim = kids[1]
    migrated = list(w["nodes"][victim]["children"])
    assert len(migrated) == 2, "seed shape changed: kids[1] should have 2 children"

    # context menu on the tree row -> "merge with parent"
    page.locator(f'.sidebar [data-node-id="{victim}"]').click(button="right")
    menu = page.locator(".menu[role=menu]")
    menu.wait_for(state="visible", timeout=2000)
    menu.get_by_role("menuitem", name="merge with parent").click()

    def merged():
        now = get_weave(api, weave)
        m = merged_node_of(now, root1, victim)
        return (now, m) if m else None

    result = wait_until(page, merged)
    assert result, "merge produced no merged node"
    now, m = result
    assert victim not in now["nodes"], "absorbed node still live"
    assert root1 in now["nodes"], "sibling-case merge must not touch the parent"
    assert set(now["nodes"][root1]["children"]) == {kids[0], kids[2]}
    assert m["id"] in now["roots"], "merged copy of a root's child must be a new root"
    assert node_text(m) == node_text(w["nodes"][root1]) + node_text(w["nodes"][victim])
    assert set(m["children"]) == set(migrated), "children did not migrate to the merge"

    # undo via the toast button: node back, merged copy KEPT (it has children)
    page.get_by_test_id("toast-action").first.click()

    def undone():
        now2 = get_weave(api, weave)
        return now2 if victim in now2["nodes"] else None

    now2 = wait_until(page, undone)
    assert now2, "undo did not restore the absorbed node"
    assert now2["nodes"][victim]["children"] == [], (
        "restored node should be childless (children stay under the merged copy)"
    )
    assert m["id"] in now2["nodes"], (
        "undo deleted the merged copy despite migrated children (cascade hazard)"
    )
    assert set(now2["nodes"][m["id"]]["children"]) == set(migrated)


def test_ctrl_m_merge_in_place_leaf_and_ctrl_z_perfect_undo(blank_weave, page_as, api):
    """Ctrl+M merges the cursor node; only-child leaf = in-place case (both
    originals absorbed, cursor moves to the merged node). Ctrl+Z then is a
    PERFECT undo: originals restored, merged copy deleted, cursor re-parked."""
    page = page_as(IDENTITY, blank_weave)
    a = add_node(api, blank_weave, "Hello ", parent=None)
    b = add_node(api, blank_weave, "world", parent=a, move_cursor=IDENTITY)
    assert wait_until(
        page, lambda: "Hello world" in (page.locator(".doc").text_content() or "")
    ), "thread never rendered"

    page.keyboard.press("Control+m")

    def merged():
        now = get_weave(api, blank_weave)
        m = merged_node_of(now, a, b)
        return (now, m) if m else None

    result = wait_until(page, merged)
    assert result, "Ctrl+M merge produced no merged node"
    now, m = result
    assert a not in now["nodes"] and b not in now["nodes"], "in-place merge keeps originals?"
    assert now["roots"] == [m["id"]]
    assert node_text(m) == "Hello world"
    assert now["cursors"][IDENTITY]["node_id"] == m["id"], "cursor did not move to the merge"

    page.keyboard.press("Control+z")

    def undone():
        now2 = get_weave(api, blank_weave)
        if a in now2["nodes"] and b in now2["nodes"] and m["id"] not in now2["nodes"]:
            return now2
        return None

    now2 = wait_until(page, undone)
    assert now2, "Ctrl+Z did not perfectly undo the leaf merge"
    assert set(now2["nodes"]) == {a, b}, f"unexpected nodes after undo: {set(now2['nodes'])}"
    assert wait_until(
        page, lambda: get_weave(api, blank_weave)["cursors"][IDENTITY]["node_id"] == b
    ), "cursor not re-parked on the restored node"


def test_merge_disabled_on_roots(weave, page_as, api):
    """The context-menu entry is disabled for roots (no parent to merge into)."""
    page = page_as(IDENTITY, weave)
    w = get_weave(api, weave)
    root1 = next(r for r in w["roots"] if "loom hummed" in node_text(w["nodes"][r]))
    page.locator(f'.sidebar [data-node-id="{root1}"]').click(button="right")
    menu = page.locator(".menu[role=menu]")
    menu.wait_for(state="visible", timeout=2000)
    item = menu.get_by_role("menuitem", name="merge with parent")
    assert item.is_disabled(), "merge-with-parent must be disabled on a root"

