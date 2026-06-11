"""Adversarial UI tests for the canvas surface (Canvas.svelte + NodeCard.svelte).

Run: cd <repo> && uv run pytest tests/ui/test_canvas.py -q

Every mutation is verified through the REST API, not just the DOM. Each test gets
a freshly seeded weave (conftest `weave` fixture) and a fresh browser context with
identity "uitest-clement" (so weave cursors `uitest-clement` is "mine").

Seed shape (scripts/seed_dev_weave.py): root1 (human snippet) -> 3 token branches
("kids"); kids[0] has 3 children, the first of which has 2 grandchildren; kids[1]
has 2 leaf children; kids[2] was split; root2 (human snippet) has 2 leaf children.
Cursors: uitest-clement @ kids[0], uitest-claude deep in kids[0]'s subtree. 2 bookmarks.
"""

import re
import time

from playwright.sync_api import expect

# ---------------------------------------------------------------- API helpers


def weave_json(api, wid):
    r = api.get(f"/weaves/{wid}")
    r.raise_for_status()
    return r.json()


def node_text(node):
    c = node["content"]
    if c["type"] == "snippet":
        return c["text"]
    return "".join(t["text"] for t in c["tokens"])


def cursor_node(api, wid, name):
    cur = weave_json(api, wid)["cursors"].get(name)
    return cur["node_id"] if cur else None


def descendants(data, nid):
    out, stack = [], [nid]
    while stack:
        cur = stack.pop()
        out.append(cur)
        stack.extend(data["nodes"][cur]["children"])
    return out


# ---------------------------------------------------------------- DOM helpers


def card_locator(page, node, words=8):
    """Cards carry no node-id attribute; locate by a normalized text snippet."""
    snippet = " ".join(node_text(node).split()[:words])
    return page.locator("g.card").filter(has_text=snippet)


def locate_card(page, node):
    ws = node_text(node).split()
    assert ws, f"node {node['id']} has no text to locate by"
    for n in (8, 14, 22, len(ws)):
        loc = card_locator(page, node, n)
        if loc.count() == 1:
            return loc
    raise AssertionError(
        f"no unique canvas card for node {node['id']!r} (text starts {ws[:8]})"
    )


_NUM = re.compile(r"-?\d+(?:\.\d+)?(?:e[+-]?\d+)?")


def transform_of(page):
    raw = page.locator(".canvas svg > g").first.get_attribute("transform")
    nums = [float(x) for x in _NUM.findall(raw)]
    assert len(nums) == 3, f"unexpected canvas transform {raw!r}"
    return nums  # [tx, ty, scale]


def raw_transform(page):
    return page.locator(".canvas svg > g").first.get_attribute("transform")


def canvas_box(page):
    box = page.locator(".canvas").bounding_box()
    assert box is not None, "canvas not mounted"
    return box


def fit_weave(page):
    """Bring every card into the viewport (cards offscreen are culled from the DOM)."""
    page.get_by_title("fit whole weave (ctrl+0)").click()
    page.wait_for_timeout(200)


def card_body(card):
    """The card's clickable body group. Clicking `rect.bg` directly trips
    playwright's hit-target check (the foreignObject text div is on top), but
    both are children of the same `g.clickable`, which is what a user hits."""
    return card.locator("g.clickable:has(> rect.bg)")


def hover_card(page, card):
    card_body(card).hover()
    page.wait_for_timeout(80)


def tool(card, title_substr):
    """A hover-toolbar button, by its <title> text (DOM order is impl detail)."""
    t = card.locator("g.tool").filter(has_text=title_substr)
    expect(t, f"toolbar button {title_substr!r}").to_have_count(1)
    return t


def poll_until(page, fn, desc, timeout_s=6.0):
    deadline = time.monotonic() + timeout_s
    while True:
        value = fn()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout_s}s waiting for: {desc}")
        page.wait_for_timeout(150)


# ================================================================ click vs drag


