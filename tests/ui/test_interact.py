"""Adversarial UI tests: context menu (ContextMenu.svelte) + keyboard suite
(keyboard.svelte.ts).

Every mutation is verified through the REST API, not just the DOM. Each test
seeds its own weave (the `weave` fixture). Identity under test: "uitest-clement".

Seeded shape (scripts/seed_dev_weave.py):
  root1 "The loom hummed quietly…"  (3 children; uitest-clement's cursor on child[0],
                                     which is bookmarked and has 3 children)
  root2 "Chapter 2. A completely different opening:" (2 generated children)
  a human interjection leaf " — wait, said the human…" under a split head
  uitest-claude's cursor on a depth-3 leaf
"""

import time

import pytest
from playwright.sync_api import expect

# generations on the fake backend: 0.4-1.2s + WS refetch
GEN_DEADLINE = 8.0
# time for the client to refetch the weave after a WS event (it polls nothing;
# the refetch is near-instant, but give the round-trip generous room)
CLIENT_SYNC = 0.6


# ---------------------------------------------------------------- helpers


def poll(fn, *, timeout=GEN_DEADLINE, interval=0.2, desc="condition"):
    """Poll `fn` until truthy; return its value. Fail loudly on timeout."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    pytest.fail(f"timed out ({timeout}s) waiting for {desc}; last={last!r}")


def weave_json(api, wid):
    r = api.get(f"/weaves/{wid}")
    r.raise_for_status()
    return r.json()


def get_cursors(api, wid):
    r = api.get(f"/weaves/{wid}/cursors")
    r.raise_for_status()
    return r.json()


def ntext(node) -> str:
    c = node["content"]
    if c["type"] == "snippet":
        return c["text"]
    return "".join(t["text"] for t in c["tokens"])


def snip(text: str, n: int = 60) -> str:
    """Whitespace-normalized prefix, for Playwright has_text matching."""
    return " ".join(text.split())[:n]


def find_by_prefix(w, prefix: str) -> str:
    ids = [nid for nid, n in w["nodes"].items() if ntext(n).startswith(prefix)]
    assert len(ids) == 1, f"prefix {prefix!r} matched {len(ids)} nodes"
    return ids[0]


def seed_ids(w):
    """Resolve the well-known seed nodes from a fresh weave snapshot."""
    root1 = find_by_prefix(w, "The loom hummed")
    root2 = find_by_prefix(w, "Chapter 2.")
    kids = w["nodes"][root1]["children"]  # creation order: 3 generated branches
    interject = find_by_prefix(w, " — wait, said the human")
    return {"root1": root1, "root2": root2, "kids": kids, "interject": interject}


def card(page, node):
    """Locator for the canvas card of a node (matched by unique text prefix)."""
    text = ntext(node)
    assert text.strip(), "card() needs a node with text"
    loc = page.locator(".canvas .text").filter(has_text=snip(text))
    return loc


def fit_weave(page):
    """ctrl+0 = fit whole weave, so every card is inside the cull bounds."""
    page.keyboard.press("Control+0")
    page.wait_for_timeout(200)


def open_menu_on(page, node):
    """Right-click the node's card; return (menu locator, click x, click y)."""
    c = card(page, node)
    expect(c).to_have_count(1)
    box = c.bounding_box()
    assert box is not None
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.click(x, y, button="right")
    menu = page.locator(".menu[role='menu']")
    expect(menu).to_be_visible()
    return menu, x, y


def menu_item(page, name: str):
    return page.get_by_role("menuitem", name=name, exact=True)


def click_item(page, name: str):
    item = menu_item(page, name)
    expect(item).to_be_enabled()
    item.click()
    # the menu closes synchronously before firing the action
    expect(page.locator(".menu[role='menu']")).to_have_count(0)


def gen_event_count(api, wid) -> int:
    r = api.get(f"/events?since=0&weave_id={wid}")
    r.raise_for_status()
    return sum(1 for e in r.json()["events"] if e["type"] == "gen_started")


# ============================================================ context menu


