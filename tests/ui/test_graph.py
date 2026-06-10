"""Adversarial UI tests for the graph minimap center tab (GraphMinimap.svelte).

Covers (spec: docs/ui-specs/shell-menus-graph.md §3 + component intent):
- graph shows EVERY node, ignoring the collapse set
- click square -> my cursor moves (verified via REST); ctrl/middle-click -> no move
- right-click -> shared context menu opens (and no cursor move)
- bookmark ribbons + per-cursor outlines are DOM-distinguishable
- hover highlights the square AND the tree-list row (shared hover state), tooltip shows
- drag pans, ctrl+wheel zooms, plain wheel pans; drag starting on a square is NOT a click
- header "fit weave" button brings all squares back into the viewport
- API-side generation -> new squares appear live (no reload)

Squares carry no node-id in the DOM, so square->node mapping goes through the hover
tooltip excerpt (exact text match against the API weave). That is deliberate: it also
exercises the tooltip path on every mapped square.
"""

import re
import time

GRAPH = ".graph"
SQUARES = ".graph svg g.node"
RECTS = ".graph svg g.node > rect"
RIBBONS = ".graph svg g.node polygon.ribbon"
TIP_TEXT = ".graph .tooltip .tip-text"
VIEW_G = ".graph svg > g"


# ---------------------------------------------------------------- helpers


def node_text(node: dict) -> str:
    content = node["content"]
    if content["type"] == "tokens":
        return "".join(t["text"] for t in content["tokens"])
    return content["text"]


def tip_excerpt(text: str) -> str:
    return text if len(text) <= 180 else text[:180] + "…"


def get_weave(api, weave_id: str) -> dict:
    resp = api.get(f"/weaves/{weave_id}")
    resp.raise_for_status()
    return resp.json()


def get_cursors(api, weave_id: str) -> dict:
    resp = api.get(f"/weaves/{weave_id}/cursors")
    resp.raise_for_status()
    return resp.json()


def goto_graph(page):
    """Switch the center tab to 'graph' and wait for squares to render."""
    page.get_by_role("button", name="graph", exact=True).click()
    page.wait_for_selector(SQUARES, state="attached", timeout=5000)
    page.wait_for_timeout(250)  # initial fit-weave effect + layout settle


def view_transform(page) -> tuple[float, float, float]:
    """(tx, ty, scale) of the minimap view transform."""
    t = page.locator(VIEW_G).get_attribute("transform")
    m = re.match(r"translate\(([-\d.eE+]+),\s*([-\d.eE+]+)\)\s*scale\(([-\d.eE+]+)\)", t or "")
    assert m, f"unparseable view transform: {t!r}"
    tx, ty, scale = (float(x) for x in m.groups())
    return tx, ty, scale


def square_node_id(page, nodes: dict, i: int) -> str | None:
    """Identify square i by hovering it and matching the tooltip excerpt.

    Returns the node id when the excerpt matches exactly one node, else None.
    Squares outside the visible viewport (after a pan) are skipped.
    """
    from playwright.sync_api import TimeoutError as PWTimeout

    try:
        page.locator(SQUARES).nth(i).hover(timeout=2000)
    except PWTimeout:
        return None  # off-viewport square (SVG can't scroll) — skip it
    page.wait_for_timeout(120)
    tip = page.locator(TIP_TEXT)
    if tip.count() == 0:
        return None
    txt = tip.text_content()
    matches = [nid for nid, n in nodes.items() if tip_excerpt(node_text(n)) == txt]
    return matches[0] if len(matches) == 1 else None


def find_identifiable_square(page, nodes: dict, exclude: set[str] = frozenset()):
    """First square whose node id is unambiguous (and not in `exclude`)."""
    count = page.locator(SQUARES).count()
    for i in range(count):
        nid = square_node_id(page, nodes, i)
        if nid is not None and nid not in exclude:
            return i, nid
    raise AssertionError("no square could be identified via its hover tooltip")


def park_mouse(page):
    """Move the pointer out of the graph pane (header area) and let hover clear."""
    page.mouse.move(5, 5)
    page.wait_for_timeout(150)


def wait_until(fn, timeout: float = 6.0, interval: float = 0.2, msg: str = "condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for: {msg}")


def squares_inside_container(page) -> bool:
    return page.evaluate(
        """() => {
            const c = document.querySelector('.graph').getBoundingClientRect()
            const rects = [...document.querySelectorAll('.graph svg g.node > rect')]
            if (rects.length === 0) return false
            return rects.every((el) => {
                const r = el.getBoundingClientRect()
                return r.left >= c.left - 1 && r.right <= c.right + 1
                    && r.top >= c.top - 1 && r.bottom <= c.bottom + 1
            })
        }"""
    )