def test_click_card_moves_my_cursor(weave, page_as, api):
    page = page_as("uitest-clement", weave)
    data = weave_json(api, weave)
    before = data["cursors"]["uitest-clement"]["node_id"]
    target = data["nodes"][data["roots"][1]]  # second root: never the seeded cursor
    assert target["id"] != before

    fit_weave(page)
    card_body(locate_card(page, target)).click()

    poll_until(
        page,
        lambda: cursor_node(api, weave, "uitest-clement") == target["id"],
        "uitest-clement's cursor to move to the clicked card (API)",
    )
    cur = weave_json(api, weave)["cursors"]["uitest-clement"]
    assert cur["moved_by"] == "uitest-clement"
    # DOM follows: dashed cursor ring lands on the clicked card after the WS refetch
    expect(locate_card(page, target).locator("rect.cursor-ring")).to_have_count(1)


def test_drag_starting_on_card_pans_and_does_not_move_cursor(weave, page_as, api):
    page = page_as("uitest-clement", weave)
    data = weave_json(api, weave)
    before = data["cursors"]["uitest-clement"]["node_id"]
    target = data["nodes"][data["roots"][1]]
    assert target["id"] != before

    fit_weave(page)
    bb = locate_card(page, target).locator("rect.bg").bounding_box()
    cx, cy = bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2
    tx0, ty0, s0 = transform_of(page)

    page.mouse.move(cx, cy)
    page.mouse.down()
    for i in range(1, 9):  # 40px right, 24px down, in steps (>4px threshold)
        page.mouse.move(cx + i * 5, cy + i * 3)
    page.mouse.up()

    tx1, ty1, s1 = transform_of(page)
    assert s1 == s0, "drag-pan must not change zoom"
    assert tx1 - tx0 > 25, f"expected rightward pan, tx {tx0} -> {tx1}"
    assert ty1 - ty0 > 12, f"expected downward pan, ty {ty0} -> {ty1}"

    # an erroneous cursor PUT would land well within this window
    page.wait_for_timeout(800)
    assert cursor_node(api, weave, "uitest-clement") == before, (
        "a drag that started on a card must NOT be treated as a click"
    )


# ================================================================ wheel / zoom


def test_wheel_scroll_pans_without_zooming(weave, page_as, api):
    page = page_as("uitest-clement", weave)
    cb = canvas_box(page)
    page.mouse.move(cb["x"] + cb["width"] / 2, cb["y"] + cb["height"] / 2)
    tx0, ty0, s0 = transform_of(page)

    page.mouse.wheel(40, 60)
    page.wait_for_timeout(100)

    tx1, ty1, s1 = transform_of(page)
    assert s1 == s0, "plain wheel must pan, not zoom"
    assert abs((tx0 - tx1) - 40) < 1, f"wheel deltaX should pan x by -40, got {tx1 - tx0}"
    assert abs((ty0 - ty1) - 60) < 1, f"wheel deltaY should pan y by -60, got {ty1 - ty0}"


def test_ctrl_wheel_zooms_at_pointer_and_clamps_at_native(weave, page_as, api):
    page = page_as("uitest-clement", weave)
    cb = canvas_box(page)
    # Integer pointer coords: the browser rounds an event's clientX/Y to whole
    # pixels, and any fractional remainder (the canvas top can be non-integral,
    # e.g. 89.56px) is amplified ~1/scale by the world-point math below — so a
    # fractional pointer would spuriously "drift". onWheel itself is exact.
    px = round(cb["x"] + cb["width"] * 0.6)
    py = round(cb["y"] + cb["height"] * 0.4)
    page.mouse.move(px, py)

    tx0, ty0, s0 = transform_of(page)
    assert s0 == 1.0, "initial view should be at native scale"

    page.keyboard.down("Control")
    try:
        # zoom IN from 1.0: clamped at 1.0, transform must not move at all
        page.mouse.wheel(0, -400)
        page.wait_for_timeout(100)
        tx1, ty1, s1 = transform_of(page)
        assert s1 == 1.0, f"zoom must clamp at 1.0, got {s1}"
        assert (tx1, ty1) == (tx0, ty0), "clamped zoom must not translate the view"

        # zoom OUT: scale drops, world point under the pointer stays fixed
        wx0 = (px - cb["x"] - tx1) / s1
        wy0 = (py - cb["y"] - ty1) / s1
        page.mouse.wheel(0, 600)
        page.wait_for_timeout(100)
        tx2, ty2, s2 = transform_of(page)
        assert s2 < s1, f"ctrl+wheel down should zoom out, scale {s1} -> {s2}"
        wx1 = (px - cb["x"] - tx2) / s2
        wy1 = (py - cb["y"] - ty2) / s2
        assert abs(wx1 - wx0) < 1 and abs(wy1 - wy0) < 1, (
            f"zoom must be about the pointer: world point drifted "
            f"({wx0:.1f},{wy0:.1f}) -> ({wx1:.1f},{wy1:.1f})"
        )

        # zoom IN hard: clamps back at exactly 1.0
        page.mouse.wheel(0, -5000)
        page.wait_for_timeout(100)
        _, _, s3 = transform_of(page)
        assert s3 == 1.0, f"zoom-in must clamp at 1.0, got {s3}"
    finally:
        page.keyboard.up("Control")