def test_menu_opens_at_pointer_escape_and_clickaway_close(page_as, weave, api):
    page = page_as("uitest-clement", weave)
    fit_weave(page)
    w = weave_json(api, weave)
    ids = seed_ids(w)

    menu, x, y = open_menu_on(page, w["nodes"][ids["root1"]])
    # opens at the pointer (clamped to the viewport)
    vw, vh = page.evaluate("[window.innerWidth, window.innerHeight]")
    mb = menu.bounding_box()
    assert mb is not None
    expected_left = max(4, min(x, vw - mb["width"] - 4))
    expected_top = max(4, min(y, vh - mb["height"] - 4))
    assert abs(mb["x"] - expected_left) < 3, f"menu x {mb['x']} != {expected_left}"
    assert abs(mb["y"] - expected_top) < 3, f"menu y {mb['y']} != {expected_top}"

    # Escape closes
    page.keyboard.press("Escape")
    expect(page.locator(".menu[role='menu']")).to_have_count(0)

    # reopen, then click away (pointerdown outside the menu) closes
    open_menu_on(page, w["nodes"][ids["root1"]])
    canvas = page.locator(".canvas").bounding_box()
    page.mouse.click(canvas["x"] + 8, canvas["y"] + 8)
    expect(page.locator(".menu[role='menu']")).to_have_count(0)


def test_generate_here_adds_children_cursor_unmoved(page_as, weave, api):
    page = page_as("uitest-clement", weave)
    fit_weave(page)
    w = weave_json(api, weave)
    ids = seed_ids(w)
    root1 = ids["root1"]
    before_children = list(w["nodes"][root1]["children"])
    cursor_before = get_cursors(api, weave)["uitest-clement"]["node_id"]

    open_menu_on(page, w["nodes"][root1])
    click_item(page, "generate here")

    # default preset has n=3
    w2 = poll(
        lambda: (lambda ww: ww if len(ww["nodes"][root1]["children"]) > len(before_children) else None)(
            weave_json(api, weave)
        ),
        desc="generated children under root1",
    )
    # let the burst finish, then check the exact count
    page.wait_for_timeout(1500)
    w2 = weave_json(api, weave)
    new_children = [c for c in w2["nodes"][root1]["children"] if c not in before_children]
    assert len(new_children) == 3, f"expected 3 new children, got {len(new_children)}"
    for nid in new_children:
        assert w2["nodes"][nid]["creator"]["type"] == "model"
    # cursor must NOT move on plain "generate here"
    assert get_cursors(api, weave)["uitest-clement"]["node_id"] == cursor_before


def test_generate_and_follow_moves_my_cursor(page_as, weave, api):
    page = page_as("uitest-clement", weave)
    fit_weave(page)
    w = weave_json(api, weave)
    target = seed_ids(w)["interject"]  # a leaf
    assert w["nodes"][target]["children"] == []

    open_menu_on(page, w["nodes"][target])
    click_item(page, "generate and follow")

    w2 = poll(
        lambda: (lambda ww: ww if ww["nodes"][target]["children"] else None)(weave_json(api, weave)),
        desc="generated children under the interjection leaf",
    )
    page.wait_for_timeout(1500)
    w2 = weave_json(api, weave)
    children = w2["nodes"][target]["children"]
    assert len(children) == 3
    cur = get_cursors(api, weave)["uitest-clement"]
    assert cur["node_id"] in children, (
        f"cursor {cur['node_id']} not among generated children {children}"
    )


