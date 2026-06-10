"""TextPane UI tests: thread doc, token/node tooltips, counterfactual branching,
caret + generate-at-caret, typing-to-append (no composer), context menu,
boundary ticks, auto-scroll, blank-weave placeholder.

Conventions: every mutation is verified through the REST API (not just the DOM);
generations on the fake backend take ~0.4-1.2s + a WS refetch, so all post-action
asserts poll with a generous deadline.

Selector map (web/src/lib/TextPane.svelte):
  .side-pane .scroller .doc          the thread document (contenteditable)
  .doc [data-node-id]                one span per thread node
  .doc .token                        per-token spans (tokens nodes only)
  .doc:empty::before                 CSS-only placeholder (data-placeholder attr)
  .caret-bar                         appears after a click in the text
  [role=tooltip]                     TokenTooltip / NodeTooltip popover
  .menu[role=menu]                   global context menu
"""

import json
import re
import time

import pytest

TIP_HOVER_MS = 700  # > TIP_SHOW_MS (350) with margin
WS_SETTLE_MS = 1000  # event -> WS push -> debounced refetch
GEN_DEADLINE_S = 8.0  # fake-backend generation + refetch


# ---------------------------------------------------------------- helpers


def get_thread(api, weave_id, cursor="uitest-clement"):
    r = api.get(f"/weaves/{weave_id}/cursors/{cursor}/thread")
    r.raise_for_status()
    return r.json()


def get_cursor(api, weave_id, name="uitest-clement"):
    r = api.get(f"/weaves/{weave_id}/cursors")
    r.raise_for_status()
    return r.json()[name]


def get_weave(api, weave_id):
    r = api.get(f"/weaves/{weave_id}")
    r.raise_for_status()
    return r.json()


def wait_until(page, predicate, deadline_s=GEN_DEADLINE_S, interval_ms=250):
    """Poll `predicate` until truthy or deadline; returns the last value."""
    end = time.monotonic() + deadline_s
    val = predicate()
    while not val and time.monotonic() < end:
        page.wait_for_timeout(interval_ms)
        val = predicate()
    return val


def tokens_node_of_thread(thread):
    """The (single) tokens node on uitest-clement's seeded thread, plus its path index."""
    for i, node in enumerate(thread["nodes"]):
        if node["content"]["type"] == "tokens":
            return i, node
    raise AssertionError("seeded uitest-clement thread has no tokens node")


def node_text(node):
    c = node["content"]
    return c["text"] if c["type"] == "snippet" else "".join(t["text"] for t in c["tokens"])


def collect_pageerrors(page):
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    return errors


def hover_token(page, index):
    """Hover the index-th token span until its TokenTooltip is open; return tip locator."""
    tok = page.locator(".doc .token").nth(index)
    tok.hover()
    page.wait_for_timeout(TIP_HOVER_MS)
    tip = page.locator("[role=tooltip]")
    assert tip.count() == 1, "token tooltip did not appear after hover dwell"
    return tip


def type_at_end(page, text):
    """Place the caret at the very end of the doc and type `text`."""
    # park the pointer off the doc first: a token tooltip opened by hover dwell
    # (pointer resting on the doc between actions) would intercept the click
    page.mouse.move(2, 2)
    page.wait_for_timeout(350)
    page.locator(".doc").click()
    page.evaluate(
        """() => {
            const doc = document.querySelector('.doc');
            const range = document.createRange();
            range.selectNodeContents(doc);
            range.collapse(false);
            const sel = document.getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
            doc.focus();
        }"""
    )
    page.keyboard.type(text)


@pytest.fixture()
def blank_weave(api):
    """A truly EMPTY weave (no nodes, no cursors) — the blank-page experience."""
    r = api.post("/weaves", json={"title": "ui-test blank weave"})
    r.raise_for_status()
    wid = r.json()["id"]
    yield wid
    api.delete(f"/weaves/{wid}")


# ---------------------------------------------------------------- the document