# ================================================================ hover toolbar


def test_toolbar_generate_adds_children(weave, page_as, api):
    page = page_as("uitest-clement", weave)
    data = weave_json(api, weave)
    root2 = data["roots"][1]
    target = data["nodes"][data["nodes"][root2]["children"][0]]  # a leaf
    assert target["children"] == []
    cursor_before = data["cursors"]["uitest-clement"]["node_id"]

    fit_weave(page)
    card = locate_card(page, target)
    expect(card.locator("g.tool")).to_have_count(0)  # toolbar only on hover
    hover_card(page, card)
    expect(card.locator("g.tool")).to_have_count(4)  # leaf: no collapse button

    tool(card, "generate completions").click()

    new_children = poll_until(
        page,
        lambda: weave_json(api, weave)["nodes"][target["id"]]["children"],
        "generated children to land on the clicked node (API)",
    )
    assert len(new_children) >= 1
    after = weave_json(api, weave)
    for cid in new_children:
        assert after["nodes"][cid]["creator"]["type"] == "model"
    # plain click must NOT move my cursor
    assert after["cursors"]["uitest-clement"]["node_id"] == cursor_before

    # toolbar unmounts when un-hovered
    page.mouse.move(10, 10)
    expect(card.locator("g.tool")).to_have_count(0)


def test_toolbar_add_child_creates_human_node_and_moves_cursor(weave, page_as, api):
    page = page_as("uitest-clement", weave)
    data = weave_json(api, weave)
    root2 = data["roots"][1]
    target = data["nodes"][data["nodes"][root2]["children"][1]]  # another leaf
    assert target["children"] == []

    fit_weave(page)
    card = locate_card(page, target)
    hover_card(page, card)
    tool(card, "add empty child").click()

    children = poll_until(
        page,
        lambda: weave_json(api, weave)["nodes"][target["id"]]["children"],
        "an added child node (API)",
    )
    assert len(children) == 1
    after = weave_json(api, weave)
    child = after["nodes"][children[0]]
    assert child["creator"]["type"] == "human"
    assert child["creator"]["label"] == "uitest-clement"
    assert node_text(child) == ""
    assert after["cursors"]["uitest-clement"]["node_id"] == child["id"], (
        "add-child must move my cursor to the new child"
    )


def test_toolbar_bookmark_toggles_on_and_off(weave, page_as, api):
    page = page_as("uitest-clement", weave)
    data = weave_json(api, weave)
    target = data["nodes"][data["roots"][1]]
    assert not target["bookmarked"]

    fit_weave(page)
    card = locate_card(page, target)
    hover_card(page, card)
    tool(card, "bookmark node").click()

    poll_until(
        page,
        lambda: weave_json(api, weave)["nodes"][target["id"]]["bookmarked"],
        "bookmarked=true (API)",
    )
    expect(locate_card(page, target).locator("text.bookmark")).to_have_count(1)

    # toggle back off (re-resolve: the card re-rendered after the WS refetch)
    card = locate_card(page, target)
    hover_card(page, card)
    tool(card, "remove bookmark").click()
    poll_until(
        page,
        lambda: not weave_json(api, weave)["nodes"][target["id"]]["bookmarked"],
        "bookmarked=false (API)",
    )
    expect(locate_card(page, target).locator("text.bookmark")).to_have_count(0)