def test_add_child_and_add_sibling(page_as, weave, api):
    page = page_as("uitest-clement", weave)
    fit_weave(page)
    w = weave_json(api, weave)
    ids = seed_ids(w)
    root2 = ids["root2"]
    leaf = w["nodes"][root2]["children"][0]  # generated leaf under root2

    # --- add child on a leaf
    open_menu_on(page, w["nodes"][leaf])
    click_item(page, "add child")
    w2 = poll(
        lambda: (lambda ww: ww if ww["nodes"][leaf]["children"] else None)(weave_json(api, weave)),
        timeout=4,
        desc="new child under the leaf",
    )
    child = w2["nodes"][leaf]["children"][0]
    assert ntext(w2["nodes"][child]) == ""
    # compare fields, not the whole dict — the serialized creator carries
    # optional nulls (color, id)
    creator = w2["nodes"][child]["creator"]
    assert (creator["type"], creator["label"]) == ("human", "uitest-clement")
    poll(
        lambda: get_cursors(api, weave)["uitest-clement"]["node_id"] == child,
        timeout=4,
        desc="cursor moved to the new child",
    )

    # --- add sibling on the same (non-root) node → child of root2
    page.wait_for_timeout(CLIENT_SYNC * 1000)
    fit_weave(page)
    root2_children_before = list(w2["nodes"][root2]["children"])
    open_menu_on(page, w2["nodes"][leaf])
    click_item(page, "add sibling")
    w3 = poll(
        lambda: (lambda ww: ww
                 if len(ww["nodes"][root2]["children"]) > len(root2_children_before)
                 else None)(weave_json(api, weave)),
        timeout=4,
        desc="new sibling under root2",
    )
    sibling = [c for c in w3["nodes"][root2]["children"] if c not in root2_children_before][0]
    assert w3["nodes"][sibling]["parents"] == [root2]
    assert ntext(w3["nodes"][sibling]) == ""
    poll(
        lambda: get_cursors(api, weave)["uitest-clement"]["node_id"] == sibling,
        timeout=4,
        desc="cursor moved to the new sibling",
    )

    # --- add sibling must be DISABLED on a root
    page.wait_for_timeout(CLIENT_SYNC * 1000)
    fit_weave(page)
    open_menu_on(page, w3["nodes"][ids["root1"]])
    expect(menu_item(page, "add sibling")).to_be_disabled()
    page.keyboard.press("Escape")


def test_bookmark_toggle_via_menu(page_as, weave, api):
    page = page_as("uitest-clement", weave)
    fit_weave(page)
    w = weave_json(api, weave)
    ids = seed_ids(w)
    target = ids["kids"][1]  # not bookmarked in the seed
    assert not w["nodes"][target]["bookmarked"]

    open_menu_on(page, w["nodes"][target])
    # label reflects current state
    expect(menu_item(page, "bookmark")).to_be_visible()
    click_item(page, "bookmark")
    poll(
        lambda: weave_json(api, weave)["nodes"][target]["bookmarked"],
        timeout=4,
        desc="node bookmarked",
    )
    assert target in weave_json(api, weave)["bookmarks"]

    # toggle back off — menu label must have flipped
    page.wait_for_timeout(CLIENT_SYNC * 1000)
    open_menu_on(page, w["nodes"][target])
    expect(menu_item(page, "remove bookmark")).to_be_visible()
    click_item(page, "remove bookmark")
    poll(
        lambda: not weave_json(api, weave)["nodes"][target]["bookmarked"],
        timeout=4,
        desc="bookmark removed",
    )
    assert target not in weave_json(api, weave)["bookmarks"]


def test_move_my_cursor_and_summon_claude(page_as, weave, api):
    page = page_as("uitest-clement", weave)
    fit_weave(page)
    w = weave_json(api, weave)
    ids = seed_ids(w)
    kid1_children = w["nodes"][ids["kids"][1]]["children"]
    target_mine = kid1_children[0]
    target_claude = w["nodes"][ids["root2"]]["children"][1]
    assert get_cursors(api, weave)["uitest-clement"]["node_id"] != target_mine
    assert get_cursors(api, weave)["uitest-claude"]["node_id"] != target_claude

    open_menu_on(page, w["nodes"][target_mine])
    click_item(page, "move my cursor here")
    cur = poll(
        lambda: (lambda c: c if c["node_id"] == target_mine else None)(
            get_cursors(api, weave)["uitest-clement"]
        ),
        timeout=4,
        desc="uitest-clement cursor moved",
    )
    assert cur["moved_by"] == "uitest-clement"

    # summon uitest-claude: uitest-claude's cursor moves, attributed to uitest-clement
    page.wait_for_timeout(CLIENT_SYNC * 1000)
    fit_weave(page)
    open_menu_on(page, w["nodes"][target_claude])
    click_item(page, "summon uitest-claude here")
    cur = poll(
        lambda: (lambda c: c if c["node_id"] == target_claude else None)(
            get_cursors(api, weave)["uitest-claude"]
        ),
        timeout=4,
        desc="uitest-claude cursor summoned",
    )
    assert cur["moved_by"] == "uitest-clement", f"moved_by={cur['moved_by']}, want uitest-clement"
    # uitest-clement's own cursor did not move
    assert get_cursors(api, weave)["uitest-clement"]["node_id"] == target_mine