def test_doc_shows_my_cursor_thread(weave, page_as, api):
    """The doc is exactly my cursor's root->cursor thread concatenation."""
    page = page_as("uitest-clement", weave)
    thread = get_thread(api, weave)
    doc = page.locator(".side-pane .doc")
    assert doc.count() == 1, "thread doc not rendered"

    # one span per thread node, in path order
    spans = page.locator(".doc [data-node-id]")
    assert spans.count() == len(thread["path"])
    dom_ids = [spans.nth(i).get_attribute("data-node-id") for i in range(spans.count())]
    assert dom_ids == thread["path"]

    # per-node text and full concatenation match the API thread exactly
    for i, node in enumerate(thread["nodes"]):
        assert spans.nth(i).evaluate("el => el.textContent") == node_text(node)
    assert doc.evaluate("el => el.textContent") == thread["content"]


def test_boundary_ticks_on_multinode_thread(weave, page_as, api):
    """Thread with >=2 nodes renders the attribution-boundary tick on every node."""
    page = page_as("uitest-clement", weave)
    thread = get_thread(api, weave)
    assert len(thread["path"]) >= 2, "seeded uitest-clement thread should be >= 2 nodes"
    spans = page.locator(".doc [data-node-id]")
    boundary = page.locator(".doc [data-node-id].boundary")
    assert boundary.count() == spans.count() >= 2


# ---------------------------------------------------------------- tooltips


def test_token_tooltip_content_and_hover_persistence(weave, page_as, api):
    """Hover a token -> tooltip with probability % + counterfactual buttons;
    moving the pointer into the tooltip keeps it open past the hide delay."""
    page = page_as("uitest-clement", weave)
    thread = get_thread(api, weave)
    _, tok_node = tokens_node_of_thread(thread)
    tip = hover_token(page, 5)

    text = tip.inner_text()
    assert re.search(r"probability: \d+(\.\d+)?%", text), f"no probability line in: {text!r}"
    # counterfactual row: one button per top_logprob of token 5
    n_alts = len(tok_node["content"]["tokens"][5]["top_logprobs"])
    assert n_alts > 0, "seeded token has no top_logprobs (fake backend regression?)"
    assert tip.locator("button.alt").count() == n_alts
    # the sampled token's own button is disabled, the others clickable
    assert tip.locator("button.alt:disabled").count() >= 1
    assert tip.locator("button.alt:not(:disabled)").count() == n_alts - 1
    # gen config surfaced (genParams from the stored raw_request)
    assert "model=" in text and "temperature=" in text and "max_tokens=" in text, (
        f"gen config missing from token tooltip: {text!r}"
    )

    # travel pointer INTO the tooltip (crossing the anchor gap), wait past
    # TIP_HIDE_MS (250) -- contiguous hover region must keep it open
    box = tip.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=10)
    page.wait_for_timeout(600)
    assert tip.count() == 1 and tip.is_visible(), "tooltip closed when pointer moved into it"


def test_snippet_node_tooltip(weave, page_as, api):
    """Hover a snippet node -> NodeTooltip with creator label/type + timestamp.
    (Seeded snippets are human-authored, so no genParams block is expected.)"""
    page = page_as("uitest-clement", weave)
    thread = get_thread(api, weave)
    root = thread["nodes"][0]
    assert root["content"]["type"] == "snippet" and root["creator"]["type"] == "human"

    span = page.locator(".doc [data-node-id]").first
    span.hover(position={"x": 25, "y": 8})  # within the first line of the snippet
    page.wait_for_timeout(TIP_HOVER_MS)
    tip = page.locator("[role=tooltip]")
    assert tip.count() == 1, "node tooltip did not appear over the snippet node"
    text = tip.inner_text()
    assert root["creator"]["label"] in text  # creator label ("uitest-clement")
    assert "(human)" in text  # creator type
    assert "created" in text  # timestamp line
    assert root["id"][:8] in text  # node id stamp


# ---------------------------------------------------------------- counterfactual


