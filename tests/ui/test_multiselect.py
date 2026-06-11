"""Canvas multi-select + bulk actions (selection.svelte.ts, SelectionBar.svelte).

shift+drag = rubber-band selection (must NOT pan); shift+click = toggle one card
(must NOT move any cursor); selection is local-only (never synced/persisted).
Bulk bar: bookmark all / collapse all / delete all (confirm, cascades) / clear;
Escape clears. Mutations verified through the REST API, not just the DOM.

Selector map:
  g.card[data-node-id]          a canvas node card (Canvas.svelte/NodeCard.svelte)
  g.card rect.bg                the card body rect (click target)
  rect.select-ring              solid blue multi-select ring (one per selected card)
  rect.rubber                   the in-drag rubber-band rect (SVG overlay)
  .selbar                       floating bulk-action bar (visible iff selection)
"""

import re
import time

# ---------------------------------------------------------------- helpers


def weave_json(api, wid):
    r = api.get(f"/weaves/{wid}")
    r.raise_for_status()
    return r.json()


def cursors_state(api, wid):
    r = api.get(f"/weaves/{wid}/cursors")
    r.raise_for_status()
    return {n: (c["node_id"], c["updated"]) for n, c in r.json().items()}


def wait_until(page, predicate, deadline_s=8.0, interval_ms=200):
    end = time.monotonic() + deadline_s
    val = predicate()
    while not val and time.monotonic() < end:
        page.wait_for_timeout(interval_ms)
        val = predicate()
    return val


_NUM = re.compile(r"-?\d+(?:\.\d+)?(?:e[+-]?\d+)?")


def transform_of(page):
    raw = page.locator(".canvas svg > g").first.get_attribute("transform")
    return [float(x) for x in _NUM.findall(raw)]


def card_rect(page, node_id):
    """The card body rect for a node id (geometry only — NOT a click target:
    the foreignObject .text div overlays it and intercepts pointer events)."""
    card = page.locator(f'g.card[data-node-id="{node_id}"]')
    assert card.count() == 1, f"card for {node_id[:8]} not rendered (culled?)"
    return card.locator("rect.bg")


def visible_point_of_card(page, node_id):
    """A screen point inside BOTH the card and the canvas viewport. Cards can
    extend past the canvas edge (clipped by overflow): their element center may
    sit over the footer/header, so locator.click() gets intercepted."""
    bb = card_rect(page, node_id).bounding_box()
    cv = page.locator(".canvas").bounding_box()
    x0 = max(bb["x"], cv["x"]) + 10
    x1 = min(bb["x"] + bb["width"], cv["x"] + cv["width"]) - 10
    y0 = max(bb["y"], cv["y"]) + 10
    y1 = min(bb["y"] + bb["height"], cv["y"] + cv["height"]) - 10
    assert x0 < x1 and y0 < y1, f"card {node_id[:8]} not visibly inside the canvas"
    return (x0 + x1) / 2, (y0 + y1) / 2


def shift_click_card(page, node_id):
    # raw mouse click at a visible point; the event lands on the card's text div
    # (or bg rect) and bubbles to the card g's click handler
    x, y = visible_point_of_card(page, node_id)
    page.keyboard.down("Shift")
    page.mouse.click(x, y)
    page.keyboard.up("Shift")
    page.wait_for_timeout(150)


def ring_of(page, node_id):
    return page.locator(f'g.card[data-node-id="{node_id}"] rect.select-ring')


def seed_ids(api, wid):
    """root1 (human, 3 children) + its kids, from the seeded weave shape."""
    w = weave_json(api, wid)
    root1 = next(
        r for r in w["roots"] if len(w["nodes"][r]["children"]) == 3
    )
    kids = w["nodes"][root1]["children"]
    return w, root1, kids


def fit_weave(page):
    """Ctrl+0 (fit_to_weave default binding): bring every card on screen — the
    initial cursor-centered view clips some target cards at the canvas edge."""
    page.keyboard.press("Control+0")
    page.wait_for_timeout(300)


# ---------------------------------------------------------------- rubber band