def test_collapse_expand_via_menu(page_as, weave, api):
    page = page_as("uitest-clement", weave)
    fit_weave(page)
    w = weave_json(api, weave)
    ids = seed_ids(w)
    branch = ids["kids"][1]  # has 2 generated children
    children = w["nodes"][branch]["children"]
    assert len(children) == 2

    for c in children:
        expect(card(page, w["nodes"][c])).to_have_count(1)

    open_menu_on(page, w["nodes"][branch])
    click_item(page, "collapse subtree")
    for c in children:
        expect(card(page, w["nodes"][c])).to_have_count(0)
    # the collapsed node itself stays visible
    expect(card(page, w["nodes"][branch])).to_have_count(1)

    # menu now offers expand; children come back
    open_menu_on(page, w["nodes"][branch])
    click_item(page, "expand subtree")
    for c in children:
        expect(card(page, w["nodes"][c])).to_have_count(1)

    # on a leaf, collapse / expand-all / delete-children are disabled
    open_menu_on(page, w["nodes"][ids["interject"]])
    expect(menu_item(page, "collapse subtree")).to_be_disabled()
    expect(menu_item(page, "expand all below")).to_be_disabled()
    expect(menu_item(page, "delete children")).to_be_disabled()
    page.keyboard.press("Escape")


def test_copy_text_and_copy_thread_text(page_as, weave, api):
    page = page_as("uitest-clement", weave)
    fit_weave(page)
    w = weave_json(api, weave)
    ids = seed_ids(w)
    kid0 = ids["kids"][0]
    target = w["nodes"][kid0]["children"][0]  # depth 2: root1 > kid0 > target

    open_menu_on(page, w["nodes"][target])
    click_item(page, "copy text")
    page.wait_for_timeout(300)
    clip = page.evaluate("() => navigator.clipboard.readText()")
    assert clip == ntext(w["nodes"][target]), (
        f"clipboard {clip!r} != node text {ntext(w['nodes'][target])!r}"
    )

    open_menu_on(page, w["nodes"][target])
    click_item(page, "copy thread text")
    page.wait_for_timeout(300)
    clip = page.evaluate("() => navigator.clipboard.readText()")
    expected = (
        ntext(w["nodes"][ids["root1"]]) + ntext(w["nodes"][kid0]) + ntext(w["nodes"][target])
    )
    assert clip == expected, f"thread clipboard {clip!r} != {expected!r}"


def test_delete_children_then_delete_node(page_as, weave, api):
    page = page_as("uitest-clement", weave)
    fit_weave(page)
    w = weave_json(api, weave)
    ids = seed_ids(w)
    branch = ids["kids"][1]
    children = list(w["nodes"][branch]["children"])
    assert len(children) == 2

    # delete children: children gone, the node itself stays
    open_menu_on(page, w["nodes"][branch])
    click_item(page, "delete children")
    poll(
        lambda: weave_json(api, weave)["nodes"][branch]["children"] == [],
        timeout=4,
        desc="children deleted",
    )
    w2 = weave_json(api, weave)
    assert branch in w2["nodes"]
    for c in children:
        assert c not in w2["nodes"], f"child {c} survived delete children"

    # delete node on root2: whole subtree (root2 + its 2 children) gone
    page.wait_for_timeout(CLIENT_SYNC * 1000)
    fit_weave(page)
    root2 = ids["root2"]
    subtree = [root2, *w2["nodes"][root2]["children"]]
    open_menu_on(page, w2["nodes"][root2])
    click_item(page, "delete node")
    poll(
        lambda: root2 not in weave_json(api, weave)["nodes"],
        timeout=4,
        desc="root2 deleted",
    )
    w3 = weave_json(api, weave)
    for nid in subtree:
        assert nid not in w3["nodes"], f"subtree node {nid} survived delete node"
    assert w3["roots"] == [ids["root1"]]


