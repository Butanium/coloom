"""Adversarial UI tests for the sidebar list panes: TreeList + FlatList.

Surface: web/src/lib/TreeList.svelte, web/src/lib/FlatList.svelte, sidebar tabs in
Editor.svelte. Spec: docs/ui-specs/lists.md (with the coloom deltas in §10).

Every mutation triggered through the DOM is verified through the REST API, and
API-side mutations are verified to reach the DOM via the WS-driven refetch.

Seeded weave topology (scripts/seed_dev_weave.py):
  root1 "The loom hummed…"  -> kids[0] (bookmarked, uitest-clement cursor), kids[1], kids[2]
    kids[0] -> 3 grandkids; grandkids[0] -> 2 deep (uitest-claude cursor on one)
    kids[2] was split (head 3 tokens -> tail) -> human interjection node
  root2 "Chapter 2…" -> 2 children
"""

import math
import re
import time

import pytest

# WS round-trip + debounced refetch budget for DOM convergence after a mutation.
DOM_DEADLINE = 6.0


# ---------------------------------------------------------------- helpers


def wait_until(fn, timeout=DOM_DEADLINE, interval=0.15, msg="condition"):
    """Poll `fn` until truthy; return its value. Generous polling, no retries."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {msg} (last={last!r})")


def node_text(node: dict) -> str:
    if node["content"]["type"] == "snippet":
        return node["content"]["text"]
    return "".join(t["text"] for t in node["content"]["tokens"])


def get_weave(api, wid: str) -> dict:
    r = api.get(f"/weaves/{wid}")
    r.raise_for_status()
    return r.json()


def get_cursors(api, wid: str) -> dict:
    r = api.get(f"/weaves/{wid}/cursors")
    r.raise_for_status()
    return r.json()


def topology(api, wid: str):
    """Resolve the seeded weave's structural node ids."""
    w = get_weave(api, wid)
    root1 = next(r for r in w["roots"] if "loom hummed" in node_text(w["nodes"][r]))
    root2 = next(r for r in w["roots"] if r != root1)
    kids = w["nodes"][root1]["children"]
    assert len(kids) == 3, "seed shape changed: expected 3 children under root1"
    grandkids = w["nodes"][kids[0]]["children"]
    assert len(grandkids) == 3
    return w, root1, root2, kids, grandkids


def sidebar(page):
    return page.locator(".sidebar")


def row(page, node_id: str):
    """A list row for a node, scoped to the sidebar (TextPane also emits
    data-node-id spans, so never query unscoped)."""
    return sidebar(page).locator(f'[data-node-id="{node_id}"]')


def row_ids(page) -> list[str]:
    rows = sidebar(page).locator("[data-node-id]")
    return [rows.nth(i).get_attribute("data-node-id") for i in range(rows.count())]


def padding_left(page, node_id: str) -> int:
    style = row(page, node_id).get_attribute("style") or ""
    m = re.search(r"padding-left:\s*(\d+)px", style)
    assert m, f"row {node_id[:8]} has no padding-left in style: {style!r}"
    return int(m.group(1))


def select_tab(page, label: str):
    page.locator(".sidebar nav.tabs button", has_text=label).first.click()
    page.wait_for_timeout(150)


def park_mouse(page):
    """Move the pointer off every row (and off the sidebar) so hover strips
    unmount and passive info is visible."""
    page.mouse.move(750, 12)  # header strip, well outside the sidebar
    page.wait_for_timeout(150)


def add_single_token_node(api, wid: str, parent_id: str, p: float = 0.9) -> dict:
    """A 1-token Tokens node with a known probability (passive-% fixture)."""
    r = api.post(
        f"/weaves/{wid}/nodes",
        json={
            "content": {
                "type": "tokens",
                "tokens": [{"text": " hi", "logprob": math.log(p), "top_logprobs": []}],
            },
            "parent_id": parent_id,
            "creator": {"type": "human", "label": "uitest-clement"},
        },
    )
    r.raise_for_status()
    return r.json()


# ================================================================ tree tab