def test_counterfactual_click_branches_and_moves_cursor(weave, page_as, api):
    """Click a NON-chosen alternative in the token tooltip -> the node is split at
    that token, a new Tokens sibling starting with the alternative appears (model
    attribution preserved), and my cursor moves to it."""
    page = page_as("uitest-clement", weave)
    errors = collect_pageerrors(page)
    thread = get_thread(api, weave)
    _, tok_node = tokens_node_of_thread(thread)
    before = get_weave(api, weave)
    tok_index = 5
    n_tokens_before = len(tok_node["content"]["tokens"])
    assert n_tokens_before > tok_index

    tip = hover_token(page, tok_index)
    alt_btn = tip.locator("button.alt:not(:disabled)").first
    alt_text = json.loads(alt_btn.locator(".alt-text").inner_text())  # debug-quoted
    chosen_text = tok_node["content"]["tokens"][tok_index]["text"]
    assert alt_text != chosen_text, "picked the sampled token by mistake (test bug)"
    alt_btn.click()

    # poll: split + new sibling must land server-side
    def new_nodes():
        now = get_weave(api, weave)
        fresh = set(now["nodes"]) - set(before["nodes"])
        return (now, fresh) if len(fresh) >= 2 else None  # tail + alt branch

    result = wait_until(page, new_nodes)
    assert result, (
        "counterfactual click produced no new nodes "
        f"(expected split tail + alternative branch); page errors: {errors}"
    )
    after, fresh = result

    # original node was split at the token boundary: head keeps the id + prefix
    head = after["nodes"][tok_node["id"]]
    assert head["content"]["type"] == "tokens"
    assert len(head["content"]["tokens"]) == tok_index, "head not truncated at the clicked token"

    # the split tail continues the original content under the head
    tails = [
        after["nodes"][c]
        for c in head["children"]
        if c in fresh and node_text(after["nodes"][c]).startswith(chosen_text)
    ]
    assert len(tails) == 1, f"expected exactly one split tail under head, got {len(tails)}"
    assert node_text(head) + node_text(tails[0]) == node_text(tok_node)

    # the alternative branch: sibling of the tail, first token = clicked alt,
    # model attribution preserved
    alts = [
        after["nodes"][c]
        for c in head["children"]
        if c in fresh
        and after["nodes"][c]["content"]["type"] == "tokens"
        and after["nodes"][c]["content"]["tokens"][0]["text"] == alt_text
    ]
    assert len(alts) == 1, f"alternative-branch node not found under head ({alt_text!r})"
    alt_node = alts[0]
    assert alt_node["creator"]["type"] == "model", "attribution not preserved on the branch"
    assert alt_node["metadata"].get("counterfactual_of") == tok_node["id"]
    assert alt_node["metadata"].get("token_index") == tok_index

    # my cursor followed the branch
    cur = get_cursor(api, weave)
    assert cur["node_id"] == alt_node["id"], (
        f"cursor did not move to the alternative branch (at {cur['node_id'][:8]})"
    )
    assert errors == [], f"uncaught page errors during counterfactual click: {errors}"


# ---------------------------------------------------------------- caret + generate


def test_single_click_shows_caret_without_moving_cursor(weave, page_as, api):
    """Single click in the text -> caret bar (generate-here affordance) appears;
    the API cursor must NOT move."""
    page = page_as("uitest-clement", weave)
    cur_before = get_cursor(api, weave)
    thread = get_thread(api, weave)
    _, tok_node = tokens_node_of_thread(thread)

    page.locator(".doc .token").nth(4).click()
    page.wait_for_timeout(400)
    bar = page.locator(".caret-bar")
    assert bar.count() == 1 and bar.is_visible(), "caret bar did not appear after click"
    assert tok_node["id"][:6] in bar.inner_text(), "caret bar reports the wrong node"
    assert bar.locator("button", has_text="generate here").is_visible()

    page.wait_for_timeout(WS_SETTLE_MS)  # give any wrongful PUT time to land
    cur_after = get_cursor(api, weave)
    assert cur_after["node_id"] == cur_before["node_id"], "single click moved the cursor"
    assert cur_after["updated"] == cur_before["updated"], "single click touched the cursor"


def test_double_click_selects_word_not_moves_cursor(weave, page_as, api):
    """The doc is now a free-form contenteditable: double-click does NATIVE word
    selection (drop of the old double-click-moves-cursor gesture). The API cursor
    must NOT move; the tree "move my cursor here" gesture handles that now."""
    page = page_as("uitest-clement", weave)
    cur_before = get_cursor(api, weave)
    thread = get_thread(api, weave)
    root = thread["nodes"][0]
    assert cur_before["node_id"] != root["id"]  # seeded cursor is deeper

    page.locator(".doc [data-node-id]").first.dblclick(position={"x": 25, "y": 8})
    page.wait_for_timeout(WS_SETTLE_MS)  # give any wrongful PUT time to land
    cur_after = get_cursor(api, weave)
    assert cur_after["node_id"] == cur_before["node_id"], "double-click moved the cursor"
    assert cur_after["updated"] == cur_before["updated"], "double-click touched the cursor"
    # native word selection happened instead
    sel = page.evaluate("() => (document.getSelection()?.toString() ?? '')")
    assert sel.strip() != "", "double-click did not produce a native word selection"