# ============================================================ keyboard suite


def press_and_wait_cursor(page, api, wid, key, expected, desc):
    page.keyboard.press(key)
    poll(
        lambda: get_cursors(api, wid)["uitest-clement"]["node_id"] == expected,
        timeout=4,
        desc=desc,
    )
    # let the client refetch before the next navigation reads local state
    page.wait_for_timeout(CLIENT_SYNC * 1000)


def test_arrow_navigation_moves_my_cursor(page_as, weave, api):
    page = page_as("uitest-clement", weave)
    w = weave_json(api, weave)
    ids = seed_ids(w)
    root1, kids = ids["root1"], ids["kids"]
    assert get_cursors(api, weave)["uitest-clement"]["node_id"] == kids[0]

    # left → parent (root1)
    press_and_wait_cursor(page, api, weave, "ArrowLeft", root1, "left → parent")
    # left again on a root: clamp, no move
    page.keyboard.press("ArrowLeft")
    page.wait_for_timeout(800)
    assert get_cursors(api, weave)["uitest-clement"]["node_id"] == root1
    # right → first child
    press_and_wait_cursor(page, api, weave, "ArrowRight", kids[0], "right → first child")
    # down → next sibling
    press_and_wait_cursor(page, api, weave, "ArrowDown", kids[1], "down → sibling 1")
    press_and_wait_cursor(page, api, weave, "ArrowDown", kids[2], "down → sibling 2")
    # down at the last sibling: clamp, no wrap
    page.keyboard.press("ArrowDown")
    page.wait_for_timeout(800)
    assert get_cursors(api, weave)["uitest-clement"]["node_id"] == kids[2]
    # up → previous sibling
    press_and_wait_cursor(page, api, weave, "ArrowUp", kids[1], "up → sibling 1")


def test_sticky_child_navigation_returns_to_last_visited_child(page_as, weave, api):
    """Going to the parent and back toward the leaves returns to the child I
    LAST visited, not children[0]."""
    page = page_as("uitest-clement", weave)
    w = weave_json(api, weave)
    ids = seed_ids(w)
    root1, kids = ids["root1"], ids["kids"]
    assert get_cursors(api, weave)["uitest-clement"]["node_id"] == kids[0]

    # visit sibling 1, climb to the parent, descend again → back at sibling 1
    press_and_wait_cursor(page, api, weave, "ArrowDown", kids[1], "down → sibling 1")
    press_and_wait_cursor(page, api, weave, "ArrowLeft", root1, "left → parent")
    press_and_wait_cursor(
        page, api, weave, "ArrowRight", kids[1], "right returns to last-visited child"
    )
    # deeper memory: kids[1]'s parent entry updates as I browse, sibling 2 sticks too
    press_and_wait_cursor(page, api, weave, "ArrowDown", kids[2], "down → sibling 2")
    press_and_wait_cursor(page, api, weave, "ArrowLeft", root1, "left → parent again")
    press_and_wait_cursor(
        page, api, weave, "ArrowRight", kids[2], "stickiness follows the newest visit"
    )


def test_alt_arrow_navigates_even_from_the_text_pane(page_as, weave, api):
    """Alt+Arrow is a hardwired nav alias that works while focus is in the
    contenteditable doc; plain arrows stay caret movement there."""
    page = page_as("uitest-clement", weave)
    w = weave_json(api, weave)
    ids = seed_ids(w)
    root1, kids = ids["root1"], ids["kids"]
    assert get_cursors(api, weave)["uitest-clement"]["node_id"] == kids[0]

    page.locator(".doc").click()  # focus the editable thread document
    # plain ArrowLeft in the doc = caret move, NOT navigation
    page.keyboard.press("ArrowLeft")
    page.wait_for_timeout(800)
    assert get_cursors(api, weave)["uitest-clement"]["node_id"] == kids[0], (
        "plain arrows inside the doc must not move the weave cursor"
    )
    # Alt+ArrowLeft from inside the doc → parent
    press_and_wait_cursor(
        page, api, weave, "Alt+ArrowLeft", root1, "alt+left navigates from the doc"
    )
    # Alt+ArrowRight → back down (sticky: the child we came from)
    page.locator(".doc").click()
    press_and_wait_cursor(
        page, api, weave, "Alt+ArrowRight", kids[0], "alt+right navigates from the doc"
    )