def test_tree_rows_indentation_and_default_window(page_as, api, weave):
    """Cursor on kids[0] (parent is a root => no grandparent): the window falls
    back to ALL roots; every node renders, indented by depth (4 + 14*depth)."""
    w, root1, root2, kids, grandkids = topology(api, weave)
    page = page_as("uitest-clement", weave)
    select_tab(page, "tree")

    def all_rows():
        ids = row_ids(page)
        return ids if len(ids) == len(w["nodes"]) else None

    ids = wait_until(all_rows, msg="all seeded rows rendered")
    assert set(ids) == set(w["nodes"]), "tree should render every node of the window"

    # depth encoding: roots at 4px, their children at 18px, grandchildren at 32px
    assert padding_left(page, root1) == 4
    assert padding_left(page, root2) == 4
    for k in kids:
        assert padding_left(page, k) == 4 + 14
    for g in grandkids:
        assert padding_left(page, g) == 4 + 28

    # depth-first order: kids[0]'s subtree rows come between kids[0] and kids[1]
    assert ids.index(root1) < ids.index(kids[0]) < ids.index(grandkids[0]) < ids.index(kids[1])

    # all-roots window => no "show parents" pseudo-row
    assert sidebar(page).locator("button.pseudo", has_text="show parents").count() == 0


def test_windowing_recenters_and_show_parents_reroots_without_cursor_move(
    page_as, api, weave
):
    """Deep cursor => window re-roots at the cursor's parent; 'show parents'
    re-roots the VIEW up one level and must NOT move my cursor (coloom delta)."""
    _, root1, root2, kids, grandkids = topology(api, weave)
    page = page_as("uitest-clement", weave)
    select_tab(page, "tree")

    # move my cursor (server-side) onto grandkids[0] (which has children):
    # window should become [kids[0]] — root rows leave the view via WS refetch
    api.put(
        f"/weaves/{weave}/cursors/uitest-clement",
        json={"node_id": grandkids[0], "moved_by": "uitest-clement"},
    ).raise_for_status()
    wait_until(lambda: row(page, root1).count() == 0, msg="root1 leaves the window")
    assert row(page, root2).count() == 0
    assert row(page, kids[0]).count() == 1
    assert padding_left(page, kids[0]) == 4, "display root renders at depth 0"
    assert row(page, kids[1]).count() == 0, "siblings of the display root are out of window"

    show_parents = sidebar(page).locator("button.pseudo", has_text="show parents")
    assert show_parents.count() == 1, "'show parents' row appears on a windowed view"
    show_parents.click()
    wait_until(lambda: row(page, root1).count() == 1, msg="view re-rooted up to root1")
    # the gesture is view-only: my cursor must not have moved
    cur = get_cursors(api, weave)["uitest-clement"]
    assert cur["node_id"] == grandkids[0], "'show parents' must not move my cursor"
    # re-rooted at root1 => root1 has no parent => the pseudo-row disappears
    assert sidebar(page).locator("button.pseudo", has_text="show parents").count() == 0


def test_click_row_moves_my_cursor_and_dom_reflects_it(page_as, api, weave):
    _, root1, root2, kids, _ = topology(api, weave)
    page = page_as("uitest-clement", weave)
    select_tab(page, "tree")

    target = row(page, kids[1])
    # click the label, not dead center (center may sit over the hover strip)
    target.locator(".label").click()
    def cursor_on_target():
        c = get_cursors(api, weave)["uitest-clement"]
        return c if c["node_id"] == kids[1] else None

    cur = wait_until(cursor_on_target, msg="uitest-clement cursor moved to clicked row")
    assert cur["moved_by"] == "uitest-clement"
    # DOM follows: the row gains the is-cursor stroke class
    wait_until(
        lambda: "is-cursor" in (row(page, kids[1]).get_attribute("class") or ""),
        msg="clicked row shows the cursor stroke",
    )
    assert "is-cursor" not in (row(page, kids[0]).get_attribute("class") or "")