def test_toolbar_delete_removes_subtree_and_relocates_stranded_cursors(
    weave, page_as, api
):
    page = page_as("uitest-clement", weave)
    data = weave_json(api, weave)
    root1 = data["roots"][0]
    victim = data["nodes"][data["nodes"][root1]["children"][0]]  # kids[0]: deep subtree
    doomed = descendants(data, victim["id"])
    assert len(doomed) >= 4
    # uitest-clement's seeded cursor sits on the victim; park uitest-claude's deep inside too
    assert data["cursors"]["uitest-clement"]["node_id"] == victim["id"]
    deep_leaf = [n for n in doomed if not data["nodes"][n]["children"]][0]
    api.put(
        f"/weaves/{weave}/cursors/uitest-claude",
        json={"node_id": deep_leaf, "moved_by": "uitest-claude"},
    ).raise_for_status()

    fit_weave(page)
    card = locate_card(page, victim)
    hover_card(page, card)
    expect(card.locator("g.tool")).to_have_count(5)  # has children: collapse btn too
    tool(card, "delete node").click()

    poll_until(
        page,
        lambda: victim["id"] not in weave_json(api, weave)["nodes"],
        "the deleted node to vanish (API)",
    )
    after = weave_json(api, weave)
    for nid in doomed:
        assert nid not in after["nodes"], f"descendant {nid} survived the delete"
    # stranded cursors relocate to the deleted node's parent (store.remove_node)
    assert after["cursors"]["uitest-clement"]["node_id"] == root1
    assert after["cursors"]["uitest-claude"]["node_id"] == root1
    expect(card_locator(page, victim)).to_have_count(0)


def test_toolbar_collapse_stub_peek_and_expand(weave, page_as, api):
    page = page_as("uitest-clement", weave)
    data = weave_json(api, weave)
    root1 = data["roots"][0]
    target = data["nodes"][data["nodes"][root1]["children"][0]]  # kids[0]
    first_child = data["nodes"][target["children"][0]]
    grandchild = data["nodes"][first_child["children"][0]]
    nodes_before = len(data["nodes"])

    fit_weave(page)
    expect(card_locator(page, first_child)).to_have_count(1)
    card = locate_card(page, target)
    hover_card(page, card)
    tool(card, "collapse subtree").click()

    # subtree cards unmount, '…' stub appears
    expect(card_locator(page, first_child)).to_have_count(0)
    expect(card_locator(page, grandchild)).to_have_count(0)
    stub = card.locator("g.stub")
    expect(stub).to_have_count(1)

    # collapse is client-local: the weave itself is untouched
    assert len(weave_json(api, weave)["nodes"]) == nodes_before

    # stub stays visible with the pointer parked far away (un-hovered)
    page.mouse.move(30, 30)
    page.wait_for_timeout(300)
    expect(stub).to_have_count(1)
    expect(stub).to_be_visible()

    # hovering the stub peeks at the first hidden child
    stub.hover()
    expect(card.locator("g.stub.peek")).to_have_count(1)

    # clicking the stub expands the subtree again
    stub.click()
    expect(card_locator(page, first_child)).to_have_count(1)
    expect(card.locator("g.stub")).to_have_count(0)


# ================================================================ the '+' strip


def strip_point(page, card, frac=0.75):
    """A point inside the card's hover strip (right of the card), clear of the
    `+` button itself (button spans 12..33 of the 60-wide strip)."""
    bb = card.locator("rect.bg").bounding_box()
    _, _, s = transform_of(page)
    return bb["x"] + bb["width"] + 60 * s * frac, bb["y"] + bb["height"] / 2