def test_ctrl_enter_generates_and_b_toggles_bookmark_at_cursor(page_as, weave, api):
    page = page_as("uitest-clement", weave)
    w = weave_json(api, weave)
    cursor_node = get_cursors(api, weave)["uitest-clement"]["node_id"]
    children_before = list(w["nodes"][cursor_node]["children"])
    assert w["nodes"][cursor_node]["bookmarked"] is True  # seeded bookmark

    page.keyboard.press("Control+Enter")
    poll(
        lambda: len(weave_json(api, weave)["nodes"][cursor_node]["children"])
        > len(children_before),
        desc="Ctrl+Enter generated children at cursor",
    )
    page.wait_for_timeout(1500)
    w2 = weave_json(api, weave)
    new = [c for c in w2["nodes"][cursor_node]["children"] if c not in children_before]
    assert len(new) == 3  # default preset n=3
    # plain generate: no follow
    assert get_cursors(api, weave)["uitest-clement"]["node_id"] == cursor_node

    page.keyboard.press("b")
    poll(
        lambda: weave_json(api, weave)["nodes"][cursor_node]["bookmarked"] is False,
        timeout=4,
        desc="b removed the bookmark",
    )
    page.wait_for_timeout(CLIENT_SYNC * 1000)
    page.keyboard.press("b")
    poll(
        lambda: weave_json(api, weave)["nodes"][cursor_node]["bookmarked"] is True,
        timeout=4,
        desc="b re-added the bookmark",
    )


def test_delete_key_deletes_cursor_node_no_confirm(page_as, weave, api):
    """Delete asks NO confirmation (undo replaces it): the node goes straight
    away, the cursor relocates to the parent (server-side), and a toast with
    an "undo" button appears."""
    page = page_as("uitest-clement", weave)
    w = weave_json(api, weave)
    ids = seed_ids(w)
    branch = ids["kids"][1]
    victim = w["nodes"][branch]["children"][1]  # a leaf

    # park uitest-clement's cursor on the victim via the API, let the client sync
    api.put(
        f"/weaves/{weave}/cursors/uitest-clement",
        json={"node_id": victim, "moved_by": "uitest-clement"},
    ).raise_for_status()
    page.wait_for_timeout(1000)

    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
    page.keyboard.press("Delete")

    poll(
        lambda: victim not in weave_json(api, weave)["nodes"],
        timeout=4,
        desc="cursor node deleted",
    )
    assert dialogs == [], f"node delete must not open a native confirm: {dialogs}"
    # the undo affordance replaces the confirmation
    expect(page.get_by_test_id("toast-action")).to_have_text("undo")
    # cursor landed on the parent (server relocates stranded cursors)
    poll(
        lambda: get_cursors(api, weave)["uitest-clement"]["node_id"] == branch,
        timeout=4,
        desc="cursor moved to parent after delete",
    )


def test_c_collapses_subtree_at_cursor(page_as, weave, api):
    page = page_as("uitest-clement", weave)
    fit_weave(page)
    w = weave_json(api, weave)
    cursor_node = get_cursors(api, weave)["uitest-clement"]["node_id"]  # kids[0], 3 children
    children = w["nodes"][cursor_node]["children"]
    assert len(children) == 3
    grandchild = w["nodes"][children[0]]["children"][0]

    for c in children:
        expect(card(page, w["nodes"][c])).to_have_count(1)

    page.keyboard.press("c")
    for c in children:
        expect(card(page, w["nodes"][c])).to_have_count(0)
    expect(card(page, w["nodes"][grandchild])).to_have_count(0)  # whole subtree hidden
    expect(card(page, w["nodes"][cursor_node])).to_have_count(1)  # node itself stays

    page.keyboard.press("c")  # toggle back
    for c in children:
        expect(card(page, w["nodes"][c])).to_have_count(1)