def test_right_click_opens_context_menu_without_moving_cursor(page_as, api, weave):
    _, _, _, kids, _ = topology(api, weave)
    page = page_as("uitest-clement", weave)
    select_tab(page, "tree")
    before = get_cursors(api, weave)["uitest-clement"]["node_id"]

    row(page, kids[1]).click(button="right")
    menu = page.locator('.menu[role="menu"]')
    wait_until(lambda: menu.count() == 1 and menu.is_visible(), msg="context menu opens")
    items = menu.locator('button[role="menuitem"]')
    labels = " | ".join(items.nth(i).inner_text() for i in range(items.count()))
    for expected in ("generate", "bookmark", "delete"):
        assert expected in labels, f"context menu misses a {expected!r} item: {labels}"

    page.keyboard.press("Escape")
    wait_until(lambda: page.locator('.menu[role="menu"]').count() == 0, msg="menu closes")
    assert get_cursors(api, weave)["uitest-clement"]["node_id"] == before, (
        "right-click must not move my cursor"
    )


def test_collapse_triangle_toggles_subtree_and_does_not_move_cursor(
    page_as, api, weave
):
    w, _, _, kids, _ = topology(api, weave)
    page = page_as("uitest-clement", weave)
    select_tab(page, "tree")
    subtree_kids = w["nodes"][kids[1]]["children"]
    assert len(subtree_kids) == 2
    before_cursor = get_cursors(api, weave)["uitest-clement"]["node_id"]

    for child in subtree_kids:
        assert row(page, child).count() == 1, "subtree starts expanded"

    tri = row(page, kids[1]).locator("button.tri")
    assert tri.count() == 1, "rows with children carry a collapse triangle"
    tri.click()
    wait_until(
        lambda: all(row(page, c).count() == 0 for c in subtree_kids),
        msg="collapse hides subtree rows",
    )
    assert row(page, kids[1]).count() == 1, "the collapsed row itself stays"
    # triangle click is collapse-only — not a row activation
    assert get_cursors(api, weave)["uitest-clement"]["node_id"] == before_cursor

    row(page, kids[1]).locator("button.tri").click()
    wait_until(
        lambda: all(row(page, c).count() == 1 for c in subtree_kids),
        msg="expand restores subtree rows",
    )


# ----------------------------------------------------------- hover strip


def test_strip_generate_plain_click_adds_children_without_cursor_move(
    page_as, api, weave
):
    w, _, _, kids, _ = topology(api, weave)
    page = page_as("uitest-clement", weave)
    select_tab(page, "tree")
    leaf = w["nodes"][kids[1]]["children"][0]  # a depth-2 leaf
    before_cursor = get_cursors(api, weave)["uitest-clement"]["node_id"]

    r = row(page, leaf)
    # passive info only until hovered; the strip must appear on hover
    assert r.locator(".strip").count() == 0
    r.hover()
    gen_btn = r.locator('button[title^="generate completions"]')
    wait_until(lambda: gen_btn.count() == 1, timeout=2, msg="hover strip appears")
    gen_btn.click()

    children = wait_until(
        lambda: get_weave(api, weave)["nodes"][leaf]["children"] or None,
        msg="generation produced children (API)",
    )
    assert len(children) >= 1
    # plain click never moves my cursor
    assert get_cursors(api, weave)["uitest-clement"]["node_id"] == before_cursor
    # and the new rows show up in the tree via the WS refetch
    wait_until(
        lambda: all(row(page, c).count() == 1 for c in children),
        msg="generated children rendered as rows",
    )


def test_strip_generate_ctrl_click_also_moves_my_cursor(page_as, api, weave):
    _, _, _, kids, grandkids = topology(api, weave)
    page = page_as("uitest-clement", weave)
    select_tab(page, "tree")
    leaf = grandkids[1]  # bookmarked leaf under kids[0]

    r = row(page, leaf)
    r.hover()
    r.locator('button[title^="generate completions"]').click(modifiers=["Control"])
    children = wait_until(
        lambda: get_weave(api, weave)["nodes"][leaf]["children"] or None,
        msg="ctrl-click generation produced children (API)",
    )
    def cursor_in_children():
        c = get_cursors(api, weave)["uitest-clement"]
        return c if c["node_id"] in children else None

    cur = wait_until(cursor_in_children, msg="cursor followed the generation")
    assert cur["node_id"] == children[0], "cursor moves to the FIRST generated node"