def test_strip_hover_reveals_plus_and_click_generates(weave, page_as, api):
    page = page_as("uitest-clement", weave)
    data = weave_json(api, weave)
    target = data["nodes"][data["nodes"][data["roots"][1]]["children"][0]]  # leaf
    cursor_before = data["cursors"]["uitest-clement"]["node_id"]

    fit_weave(page)
    card = locate_card(page, target)
    expect(card.locator("g.gen")).to_have_count(0)  # hidden until hover

    # enter the EMPTY strip area directly (never crossing the card itself)
    sx, sy = strip_point(page, card)
    page.mouse.move(sx, sy)
    gen = card.locator("g.gen")
    expect(gen).to_have_count(1)

    gen.click()
    new_children = poll_until(
        page,
        lambda: weave_json(api, weave)["nodes"][target["id"]]["children"],
        "strip + click to generate children (API)",
    )
    assert len(new_children) >= 1
    # plain click on + does NOT move my cursor
    page.wait_for_timeout(500)
    assert cursor_node(api, weave, "uitest-clement") == cursor_before


def test_strip_ctrl_click_and_middle_click_also_move_cursor(weave, page_as, api):
    page = page_as("uitest-clement", weave)
    data = weave_json(api, weave)
    root1, root2 = data["roots"]
    t_ctrl = data["nodes"][data["nodes"][root2]["children"][1]]  # leaf
    kids1 = data["nodes"][data["nodes"][root1]["children"][1]]
    t_mid = data["nodes"][kids1["children"][0]]  # another leaf
    assert t_ctrl["children"] == [] and t_mid["children"] == []

    # --- ctrl+click on the strip `+`
    fit_weave(page)
    card = locate_card(page, t_ctrl)
    page.mouse.move(*strip_point(page, card))
    expect(card.locator("g.gen")).to_have_count(1)
    card.locator("g.gen").click(modifiers=["Control"])

    new_ctrl = poll_until(
        page,
        lambda: weave_json(api, weave)["nodes"][t_ctrl["id"]]["children"],
        "ctrl+click + to generate children (API)",
    )
    poll_until(
        page,
        lambda: cursor_node(api, weave, "uitest-clement") in new_ctrl,
        "ctrl+click + to also move my cursor to a generated child (API)",
    )

    # --- middle-click on another leaf's strip `+` (layout shifted: re-fit, re-resolve)
    fit_weave(page)
    card = locate_card(page, t_mid)
    page.mouse.move(*strip_point(page, card))
    expect(card.locator("g.gen")).to_have_count(1)
    card.locator("g.gen").click(button="middle")

    new_mid = poll_until(
        page,
        lambda: weave_json(api, weave)["nodes"][t_mid["id"]]["children"],
        "middle-click + to generate children (API)",
    )
    poll_until(
        page,
        lambda: cursor_node(api, weave, "uitest-clement") in new_mid,
        "middle-click + to also move my cursor to a generated child (API)",
    )


def test_cursor_pill_does_not_swallow_clicks_on_card_above(weave, page_as, api):
    """Pills render 21 world-px above their card but the row gap is only 14, so a
    pill overlaps the bottom 7px of the card above. The pill is an indicator, not
    a control — clicking the visible body of the upper card must still move my
    cursor there (KNOWN-BUG candidate: a painted pill rect intercepts the click)."""
    page = page_as("uitest-clement", weave)
    data = weave_json(api, weave)
    root1 = data["roots"][0]
    kids0 = data["nodes"][root1]["children"][0]
    gk0 = data["nodes"][kids0]["children"][0]
    upper_id, lower_id = data["nodes"][gk0]["children"][:2]  # adjacent depth-3 leaves

    # park uitest-claude's cursor on the LOWER card -> its pill overlaps the upper card
    api.put(
        f"/weaves/{weave}/cursors/uitest-claude",
        json={"node_id": lower_id, "moved_by": "uitest-claude"},
    ).raise_for_status()

    fit_weave(page)
    lower_card = locate_card(page, data["nodes"][lower_id])
    expect(  # the pill is rendered before we aim at the overlap zone
        lower_card.locator("text.pill-label").filter(has_text="uitest-claude")
    ).to_have_count(1)

    upper_card = locate_card(page, data["nodes"][upper_id])
    bb = upper_card.locator("rect.bg").bounding_box()
    _, _, s = transform_of(page)
    # bottom-left sliver of the upper card, inside the pill-overlap zone
    page.mouse.click(bb["x"] + 15 * s, bb["y"] + bb["height"] - 2 * s)

    poll_until(
        page,
        lambda: cursor_node(api, weave, "uitest-clement") == upper_id,
        "clicking inside the upper card's body to move my cursor there "
        "(a cursor pill on the card below must not swallow the click)",
    )