def test_digit_toggles_generator_and_slash_focuses_search(page_as, weave, api):
    """Digits 1..9 TOGGLE the k-th visible generator chip (multi-active)."""
    import re as _re

    page = page_as("uitest-clement", weave)
    # fresh profile → chips = the seeded per-builtin-template generators
    gens = api.get("/generators?profile=uitest-clement").json()
    names = [g["name"] for g in gens]
    active = page.locator(".generators .chip.active")
    expect(active).to_have_count(0)  # nothing active initially (focused fallback)

    page.keyboard.press("2")
    expect(page.get_by_test_id(f"gc-gen-{names[1]}")).to_have_class(
        _re.compile(r"\bactive\b")
    )
    page.keyboard.press("1")
    expect(active).to_have_count(2)  # several active at once
    page.keyboard.press("2")  # toggles OFF again
    expect(active).to_have_count(1)
    expect(page.get_by_test_id(f"gc-gen-{names[0]}")).to_have_class(
        _re.compile(r"\bactive\b")
    )

    # '/' focuses the tree search input
    page.keyboard.press("/")
    assert page.evaluate(
        "() => document.activeElement?.hasAttribute('data-search-input') ?? false"
    ), "search input not focused after '/'"
    # digits typed in the (focused) search input must NOT toggle generators
    page.keyboard.press("3")
    page.wait_for_timeout(300)
    expect(active).to_have_count(1)
    expect(page.locator("[data-search-input]")).to_have_value("3")


def test_ctrl_9_and_ctrl_0_change_canvas_transform(page_as, weave, api):
    page = page_as("uitest-clement", weave)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    transform = page.locator(".canvas svg > g").first

    t0 = transform.get_attribute("transform")
    page.keyboard.press("Control+9")  # fit-cursor (zoom + center)
    poll(
        lambda: transform.get_attribute("transform") != t0,
        timeout=4,
        interval=0.1,
        desc="ctrl+9 changed the view transform",
    )
    t1 = transform.get_attribute("transform")
    page.keyboard.press("Control+0")  # fit-weave
    poll(
        lambda: transform.get_attribute("transform") != t1,
        timeout=4,
        interval=0.1,
        desc="ctrl+0 changed the view transform",
    )
    assert errors == [], f"page errors during view commands: {errors}"


def test_typing_in_doc_does_not_trigger_shortcuts(page_as, weave, api):
    """The free-form doc is contenteditable: typing g/b/2/Delete there EDITS
    TEXT (creating human nodes is fine) but must never fire the g-generate,
    b-bookmark, digit-generator-toggle or Delete-node shortcuts."""
    page = page_as("uitest-clement", weave)
    w = weave_json(api, weave)
    cursor_before = get_cursors(api, weave)["uitest-clement"]["node_id"]
    bookmarked_before = w["nodes"][cursor_before]["bookmarked"]
    gens_before = gen_event_count(api, weave)

    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))

    doc = page.locator(".doc")
    doc.click()
    page.keyboard.press("End")
    page.keyboard.type("gb2")
    page.keyboard.press("ArrowLeft")
    page.keyboard.press("ArrowUp")
    # generous window: a wrongly-triggered generation/bookmark would land
    # within this (the typed text itself stays local — edits are
    # local-until-boundary and no boundary fires here)
    page.wait_for_timeout(2500)

    assert dialogs == [], f"confirm dialog fired from doc keys: {dialogs}"
    w2 = weave_json(api, weave)
    assert gen_event_count(api, weave) == gens_before, "a generation was triggered"
    assert w2["nodes"][cursor_before]["bookmarked"] == bookmarked_before
    # NOTE: the edit itself may create nodes (hybrids, split tails — they
    # legitimately inherit model creators/metadata); the gen_event_count
    # assertion above is the authoritative "no generation fired" check.
    # the '2' went into the text, not into the generator chips
    expect(page.locator(".generators .chip.active")).to_have_count(0)