def test_strip_generate_middle_click_also_moves_my_cursor(page_as, api, weave):
    _, _, _, kids, grandkids = topology(api, weave)
    page = page_as("uitest-clement", weave)
    select_tab(page, "tree")
    leaf = grandkids[2]

    r = row(page, leaf)
    r.hover()
    r.locator('button[title^="generate completions"]').click(button="middle")
    children = wait_until(
        lambda: get_weave(api, weave)["nodes"][leaf]["children"] or None,
        msg="middle-click generation produced children (API)",
    )
    assert get_cursors(api, weave)["uitest-clement"]["node_id"] == children[0]


def test_strip_add_child_creates_empty_human_node(page_as, api, weave):
    _, _, root2, _, _ = topology(api, weave)
    page = page_as("uitest-clement", weave)
    select_tab(page, "tree")
    before = set(get_weave(api, weave)["nodes"][root2]["children"])
    before_cursor = get_cursors(api, weave)["uitest-clement"]["node_id"]

    r = row(page, root2)
    r.hover()
    r.locator('button[title^="add empty child"]').click()
    new = wait_until(
        lambda: [
            c
            for c in get_weave(api, weave)["nodes"][root2]["children"]
            if c not in before
        ]
        or None,
        msg="add-child created a node (API)",
    )
    assert len(new) == 1
    node = get_weave(api, weave)["nodes"][new[0]]
    assert node["creator"]["type"] == "human"
    assert node["creator"]["label"] == "uitest-clement", "child is attributed to MY identity"
    assert node_text(node) == ""
    # plain add-child does not move my cursor
    assert get_cursors(api, weave)["uitest-clement"]["node_id"] == before_cursor
    wait_until(lambda: row(page, new[0]).count() == 1, msg="new child row rendered")


def test_strip_bookmark_toggle_roundtrip_with_passive_star(page_as, api, weave):
    _, _, _, kids, _ = topology(api, weave)
    page = page_as("uitest-clement", weave)
    select_tab(page, "tree")
    target = kids[1]
    assert not get_weave(api, weave)["nodes"][target]["bookmarked"]

    r = row(page, target)
    r.hover()
    r.locator('button[title="bookmark node"]').click()
    wait_until(
        lambda: get_weave(api, weave)["nodes"][target]["bookmarked"],
        msg="bookmark set (API)",
    )
    park_mouse(page)
    wait_until(
        lambda: row(page, target).locator(".bm").count() == 1,
        msg="passive bookmark star appears on the un-hovered row",
    )

    r.hover()
    unbtn = r.locator('button[title="remove bookmark"]')
    wait_until(lambda: unbtn.count() == 1, timeout=3, msg="strip reflects bookmarked state")
    unbtn.click()
    wait_until(
        lambda: not get_weave(api, weave)["nodes"][target]["bookmarked"],
        msg="bookmark removed (API)",
    )
    park_mouse(page)
    wait_until(
        lambda: row(page, target).locator(".bm").count() == 0,
        msg="passive star gone again",
    )


def test_strip_delete_removes_subtree_and_row(page_as, api, weave):
    w, _, root2, _, _ = topology(api, weave)
    page = page_as("uitest-clement", weave)
    select_tab(page, "tree")
    victim = w["nodes"][root2]["children"][0]

    r = row(page, victim)
    r.hover()
    r.locator('button[title^="delete node"]').click()
    wait_until(
        lambda: victim not in get_weave(api, weave)["nodes"],
        msg="node deleted (API)",
    )
    wait_until(lambda: row(page, victim).count() == 0, msg="row unmounted")