# ---------------------------------------------------------------- tests


def test_graph_shows_all_nodes_ignoring_collapse(page_as, api, weave):
    """Collapse a subtree in the tree list, then switch to graph: every node
    must still be a square (the minimap deliberately ignores the collapse set)."""
    page = page_as("uitest-clement", weave)
    w = get_weave(api, weave)
    n_nodes = len(w["nodes"])
    assert n_nodes >= 10, f"seed produced suspiciously few nodes: {n_nodes}"

    # collapse the (visible) root row in the tree sidebar
    root_id = next(nid for nid, n in w["nodes"].items() if not n["parents"])
    rows_before = page.locator(".sidebar .row").count()
    root_row = page.locator(f'.sidebar .row[data-node-id="{root_id}"]')
    assert root_row.count() == 1, "root row not visible in tree list"
    root_row.locator("button.tri").click()
    page.wait_for_timeout(200)
    rows_after = page.locator(".sidebar .row").count()
    assert rows_after < rows_before, "collapse had no effect on the tree list"

    goto_graph(page)
    assert page.locator(SQUARES).count() == n_nodes, (
        "graph square count != API node count (collapse must be ignored)"
    )
    # every square has exactly one unit rect
    assert page.locator(RECTS).count() == n_nodes


def test_click_square_moves_my_cursor(page_as, api, weave):
    page = page_as("uitest-clement", weave)
    goto_graph(page)
    w = get_weave(api, weave)
    my_before = get_cursors(api, weave)["uitest-clement"]["node_id"]

    i, target = find_identifiable_square(page, w["nodes"], exclude={my_before})
    page.locator(SQUARES).nth(i).click()

    wait_until(
        lambda: get_cursors(api, weave)["uitest-clement"]["node_id"] == target,
        msg=f"uitest-clement cursor -> {target} after square click",
    )
    cur = get_cursors(api, weave)["uitest-clement"]
    assert cur["moved_by"] == "uitest-clement"

    # the clicked square should now carry my cursor outline (after WS refetch)
    page.wait_for_timeout(600)
    park_mouse(page)
    stroke = page.locator(RECTS).nth(i).get_attribute("stroke")
    assert stroke not in (None, "none"), "clicked square lost/never got the cursor outline"


def test_modified_clicks_do_not_move_cursor(page_as, api, weave):
    """ctrl+click and middle-click are 'teleport view' gestures: they must
    recenter the view but leave my cursor untouched."""
    page = page_as("uitest-clement", weave)
    goto_graph(page)
    w = get_weave(api, weave)
    before = get_cursors(api, weave)["uitest-clement"]

    # pan away first so a recenter visibly changes the transform
    box = page.locator(GRAPH).bounding_box()
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(cx, cy)
    page.mouse.down()
    page.mouse.move(cx + 150, cy + 110, steps=5)
    page.mouse.up()
    t_panned = view_transform(page)

    # map a still-visible square AFTER the pan (off-viewport ones are skipped)
    i, target = find_identifiable_square(page, w["nodes"], exclude={before["node_id"]})
    park_mouse(page)

    sq = page.locator(SQUARES).nth(i)
    sq.click(modifiers=["Control"])
    page.wait_for_timeout(700)
    after_ctrl = get_cursors(api, weave)["uitest-clement"]
    assert after_ctrl["node_id"] == before["node_id"], "ctrl+click moved the cursor"
    assert after_ctrl["updated"] == before["updated"], "ctrl+click touched the cursor"
    t_centered = view_transform(page)
    assert (t_centered[0], t_centered[1]) != (t_panned[0], t_panned[1]), (
        "ctrl+click did not recenter the view (center-node command lost)"
    )

    sq.click(button="middle")
    page.wait_for_timeout(700)
    after_mid = get_cursors(api, weave)["uitest-clement"]
    assert after_mid["node_id"] == before["node_id"], "middle-click moved the cursor"
    assert after_mid["updated"] == before["updated"], "middle-click touched the cursor"