def test_generate_at_caret_splits_and_generates(weave, page_as, api):
    """Click mid-token-node, then 'generate here' -> the node is split at the
    caret's token boundary, new children are generated on the head, and my
    cursor moves to a generated node."""
    page = page_as("uitest-clement", weave)
    errors = collect_pageerrors(page)
    thread = get_thread(api, weave)
    _, tok_node = tokens_node_of_thread(thread)
    before = get_weave(api, weave)
    n_tokens_before = len(tok_node["content"]["tokens"])
    original_text = node_text(tok_node)

    page.locator(".doc .token").nth(4).click()  # caret mid-node (4 full tokens before)
    page.wait_for_timeout(400)
    bar = page.locator(".caret-bar")
    assert bar.count() == 1, "caret bar missing"
    bar.locator("button", has_text="generate here").click()

    # poll: split tail + >=1 generated node (default preset generates n>=1)
    def settled():
        now = get_weave(api, weave)
        fresh = set(now["nodes"]) - set(before["nodes"])
        head_now = now["nodes"][tok_node["id"]]
        if len(fresh) >= 2 and len(head_now["content"]["tokens"]) < n_tokens_before:
            return now, fresh
        return None

    result = wait_until(page, settled)
    assert result, f"generate-at-caret produced no split+generation; page errors: {errors}"
    after, fresh = result

    head = after["nodes"][tok_node["id"]]
    n_head = len(head["content"]["tokens"])
    assert 0 < n_head < n_tokens_before, "split did not truncate the head mid-node"

    children = [after["nodes"][c] for c in head["children"]]
    # the split tail completes the original text
    tails = [c for c in children if node_text(head) + node_text(c) == original_text]
    assert len(tails) >= 1, "split tail not found under the head"
    # at least one freshly generated model child
    gen_children = [
        c for c in children if c["id"] in fresh and c["creator"]["type"] == "model"
        and c["id"] not in {t["id"] for t in tails}
    ]
    assert len(gen_children) >= 1, "no generated children on the head"

    # cursor followed the generation onto one of the new children of the head
    cur = get_cursor(api, weave)
    assert cur["node_id"] in {c["id"] for c in gen_children}, (
        "cursor did not follow the generate-at-caret result"
    )
    assert errors == [], f"uncaught page errors during generate-at-caret: {errors}"


# ---------------------------------------------------------------- typing appends


def test_typing_at_doc_end_appends_and_follows_cursor(weave, page_as, api):
    """The legacy composer is GONE: typing at the end of the doc IS the append
    path. A new human node lands at my cursor and my cursor follows it."""
    page = page_as("uitest-clement", weave)
    assert page.locator(".composer").count() == 0, "legacy append composer still rendered"
    cur_before = get_cursor(api, weave)
    before = get_weave(api, weave)
    marker = " HELLO-FROM-TYPING"

    type_at_end(page, marker)

    def appended():
        now = get_weave(api, weave)
        fresh = [n for nid, n in now["nodes"].items() if nid not in before["nodes"]]
        return fresh[0] if len(fresh) == 1 else None

    new_node = wait_until(page, appended, deadline_s=5)
    assert new_node, "typing at the doc end did not create a node"
    assert new_node["content"] == {"type": "snippet", "text": marker}
    assert new_node["creator"]["type"] == "human"
    assert new_node["creator"]["label"] == "uitest-clement"
    assert new_node["parents"] == [cur_before["node_id"]], "not appended at my cursor"
    assert get_cursor(api, weave)["node_id"] == new_node["id"], "cursor did not follow"

    # doc rendered it exactly once after the WS refetch
    assert wait_until(
        page, lambda: (page.locator(".doc").text_content() or "").count(marker) == 1,
        deadline_s=4,
    ), "appended text did not appear exactly once in the doc"