def test_delete_my_cursor_node_relocates_cursor_to_parent(page_as, api, weave):
    """Deleting the node my cursor sits on must fall the cursor back to the
    deleted node's parent (server-side rule, spec lists.md §10)."""
    _, root1, _, kids, _ = topology(api, weave)
    page = page_as("uitest-clement", weave)
    select_tab(page, "tree")
    assert get_cursors(api, weave)["uitest-clement"]["node_id"] == kids[0]

    r = row(page, kids[0])
    r.hover()
    r.locator('button[title^="delete node"]').click()
    wait_until(
        lambda: kids[0] not in get_weave(api, weave)["nodes"],
        msg="cursor node deleted (API)",
    )
    cur = wait_until(
        lambda: get_cursors(api, weave).get("uitest-clement"),
        msg="uitest-clement cursor survives the delete",
    )
    assert cur["node_id"] == root1, "cursor falls back to the deleted node's parent"
    wait_until(
        lambda: "is-cursor" in (row(page, root1).get_attribute("class") or ""),
        msg="root row now carries the cursor stroke",
    )


# ------------------------------------------------------------ passive info


def test_passive_single_token_probability_and_bookmark_star(page_as, api, weave):
    _, _, root2, kids, _ = topology(api, weave)
    single = add_single_token_node(api, weave, root2, p=0.9)
    page = page_as("uitest-clement", weave)
    select_tab(page, "tree")
    park_mouse(page)

    srow = wait_until(
        lambda: row(page, single["id"]) if row(page, single["id"]).count() else None,
        msg="single-token row rendered",
    )
    assert srow.locator(".prob").text_content() == "90.0%", (
        "1-token node shows exp(logprob) as a percent on the un-hovered row"
    )
    # multi-token / snippet rows must NOT show a probability
    assert row(page, kids[0]).locator(".prob").count() == 0
    assert row(page, root2).locator(".prob").count() == 0
    # seeded bookmark on kids[0] shows the passive star
    assert row(page, kids[0]).locator(".bm").count() == 1
    # hovering swaps passive info for the strip
    srow.hover()
    wait_until(lambda: srow.locator(".strip").count() == 1, timeout=2, msg="strip on hover")
    assert srow.locator(".prob").count() == 0, "passive info hides while hovered"


def test_cursor_dots_mark_participant_rows(page_as, api, weave):
    _, _, _, kids, _ = topology(api, weave)
    page = page_as("uitest-clement", weave)
    select_tab(page, "tree")
    claude_node = get_cursors(api, weave)["uitest-claude"]["node_id"]

    assert row(page, kids[0]).locator('.cdot[title="uitest-clement"]').count() == 1
    assert row(page, claude_node).locator('.cdot[title="uitest-claude"]').count() == 1
    # no stray dots elsewhere
    assert sidebar(page).locator(".cdot").count() == 2


# ----------------------------------------------------------------- search


def test_search_filters_click_moves_cursor_and_clear_restores(page_as, api, weave):
    w, root1, root2, kids, _ = topology(api, weave)
    page = page_as("uitest-clement", weave)
    select_tab(page, "tree")

    page.fill("[data-search-input]", "Chapter 2")
    wait_until(
        lambda: row_ids(page) == [root2],
        msg="search shows only the matching row",
    )
    match = row(page, root2)
    assert match.locator("button.tri").count() == 0, "search rows are flat (no triangle)"

    match.locator(".label").click()
    wait_until(
        lambda: get_cursors(api, weave)["uitest-clement"]["node_id"] == root2,
        msg="clicking a match moves my cursor (API)",
    )

    # no-match query shows the empty message, not a stale tree
    page.fill("[data-search-input]", "zzz-no-such-text-zzz")
    wait_until(
        lambda: sidebar(page).locator(".empty", has_text="no nodes match").count() == 1,
        msg="no-match message",
    )
    assert row_ids(page) == []

    # clearing restores the windowed tree (cursor on root2 => all-roots window)
    page.locator(".search button.clear").click()
    wait_until(
        lambda: len(row_ids(page)) == len(get_weave(api, weave)["nodes"]),
        msg="clearing the search restores the tree",
    )
    assert row(page, root1).count() == 1


# ----------------------------------------------------------- children tab