# ================================================================ context menu


def test_right_click_opens_context_menu(weave, page_as, api):
    page = page_as("uitest-clement", weave)
    data = weave_json(api, weave)
    target = data["nodes"][data["roots"][1]]
    cursor_before = data["cursors"]["uitest-clement"]["node_id"]

    fit_weave(page)
    card_body(locate_card(page, target)).click(button="right")
    expect(page.locator("div.menu")).to_be_visible()

    # right-click must not act as a left click (no cursor move)
    page.wait_for_timeout(600)
    assert cursor_node(api, weave, "uitest-clement") == cursor_before


# ================================================================ fit commands


def test_fit_weave_and_fit_cursor_buttons(weave, page_as, api):
    page = page_as("uitest-clement", weave)
    data = weave_json(api, weave)
    cb = canvas_box(page)

    # shove the view far away first so "fit" has work to do
    page.mouse.move(cb["x"] + cb["width"] / 2, cb["y"] + cb["height"] / 2)
    page.mouse.wheel(2500, 1800)
    page.wait_for_timeout(100)
    t_panned = raw_transform(page)

    page.get_by_title("fit whole weave (ctrl+0)").click()
    page.wait_for_timeout(200)
    assert raw_transform(page) != t_panned, "fit-weave did not change the view"
    _, _, s_fit = transform_of(page)
    assert s_fit <= 1.0 + 1e-9

    # every node of the weave is rendered and fully inside the canvas viewport
    bgs = page.locator("g.card rect.bg")
    assert bgs.count() == len(data["nodes"]), (
        f"fit-weave should show all {len(data['nodes'])} cards, got {bgs.count()}"
    )
    for i in range(bgs.count()):
        bb = bgs.nth(i).bounding_box()
        assert bb["x"] >= cb["x"] - 1 and bb["y"] >= cb["y"] - 1, f"card {i} offscreen"
        assert bb["x"] + bb["width"] <= cb["x"] + cb["width"] + 1, f"card {i} offscreen"
        assert bb["y"] + bb["height"] <= cb["y"] + cb["height"] + 1, f"card {i} offscreen"

    # fit-cursor: zoom toward native (90%) and center my cursor's card
    page.get_by_title("center on my cursor (ctrl+9)").click()
    page.wait_for_timeout(200)
    _, _, s_cur = transform_of(page)
    assert 0.7 <= s_cur <= 1.0, f"fit-cursor should zoom near native (0.9), got {s_cur}"
    cur_node = data["nodes"][data["cursors"]["uitest-clement"]["node_id"]]
    bb = locate_card(page, cur_node).locator("rect.bg").bounding_box()
    ccx, ccy = bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2
    assert abs(ccx - (cb["x"] + cb["width"] / 2)) < 15, "cursor card not centered (x)"
    assert abs(ccy - (cb["y"] + cb["height"] / 2)) < 15, "cursor card not centered (y)"


# ================================================================ pointer suppression