# ---------------------------------------------------------------- blank weave


def test_blank_weave_placeholder_and_focus(blank_weave, page_as, api):
    """An empty weave renders an editable empty doc: CSS-only placeholder (never
    in the buffer), no generate-at-caret bar, and clicking anywhere in the pane
    focuses the contenteditable."""
    page = page_as("uitest-clement", blank_weave)
    doc = page.locator(".side-pane .doc")
    assert doc.count() == 1, "empty weave did not render the editable doc"
    assert (doc.text_content() or "") == "", "placeholder leaked into the buffer text"
    hint = doc.evaluate("el => getComputedStyle(el, '::before').content")
    assert "start typing" in hint, f"placeholder hint not shown: {hint!r}"
    assert page.locator(".caret-bar").count() == 0, "caret bar shown on an empty doc"
    assert page.locator(".composer").count() == 0, "legacy composer on the blank weave"

    # click on the scroller padding (well below the one-line doc) -> doc focused
    sc = page.locator(".side-pane .scroller")
    box = sc.bounding_box()
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] - 30)
    page.wait_for_timeout(200)
    assert page.evaluate(
        "() => document.activeElement?.classList?.contains('doc') ?? false"
    ), "clicking the empty pane did not focus the contenteditable"


# ---------------------------------------------------------------- context menu


def test_right_click_opens_context_menu(weave, page_as, api):
    """Right-click a node's text -> the global context menu opens on that node."""
    page = page_as("uitest-clement", weave)
    page.locator(".doc [data-node-id]").first.click(
        button="right", position={"x": 25, "y": 8}
    )
    page.wait_for_timeout(300)
    menu = page.locator(".menu[role=menu]")
    assert menu.count() == 1 and menu.is_visible(), "context menu did not open"
    assert menu.locator("button", has_text="generate here").is_visible()
    assert menu.locator("button", has_text="move my cursor here").is_visible()
    # Escape closes it
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    assert menu.count() == 0, "Escape did not close the context menu"


# ---------------------------------------------------------------- auto-scroll


def test_autoscroll_suppressed_while_pointer_inside(weave, page_as, api):
    """The pane auto-follows the growing thread tail -- but never while the
    reader's pointer is inside the pane (the sacred rule)."""
    page = page_as("uitest-clement", weave)

    def append(text, parent):
        r = api.post(
            f"/weaves/{weave}/nodes",
            json={
                "text": text,
                "parent_id": parent,
                "creator": {"type": "human", "label": "uitest-clement"},
                "move_cursor": "uitest-clement",
            },
        )
        r.raise_for_status()
        return r.json()

    # grow my thread until the scroller overflows, pointer OUTSIDE the pane:
    # auto-scroll should pin to the bottom
    page.mouse.move(500, 400)  # over the center canvas
    cur = get_cursor(api, weave)["node_id"]
    for i in range(6):
        cur = append(f"paragraph {i}. " + "lorem ipsum dolor sit amet " * 20 + "\n\n", cur)["id"]
    sc = page.locator(".side-pane .scroller")

    def at_bottom():
        m = sc.evaluate("el => ({top: el.scrollTop, h: el.scrollHeight, ch: el.clientHeight})")
        return m if m["h"] > m["ch"] and abs(m["top"] + m["ch"] - m["h"]) < 3 else None

    assert wait_until(page, at_bottom, deadline_s=5), (
        "pane did not auto-scroll to the tail while pointer was outside"
    )

    # now put the pointer INSIDE the pane, scroll up, and append again:
    # scrollTop must not move
    box = sc.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.wait_for_timeout(200)
    sc.evaluate("el => { el.scrollTop = 80 }")
    page.wait_for_timeout(200)
    assert sc.evaluate("el => el.scrollTop") == 80

    marker = "SUPPRESSION-MARKER node"
    append(marker, cur)
    assert wait_until(
        page, lambda: marker in page.locator(".doc").text_content(), deadline_s=5
    ), "appended node never reached the doc"
    page.wait_for_timeout(800)  # any deferred scroll would land within this window
    assert sc.evaluate("el => el.scrollTop") == 80, (
        "pane auto-scrolled while the pointer was inside it"
    )