def test_right_click_opens_context_menu(page_as, api, weave):
    page = page_as("uitest-clement", weave)
    goto_graph(page)
    before = get_cursors(api, weave)["uitest-clement"]

    page.locator(SQUARES).nth(0).click(button="right")
    menu = page.locator("div.menu")
    menu.wait_for(state="visible", timeout=3000)
    # it's the shared node context menu — spot-check a couple of node actions
    assert menu.get_by_role("menuitem", name="generate here").count() == 1
    assert menu.get_by_role("menuitem", name=re.compile("bookmark")).count() >= 1
    # tooltip is suppressed while the menu is open
    assert page.locator(".graph .tooltip").count() == 0

    page.keyboard.press("Escape")
    menu.wait_for(state="hidden", timeout=3000)

    page.wait_for_timeout(400)
    after = get_cursors(api, weave)["uitest-clement"]
    assert after["node_id"] == before["node_id"], "right-click moved the cursor"
    assert after["updated"] == before["updated"]


def test_bookmark_ribbons_and_cursor_outlines(page_as, api, weave):
    page = page_as("uitest-clement", weave)
    goto_graph(page)
    park_mouse(page)
    w = get_weave(api, weave)

    n_bookmarked = sum(1 for n in w["nodes"].values() if n["bookmarked"])
    assert n_bookmarked >= 2, "seed should have >= 2 bookmarks"
    assert page.locator(RIBBONS).count() == n_bookmarked, (
        "bookmark ribbon count != bookmarked node count"
    )

    cursor_nodes = {c["node_id"] for c in w["cursors"].values()}
    assert len(cursor_nodes) >= 2, "seed cursors should sit on distinct nodes"
    outlined = page.evaluate(
        """() => [...document.querySelectorAll('.graph svg g.node > rect')]
                .filter((r) => r.getAttribute('stroke') !== 'none')
                .map((r) => r.getAttribute('stroke'))"""
    )
    assert len(outlined) == len(cursor_nodes), (
        f"outlined squares {len(outlined)} != distinct cursor nodes {len(cursor_nodes)}"
    )
    # outlines are per-participant colors -> distinct strokes for distinct cursors
    assert len(set(outlined)) == len(cursor_nodes), f"cursor outline colors collide: {outlined}"


def test_hover_highlights_square_tooltip_and_tree_row(page_as, api, weave):
    page = page_as("uitest-clement", weave)
    goto_graph(page)
    w = get_weave(api, weave)

    # tree list windows around my cursor (root subtree) — pick a square that maps
    # to a node whose row is visible in the sidebar
    visible_rows = set(
        page.eval_on_selector_all(
            ".sidebar .row[data-node-id]", "els => els.map(e => e.dataset.nodeId)"
        )
    )
    assert visible_rows, "tree list shows no rows"
    count = page.locator(SQUARES).count()
    target_i, target_id = None, None
    for i in range(count):
        nid = square_node_id(page, w["nodes"], i)
        if nid in visible_rows:
            target_i, target_id = i, nid
            break
    assert target_id is not None, "no graph square maps to a visible tree row"

    sq_rect = page.locator(RECTS).nth(target_i)
    page.locator(SQUARES).nth(target_i).hover()
    page.wait_for_timeout(150)

    # square: hovered class + the hover stroke
    assert "hovered" in (sq_rect.get_attribute("class") or "")
    assert sq_rect.get_attribute("stroke") == "var(--text)"
    # tooltip visible with a creator label in the header
    tip_head = page.locator(".graph .tooltip .tip-head")
    assert tip_head.count() == 1 and tip_head.text_content().strip() != ""
    # shared hover: the tree-list row for the same node highlights
    row = page.locator(f'.sidebar .row[data-node-id="{target_id}"]')
    assert "hovered" in (row.get_attribute("class") or ""), (
        "shared hover did not propagate to the tree list row"
    )

    # leave: highlight + tooltip + shared hover all clear
    park_mouse(page)
    assert "hovered" not in (sq_rect.get_attribute("class") or "")
    assert page.locator(".graph .tooltip").count() == 0
    assert "hovered" not in (row.get_attribute("class") or "")