def test_remote_node_add_never_moves_view_under_my_pointer(weave, page_as, api):
    page = page_as("uitest-clement", weave)
    data = weave_json(api, weave)
    n0 = len(data["nodes"])
    cb = canvas_box(page)

    # park the pointer INSIDE the canvas
    page.mouse.move(cb["x"] + cb["width"] / 2, cb["y"] + cb["height"] / 2)
    page.wait_for_timeout(100)
    t0 = raw_transform(page)

    api.post(
        f"/weaves/{weave}/nodes",
        json={
            "text": "remote interjection while uitest-clement hovers",
            "parent_id": data["roots"][0],
            "creator": {"type": "human", "label": "uitest-claude"},
        },
    ).raise_for_status()

    # the client provably received the change (footer node count updates via WS)
    expect(page.locator("footer")).to_contain_text(f"{n0 + 1} nodes")
    page.wait_for_timeout(500)  # any focus-follow effect would have fired by now
    assert raw_transform(page) == t0, (
        "view auto-scrolled while the pointer was inside the canvas (suppression broken)"
    )

    # complement: with the pointer OUTSIDE, focus-follow DOES move the view
    page.mouse.move(30, 30)  # header area
    page.wait_for_timeout(100)
    t1 = raw_transform(page)
    api.post(
        f"/weaves/{weave}/nodes",
        json={
            "text": "second remote interjection, pointer is away",
            "parent_id": data["roots"][1],
            "creator": {"type": "human", "label": "uitest-claude"},
        },
    ).raise_for_status()
    expect(page.locator("footer")).to_contain_text(f"{n0 + 2} nodes")
    poll_until(
        page,
        lambda: raw_transform(page) != t1,
        "focus-follow to center the new node once the pointer is outside",
        timeout_s=4.0,
    )


# ================================================================ center-on-demand (task #23)


def test_cursor_move_to_visible_node_does_not_pan(weave, page_as, api):
    """Selecting/navigating to a node already inside the viewport must NOT move
    the canvas; once the target is offscreen, the same action pans to it."""
    page = page_as("uitest-clement", weave)
    data = weave_json(api, weave)
    root1 = next(r for r in data["roots"] if "loom hummed" in node_text(data["nodes"][r]))
    kids = data["nodes"][root1]["children"]
    fit_weave(page)  # every card visible

    # pointer OUT of the canvas (else pointer-suppression masks the behavior):
    # move the cursor from the TREE list
    page.mouse.move(30, 30)
    page.wait_for_timeout(100)
    t0 = raw_transform(page)
    page.locator(f'.sidebar [data-node-id="{kids[1]}"]').click()
    poll_until(
        page,
        lambda: cursor_node(api, weave, "uitest-clement") == kids[1],
        "cursor onto a visible sibling",
    )
    page.wait_for_timeout(400)  # focus-follow would fire within this
    assert raw_transform(page) == t0, (
        "canvas panned to a node that was already fully visible"
    )

    # pan far away so EVERYTHING is offscreen -> the same tree-click must pan
    cb = canvas_box(page)
    page.mouse.move(cb["x"] + cb["width"] / 2, cb["y"] + cb["height"] / 2)
    page.mouse.wheel(2500, 2000)
    page.wait_for_timeout(100)
    page.mouse.move(30, 30)  # leave the canvas again
    page.wait_for_timeout(100)
    t1 = raw_transform(page)
    assert t1 != t0, "wheel pan did not move the view (test setup)"
    page.locator(f'.sidebar [data-node-id="{kids[0]}"]').click()
    poll_until(
        page,
        lambda: cursor_node(api, weave, "uitest-clement") == kids[0],
        "cursor onto the offscreen node",
    )
    poll_until(
        page,
        lambda: raw_transform(page) != t1,
        "canvas pans to an offscreen selection",
        timeout_s=4.0,
    )


# ================================================================ shift+scroll (task #25)


def test_shift_wheel_pans_horizontally(weave, page_as, api):
    """Shift+scroll pans the canvas on the X axis only (a vertical wheel maps
    onto horizontal movement); zoom and Y stay untouched."""
    page = page_as("uitest-clement", weave)
    cb = canvas_box(page)
    page.mouse.move(cb["x"] + cb["width"] / 2, cb["y"] + cb["height"] / 2)
    tx0, ty0, s0 = transform_of(page)

    page.keyboard.down("Shift")
    page.mouse.wheel(0, 80)  # vertical wheel while shift is held
    page.keyboard.up("Shift")
    page.wait_for_timeout(100)

    tx1, ty1, s1 = transform_of(page)
    assert s1 == s0, "shift+wheel must not zoom"
    assert abs(ty1 - ty0) < 1, f"shift+wheel must not pan vertically (dy={ty1 - ty0})"
    assert abs((tx0 - tx1) - 80) < 1, (
        f"shift+wheel should pan x by -80, got {tx1 - tx0}"
    )