def test_shift_drag_selects_without_panning(weave, page_as, api):
    """shift+drag draws a rubber rect, selects every intersected card, does NOT
    pan the canvas, and does NOT move any cursor."""
    page = page_as("uitest-clement", weave)
    w, root1, kids = seed_ids(api, weave)
    fit_weave(page)
    cursors_before = cursors_state(api, weave)
    tf_before = transform_of(page)

    # drag a rect spanning root1 and its kids column (>= 2 cards), endpoints
    # clamped into the canvas viewport (cards can poke past its clipped edges)
    b1 = card_rect(page, root1).bounding_box()
    b2 = card_rect(page, kids[1]).bounding_box()
    cv = page.locator(".canvas").bounding_box()
    x0 = max(min(b1["x"], b2["x"]) - 12, cv["x"] + 5)
    y0 = max(min(b1["y"], b2["y"]) - 12, cv["y"] + 5)
    x1 = min(max(b1["x"] + b1["width"], b2["x"] + b2["width"]) + 12, cv["x"] + cv["width"] - 5)
    y1 = min(max(b1["y"] + b1["height"], b2["y"] + b2["height"]) + 12, cv["y"] + cv["height"] - 5)

    page.keyboard.down("Shift")
    page.mouse.move(x0, y0)
    page.mouse.down()
    page.mouse.move((x0 + x1) / 2, (y0 + y1) / 2, steps=4)
    assert page.locator("rect.rubber").count() == 1, "rubber rect not drawn mid-drag"
    page.mouse.move(x1, y1, steps=6)
    page.mouse.up()
    page.keyboard.up("Shift")
    page.wait_for_timeout(200)

    assert page.locator("rect.rubber").count() == 0, "rubber rect outlived the drag"
    n_rings = page.locator("rect.select-ring").count()
    assert n_rings >= 2, f"rubber-band selected {n_rings} cards (expected >= 2)"
    # root1 and kids[1] are definitely inside the rect
    assert ring_of(page, root1).count() == 1
    assert ring_of(page, kids[1]).count() == 1

    # the drag must NOT have panned
    assert transform_of(page) == tf_before, "shift+drag panned the canvas"
    # and must NOT have touched any cursor
    page.wait_for_timeout(800)
    assert cursors_state(api, weave) == cursors_before, "rubber-band moved a cursor"

    # the bulk bar reports the same count
    bar = page.locator(".selbar")
    assert bar.is_visible(), "bulk-action bar did not appear"
    assert f"{n_rings} selected" in bar.inner_text()


# ---------------------------------------------------------------- shift+click


def test_shift_click_toggles_and_keeps_cursors(weave, page_as, api):
    """shift+click selects a card, shift+click again deselects it; neither click
    moves any cursor (REST-verified, timestamps included)."""
    page = page_as("uitest-clement", weave)
    w, root1, kids = seed_ids(api, weave)
    fit_weave(page)
    cursors_before = cursors_state(api, weave)

    shift_click_card(page, root1)
    assert ring_of(page, root1).count() == 1, "shift+click did not select the card"
    assert page.locator(".selbar").is_visible()
    assert "1 selected" in page.locator(".selbar").inner_text()

    shift_click_card(page, kids[1])
    assert ring_of(page, kids[1]).count() == 1
    assert "2 selected" in page.locator(".selbar").inner_text()

    shift_click_card(page, root1)  # toggle OFF
    assert ring_of(page, root1).count() == 0, "second shift+click did not deselect"
    assert "1 selected" in page.locator(".selbar").inner_text()

    page.wait_for_timeout(800)  # give any wrongful cursor PUT time to land
    assert cursors_state(api, weave) == cursors_before, "shift+click moved a cursor"


# ---------------------------------------------------------------- bulk actions


def test_bulk_bookmark_persists(weave, page_as, api):
    """'bookmark all' sets bookmarked=true on every selected node, server-side."""
    page = page_as("uitest-clement", weave)
    w, root1, kids = seed_ids(api, weave)
    fit_weave(page)
    targets = [root1, kids[1]]
    assert all(not w["nodes"][t]["bookmarked"] for t in targets), "targets pre-bookmarked"

    for t in targets:
        shift_click_card(page, t)
    page.locator(".selbar button", has_text="bookmark all").click()

    assert wait_until(
        page,
        lambda: all(weave_json(api, weave)["nodes"][t]["bookmarked"] for t in targets),
    ), "bulk bookmark did not persist via REST"
    # selection survives a bookmark (it is not a destructive action)
    assert page.locator("rect.select-ring").count() == 2


def test_bulk_collapse(weave, page_as, api):
    """'collapse all' collapses each selected node's subtree (client-side)."""
    page = page_as("uitest-clement", weave)
    w, root1, kids = seed_ids(api, weave)
    fit_weave(page)
    assert card_rect(page, kids[0]).count() == 1  # children visible before

    shift_click_card(page, root1)
    page.locator(".selbar button", has_text="collapse all").click()
    page.wait_for_timeout(300)

    for k in kids:
        assert page.locator(f'g.card[data-node-id="{k}"]').count() == 0, (
            "collapse all left a child card visible"
        )
    assert page.locator(f'g.card[data-node-id="{root1}"]').count() == 1