def test_drag_pans_and_is_not_a_click(page_as, api, weave):
    page = page_as("uitest-clement", weave)
    goto_graph(page)
    before_cursor = get_cursors(api, weave)["uitest-clement"]

    # drag starting ON a square: must pan, must NOT move the cursor
    sq_box = page.locator(SQUARES).nth(0).bounding_box()
    assert sq_box is not None
    sx, sy = sq_box["x"] + sq_box["width"] / 2, sq_box["y"] + sq_box["height"] / 2
    t0 = view_transform(page)
    page.mouse.move(sx, sy)
    page.mouse.down()
    page.mouse.move(sx + 120, sy + 80, steps=8)
    page.mouse.up()
    t1 = view_transform(page)
    assert abs((t1[0] - t0[0]) - 120) < 8 and abs((t1[1] - t0[1]) - 80) < 8, (
        f"drag did not pan by the drag delta: {t0} -> {t1}"
    )

    page.wait_for_timeout(700)
    after_cursor = get_cursors(api, weave)["uitest-clement"]
    assert after_cursor["node_id"] == before_cursor["node_id"], (
        "a drag that started on a square was treated as a click (cursor moved)"
    )
    assert after_cursor["updated"] == before_cursor["updated"]

    # a normal click right AFTER a drag must still work (dragDist resets);
    # refit first so every square is back inside the viewport
    page.get_by_role("button", name="fit weave", exact=True).click()
    page.wait_for_timeout(250)
    w = get_weave(api, weave)
    i, target = find_identifiable_square(
        page, w["nodes"], exclude={after_cursor["node_id"]}
    )
    page.locator(SQUARES).nth(i).click()
    wait_until(
        lambda: get_cursors(api, weave)["uitest-clement"]["node_id"] == target,
        msg="click-after-drag still moves the cursor",
    )


def test_wheel_pans_and_ctrl_wheel_zooms(page_as, weave):
    page = page_as("uitest-clement", weave)
    goto_graph(page)
    box = page.locator(GRAPH).bounding_box()
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(cx, cy)

    # plain wheel pans (ty -= deltaY)
    t0 = view_transform(page)
    page.mouse.wheel(0, 90)
    page.wait_for_timeout(150)
    t1 = view_transform(page)
    assert abs((t0[1] - t1[1]) - 90) < 2, f"plain wheel did not pan: {t0} -> {t1}"
    assert t1[2] == t0[2], "plain wheel changed the zoom"

    # ctrl+wheel zooms, anchored at the pointer
    page.keyboard.down("Control")
    page.mouse.wheel(0, -240)
    page.keyboard.up("Control")
    page.wait_for_timeout(150)
    t2 = view_transform(page)
    assert t2[2] > t1[2] * 1.2, f"ctrl+wheel up did not zoom in: scale {t1[2]} -> {t2[2]}"

    page.keyboard.down("Control")
    page.mouse.wheel(0, 240)
    page.keyboard.up("Control")
    page.wait_for_timeout(150)
    t3 = view_transform(page)
    assert t3[2] < t2[2], f"ctrl+wheel down did not zoom out: {t2[2]} -> {t3[2]}"


def test_fit_weave_button_fits_content(page_as, weave):
    page = page_as("uitest-clement", weave)
    goto_graph(page)
    assert squares_inside_container(page), "initial mount should already fit the weave"

    # shove the content way off-screen
    box = page.locator(GRAPH).bounding_box()
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    for _ in range(3):
        page.mouse.move(cx, cy)
        page.mouse.down()
        page.mouse.move(cx + 400, cy + 350, steps=4)
        page.mouse.up()
    assert not squares_inside_container(page), "panning failed to move content off-screen"

    page.get_by_role("button", name="fit weave", exact=True).click()
    page.wait_for_timeout(250)
    assert squares_inside_container(page), (
        "fit-weave header button did not bring all squares back into the viewport"
    )


def test_live_update_new_squares_without_reload(page_as, api, weave):
    page = page_as("uitest-clement", weave)
    goto_graph(page)
    park_mouse(page)
    w = get_weave(api, weave)
    n0_dom = page.locator(SQUARES).count()
    assert n0_dom == len(w["nodes"])

    root_id = next(nid for nid, n in w["nodes"].items() if not n["parents"])
    gen_id = next(  # /gen takes generator_id now (docs/generators-api.md)
        g["id"]
        for g in api.get("/generators?profile=uitest-clement").json()
        if g["name"] == "default"
    )
    resp = api.post(
        f"/weaves/{weave}/gen",
        json={"node_id": root_id, "cursor": "uitest-claude",
              "generator_id": gen_id, "params": {"n": 2}},
    )
    resp.raise_for_status()
    new_nodes = resp.json()
    assert len(new_nodes) == 2
    n_expected = len(get_weave(api, weave)["nodes"])
    assert n_expected == n0_dom + 2

    deadline = time.time() + 8.0
    while time.time() < deadline:
        if page.locator(SQUARES).count() == n_expected:
            break
        page.wait_for_timeout(250)
    assert page.locator(SQUARES).count() == n_expected, (
        f"new squares did not appear live: DOM {page.locator(SQUARES).count()} "
        f"!= API {n_expected}"
    )