def test_children_tab_shows_parent_row_and_children_of_my_cursor(page_as, api, weave):
    _, root1, _, kids, grandkids = topology(api, weave)
    page = page_as("uitest-clement", weave)
    select_tab(page, "children")

    heading = sidebar(page).locator(".heading")
    wait_until(lambda: heading.count() == 1, msg="flat list heading")
    assert "children of my cursor" in heading.text_content().lower()

    # parent row at the top, then the cursor's children in weave order
    def parent_plus_children():
        ids = row_ids(page)
        return ids if len(ids) == 1 + len(grandkids) else None

    ids = wait_until(parent_plus_children, msg="parent row + children rows")
    assert ids[0] == root1, "first row is my cursor's parent (the way back up)"
    assert ids[1:] == grandkids
    assert "parent-row" in (row(page, root1).get_attribute("class") or "")


def test_children_tab_click_child_descends_and_parent_row_ascends(page_as, api, weave):
    _, root1, _, kids, grandkids = topology(api, weave)
    page = page_as("uitest-clement", weave)
    select_tab(page, "children")
    leaf = grandkids[1]  # childless

    row(page, leaf).locator(".label").click()
    wait_until(
        lambda: get_cursors(api, weave)["uitest-clement"]["node_id"] == leaf,
        msg="clicking a child moves my cursor down (API)",
    )
    # list re-derives: parent row is now kids[0], and the leaf has no children
    wait_until(
        lambda: row_ids(page) == [kids[0]],
        msg="flat list shows only the new parent row for a childless cursor",
    )
    assert sidebar(page).locator(".empty", has_text="no children").count() == 1

    row(page, kids[0]).locator(".label").click()
    wait_until(
        lambda: get_cursors(api, weave)["uitest-clement"]["node_id"] == kids[0],
        msg="clicking the parent row moves my cursor up (API)",
    )
    wait_until(
        lambda: row_ids(page) == [root1, *grandkids],
        msg="flat list re-rooted at kids[0] again",
    )


def test_children_tab_strip_actions_work_too(page_as, api, weave):
    """The flat list rows carry the same action strip — exercise add-child."""
    _, root1, _, kids, grandkids = topology(api, weave)
    page = page_as("uitest-clement", weave)
    select_tab(page, "children")
    target = grandkids[0]
    before = set(get_weave(api, weave)["nodes"][target]["children"])

    r = row(page, target)
    r.hover()
    addbtn = r.locator('button[title^="add empty child"]')
    wait_until(lambda: addbtn.count() == 1, timeout=2, msg="flat-list strip appears")
    # ctrl-click: add AND move my cursor to the new node
    addbtn.click(modifiers=["Control"])
    new = wait_until(
        lambda: [
            c
            for c in get_weave(api, weave)["nodes"][target]["children"]
            if c not in before
        ]
        or None,
        msg="flat-list add-child created a node (API)",
    )
    wait_until(
        lambda: get_cursors(api, weave)["uitest-clement"]["node_id"] == new[0],
        msg="ctrl-click add-child moved my cursor to the new node",
    )


# ------------------------------------------------------------- hover sync


def test_hover_tree_row_highlights_matching_canvas_card(page_as, api, weave):
    _, root1, root2, _, _ = topology(api, weave)
    page = page_as("uitest-clement", weave)
    select_tab(page, "tree")
    # make sure the canvas shows every card (hover sync needs the card mounted)
    page.locator('button[title^="fit whole weave"]').click()
    page.wait_for_timeout(400)

    park_mouse(page)
    assert page.locator("g.card.hovered").count() == 0, "no hovered card at rest"

    row(page, root2).hover()
    card = wait_until(
        lambda: page.locator("g.card.hovered")
        if page.locator("g.card.hovered").count() == 1
        else None,
        timeout=3,
        msg="exactly one canvas card mirrors the tree-row hover",
    )
    assert "Chapter 2" in (card.text_content() or ""), (
        "the hovered canvas card is the SAME node as the hovered tree row"
    )

    # hover a different row: the highlight must follow, not stick
    row(page, root1).hover()
    wait_until(
        lambda: "loom hummed" in (page.locator("g.card.hovered").first.text_content() or "")
        if page.locator("g.card.hovered").count() == 1
        else False,
        timeout=3,
        msg="hover highlight follows to the other row's card",
    )

    park_mouse(page)
    wait_until(
        lambda: page.locator("g.card.hovered").count() == 0,
        timeout=3,
        msg="leaving the row clears the shared hover",
    )