def test_bulk_delete_cascades_no_confirm_undo_toast(weave, page_as, api):
    """'delete all' asks NO confirmation (undo replaces it): the selected
    nodes AND their subtrees go straight away server-side, and ONE toast with
    an "undo" button covers the whole batch."""
    page = page_as("uitest-clement", weave)
    w, root1, kids = seed_ids(api, weave)
    fit_weave(page)
    victim = kids[1]  # has 2 leaf children; no cursor in its subtree
    subtree = [victim, *w["nodes"][victim]["children"]]
    assert len(subtree) == 3

    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
    shift_click_card(page, victim)
    page.locator(".selbar button", has_text="delete all").click()

    def gone():
        nodes = weave_json(api, weave)["nodes"]
        return all(nid not in nodes for nid in subtree)

    assert wait_until(page, gone), "bulk delete did not remove the subtree via REST"
    assert dialogs == [], f"bulk delete must not open a native confirm: {dialogs}"
    # one toast for the whole batch, naming the cascaded count, offering undo
    assert wait_until(
        page, lambda: page.get_by_test_id("toast-action").count() == 1, deadline_s=4
    ), "expected exactly one undo toast for the whole bulk delete"
    toast_text = page.locator(".toast", has=page.get_by_test_id("toast-action")).inner_text()
    assert "3 nodes deleted" in toast_text, f"toast does not name the count: {toast_text!r}"
    # selection cleared after the delete -> bar gone
    assert wait_until(page, lambda: page.locator(".selbar").count() == 0, deadline_s=4), (
        "bulk bar still visible after deleting the whole selection"
    )


# ---------------------------------------------------------------- escape


def test_escape_clears_selection(weave, page_as, api):
    page = page_as("uitest-clement", weave)
    w, root1, kids = seed_ids(api, weave)
    fit_weave(page)

    shift_click_card(page, root1)
    shift_click_card(page, kids[1])
    assert page.locator("rect.select-ring").count() == 2

    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    assert page.locator("rect.select-ring").count() == 0, "Escape did not clear selection"
    assert page.locator(".selbar").count() == 0, "bulk bar survived Escape"
    # nothing was deleted/changed server-side
    assert len(weave_json(api, weave)["nodes"]) == len(w["nodes"])


# ---------------------------------------------------------------- Delete key (item 7)


def test_delete_key_deletes_whole_selection(weave, page_as, api):
    """With a multi-selection active, the Delete key takes the SelectionBar
    bulk path: every selected node (+ cascade) goes, ONE undo toast for the
    batch, no confirmation, selection cleared."""
    page = page_as("uitest-clement", weave)
    w, root1, kids = seed_ids(api, weave)
    fit_weave(page)
    victim = kids[1]  # 2 leaf children -> 3-node cascade
    subtree = [victim, *w["nodes"][victim]["children"]]

    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
    shift_click_card(page, victim)
    assert "1 selected" in page.locator(".selbar").inner_text()

    # focus is on the canvas area (not an editable) -> Delete = bulk delete
    page.keyboard.press("Delete")

    def gone():
        nodes = weave_json(api, weave)["nodes"]
        return all(nid not in nodes for nid in subtree)

    assert wait_until(page, gone), "Delete key did not remove the selected subtree"
    assert dialogs == [], f"Delete-key bulk delete must not confirm: {dialogs}"
    assert wait_until(
        page, lambda: page.get_by_test_id("toast-action").count() == 1, deadline_s=4
    ), "expected exactly one undo toast for the Delete-key batch"
    assert wait_until(page, lambda: page.locator(".selbar").count() == 0, deadline_s=4), (
        "selection not cleared after Delete-key bulk delete"
    )
    # the cursor node was NOT deleted (selection took precedence over cursor)
    cur = weave_json(api, weave)["cursors"]["uitest-clement"]["node_id"]
    assert cur in weave_json(api, weave)["nodes"]


def test_delete_key_while_typing_stays_text_deletion(weave, page_as, api):
    """Delete pressed while the focus is in the editable doc must NOT delete
    nodes, selection or not."""
    page = page_as("uitest-clement", weave)
    w, root1, kids = seed_ids(api, weave)
    fit_weave(page)
    shift_click_card(page, kids[1])
    n_before = len(weave_json(api, weave)["nodes"])

    page.locator(".doc").click()  # focus the contenteditable
    page.keyboard.press("Delete")
    page.wait_for_timeout(800)

    assert len(weave_json(api, weave)["nodes"]) == n_before, (
        "Delete inside the doc deleted nodes (editable guard broken)"
    )
