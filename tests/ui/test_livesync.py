"""Adversarial UI tests for multi-client live sync — coloom's core differentiator.

Two real browser pages on the SAME weave (uitest-clement + uitest-claude, separate contexts /
separate WS connections). Every cross-client effect is verified through BOTH the
other client's DOM and the REST API.

Expected behavior per the component sources (state.svelte.ts WS handling,
Editor.svelte header/footer, Canvas.svelte + NodeCard.svelte pills,
TextPane.svelte thread, ActivityFeed.svelte, ContextMenu.svelte summon,
InfoPane.svelte) and docs/ui-specs/shared-state.md (§2 coloom divergence:
per-cursor deletion fallback must be enforced server-side).

Run: cd /home/c.dumas/projects2/coloom && uv run pytest tests/ui/test_livesync.py -q
"""

import os
import re
import time

import httpx
import pytest
from playwright.sync_api import expect

API = os.environ.get("COLOOM_API", "http://localhost:4444")

# ---------------------------------------------------------------- helpers


def node_text(node: dict) -> str:
    c = node["content"]
    if c["type"] == "snippet":
        return c["text"]
    return "".join(t["text"] for t in c["tokens"])


def get_weave(api: httpx.Client, wid: str) -> dict:
    r = api.get(f"/weaves/{wid}")
    r.raise_for_status()
    return r.json()


def get_cursors(api: httpx.Client, wid: str) -> dict:
    r = api.get(f"/weaves/{wid}/cursors")
    r.raise_for_status()
    return r.json()


def thread_ids(weave: dict, node_id: str) -> list[str]:
    """Root→node path, mirroring threadPath() in state.svelte.ts."""
    path, cur, seen = [], node_id, set()
    while cur is not None and cur in weave["nodes"] and cur not in seen:
        seen.add(cur)
        path.append(cur)
        parents = weave["nodes"][cur]["parents"]
        cur = parents[0] if parents else None
    return list(reversed(path))


def thread_text(weave: dict, node_id: str) -> str:
    return "".join(node_text(weave["nodes"][i]) for i in thread_ids(weave, node_id))


def wait_until(predicate, message: str, deadline: float = 6.0, interval: float = 0.15):
    """Poll until truthy. WS round-trips ~100-800ms, fake generations up to ~1.6s."""
    end = time.monotonic() + deadline
    while True:
        result = predicate()
        if result:
            return result
        if time.monotonic() > end:
            raise AssertionError(f"timed out after {deadline}s: {message}")
        time.sleep(interval)


def open_tab(page, label: str) -> None:
    page.locator(".sidebar .tabs").get_by_role("button", name=label, exact=True).click()
    page.wait_for_timeout(150)


def fit_weave(page) -> None:
    """Click the '⛶ weave' fit button so culling hides nothing."""
    page.locator("button[title^='fit whole weave']").click()
    page.wait_for_timeout(200)


def canvas_cards(page):
    return page.locator(".canvas g.card")


def pill_label(name: str) -> str:
    """Mirror NodeCard's pill truncation: names >14 chars middle-truncate
    (7 chars + … + last 6), so prefix-sharing names keep distinct pills."""
    return f"{name[:7]}…{name[-6:]}" if len(name) > 14 else name


def card_with_cursor_pill(page, name: str):
    """The canvas card carrying the named cursor's pill (exact label match)."""
    pill = page.locator("text.pill-label").filter(
        has_text=re.compile(rf"^{re.escape(pill_label(name))}$")
    )
    return canvas_cards(page).filter(has=pill)


def footer_nodes(page) -> str:
    return page.locator("footer span").first.text_content() or ""


# ============================================================ generation sync


def test_generation_in_a_appears_live_in_b(weave, page_as, api):
    """uitest-clement generates via the gen controls; uitest-claude's client (no reload, no
    interaction with the weave) grows the new cards and logs it in the feed."""
    page_a = page_as("uitest-clement", weave)
    page_b = page_as("uitest-claude", weave)

    n0 = len(get_weave(api, weave)["nodes"])

    # B: watch the activity feed; snapshot the canvas at full fit
    open_tab(page_b, "activity")
    fit_weave(page_b)
    expect(canvas_cards(page_b)).to_have_count(n0, timeout=4000)
    feed = page_b.locator(".tab-body")
    weaving_before = feed.locator("li", has_text="is weaving at").count()
    done3_before = feed.locator("li", has_text="done: 3 branches at").count()

    # A: generate at my cursor via the UI (default preset, n=3)
    gen_btn = page_a.locator(".controls button.gen")
    expect(gen_btn).to_be_enabled()
    gen_btn.click()

    # API ground truth: exactly 3 new nodes, children of uitest-clement's cursor node
    cursor_node = get_cursors(api, weave)["uitest-clement"]["node_id"]
    wait_until(
        lambda: len(get_weave(api, weave)["nodes"]) == n0 + 3,
        "generation should add 3 nodes (n=3 default preset)",
        deadline=8.0,
    )
    w = get_weave(api, weave)
    new_ids = [i for i, n in w["nodes"].items() if i not in thread_ids(w, cursor_node)]
    assert all(
        cursor_node in w["nodes"][c]["parents"]
        for c in w["nodes"][cursor_node]["children"][-3:]
    )

    # B DOM, no reload: footer count updates live...
    wait_until(
        lambda: f"{n0 + 3} nodes" in footer_nodes(page_b),
        "B's footer should reflect the 3 new nodes within ~5s",
        deadline=6.0,
    )
    # ...and the canvas grew the new cards
    fit_weave(page_b)
    expect(canvas_cards(page_b)).to_have_count(n0 + 3, timeout=4000)

    # B's activity feed shows the generation, attributed to uitest-clement
    weaving = feed.locator("li", has_text="is weaving at")
    expect(weaving).to_have_count(weaving_before + 1, timeout=6000)
    expect(weaving.first.locator(".actor")).to_have_text("uitest-clement")
    done = feed.locator("li", has_text="done: 3 branches at")
    expect(done).to_have_count(done3_before + 1, timeout=8000)
    expect(done.first.locator(".actor")).to_have_text("uitest-clement")


# ============================================================ the look-here gesture


def test_summon_via_context_menu_redirects_other_text_pane(weave, page_as, api):
    """uitest-claude right-clicks 'Chapter 2' and summons uitest-clement there; uitest-clement's
    text pane thread must change to the new position without any action by A."""
    page_a = page_as("uitest-clement", weave)
    page_b = page_as("uitest-claude", weave)

    w = get_weave(api, weave)
    ch2 = w["roots"][1]
    assert "Chapter 2" in node_text(w["nodes"][ch2])

    # A's thread before: root→kids[0], starting with the loom opening
    doc_a = page_a.locator(".side-pane .doc")
    expect(doc_a).to_contain_text("The loom hummed")
    expect(doc_a).not_to_contain_text("Chapter 2")

    # B: right-click the Chapter 2 card, pick "summon uitest-clement here"
    fit_weave(page_b)
    card = canvas_cards(page_b).filter(has_text="Chapter 2. A completely different")
    expect(card).to_have_count(1)
    # right-click the card's text body (it paints over rect.bg); the
    # contextmenu event bubbles to the g.card handler
    card.locator("div.text").click(button="right")
    summon = page_b.locator(".menu").get_by_role(
        "menuitem", name="summon uitest-clement here"
    )
    expect(summon).to_be_visible()
    summon.click()

    # API: the gesture landed, with attribution
    cur = wait_until(
        lambda: (lambda c: c if c["node_id"] == ch2 else None)(
            get_cursors(api, weave)["uitest-clement"]
        ),
        "uitest-clement's cursor should land on the Chapter 2 root",
    )
    assert cur["moved_by"] == "uitest-claude", "moved_by must record who summoned"

    # A's text pane follows within ~5s: the thread is now the Chapter 2 root
    expect(doc_a).to_contain_text("Chapter 2. A completely different opening:", timeout=6000)
    expect(doc_a).not_to_contain_text("The loom hummed")


# ============================================================ presence


def test_inflight_generation_shows_weaver_name_in_other_header(weave, page_as, api):
    """While uitest-clement's generation is in flight, uitest-claude's header shows the
    '⟳ … weaving: uitest-clement' presence indicator; it disappears when done.
    Timing-sensitive by design (gen window is ~0.4-1.6s on the fake backend),
    hence the eager expect() polling right after the click."""
    page_a = page_as("uitest-clement", weave)
    page_b = page_as("uitest-claude", weave)

    indicator_b = page_b.locator("header .inflight")
    expect(indicator_b).to_have_count(0)

    # A: activate the 'fake-slow' preset chip (max_tokens=48) and generate
    page_a.get_by_test_id("gc-preset-fake-slow").click()
    page_a.locator(".controls button.gen").click()

    # B: indicator appears while in flight, names the weaver
    expect(indicator_b).to_contain_text("uitest-clement", timeout=4000)
    expect(indicator_b).to_contain_text("weaving")

    # ...and is gone once the generation finishes (gen_finished event)
    expect(indicator_b).to_have_count(0, timeout=10000)
    # sanity: the generation really happened
    w = get_weave(api, weave)
    assert len(w["nodes"]) > 16


# ============================================================ cursor visibility


def test_both_cursors_visible_in_both_clients(weave, page_as, api):
    """Both named cursors render as pills on the canvas and dots in the tree,
    in BOTH clients."""
    cursors = get_cursors(api, weave)
    assert set(cursors) == {"uitest-clement", "uitest-claude"}, "seed should have both cursors"

    for who in ("uitest-clement", "uitest-claude"):
        page = page_as(who, weave)
        fit_weave(page)

        # canvas pills: one per cursor, exact labels
        labels = page.locator(".canvas text.pill-label")
        expect(labels).to_have_count(2, timeout=4000)
        expected = sorted(pill_label(n) for n in ("uitest-claude", "uitest-clement"))
        assert sorted(labels.all_text_contents()) == expected, (
            f"{who}'s canvas should show both cursor pills"
        )

        # tree dots (default sidebar tab is 'tree'): one dot per cursor
        for name in ("uitest-clement", "uitest-claude"):
            dot = page.locator(f".sidebar .cdot[title='{name}']")
            expect(dot).to_have_count(1, timeout=4000)

        # pills sit on the right nodes: the card carrying each pill exists
        for name, cur in cursors.items():
            card = card_with_cursor_pill(page, name)
            expect(card).to_have_count(1)


# ============================================================ delete under a cursor


def test_delete_node_under_other_cursor_relocates_it(weave, page_as, api):
    """uitest-clement deletes the node uitest-claude's cursor sits on. The server must
    relocate uitest-claude's cursor (refuge = surviving ancestor) and uitest-claude's text
    pane must follow — no crash, no stale thread (spec shared-state.md §2)."""
    page_a = page_as("uitest-clement", weave)
    page_b = page_as("uitest-claude", weave)

    w = get_weave(api, weave)
    doomed = get_cursors(api, weave)["uitest-claude"]["node_id"]
    doomed_node = w["nodes"][doomed]
    assert doomed_node["children"] == [], "seed: uitest-claude's cursor is a leaf"
    parent = doomed_node["parents"][0]
    doomed_text = node_text(doomed_node)
    n0 = len(w["nodes"])

    # B's thread currently ends in the doomed node
    doc_b = page_b.locator(".side-pane .doc")
    expect(doc_b).to_contain_text(doomed_text[:40])

    # A: find the card carrying uitest-claude's pill and delete it via the context menu
    fit_weave(page_a)
    card = card_with_cursor_pill(page_a, "uitest-claude")
    expect(card).to_have_count(1)
    card.locator("div.text").click(button="right")
    delete = page_a.locator(".menu").get_by_role("menuitem", name="delete node")
    expect(delete).to_be_visible()
    delete.click()

    # API: node gone, uitest-claude's cursor relocated to a surviving ancestor
    wait_until(
        lambda: doomed not in get_weave(api, weave)["nodes"],
        "the node should be deleted",
    )
    cur = wait_until(
        lambda: (lambda c: c if c["node_id"] != doomed else None)(
            get_cursors(api, weave)["uitest-claude"]
        ),
        "uitest-claude's cursor must be relocated off the deleted node",
    )
    assert cur["node_id"] == parent, (
        f"cursor should take refuge on the parent {parent[:8]}, got {cur['node_id'][:8]}"
    )

    # B's UI follows: thread re-derived for the new cursor, exactly root→parent
    w_after = get_weave(api, weave)
    expected = thread_text(w_after, parent)
    wait_until(
        lambda: (doc_b.text_content() or "") == expected,
        "B's text pane should show the relocated thread root→parent",
    )
    assert doomed_text not in (doc_b.text_content() or "")
    # no crash: B still live and consistent
    expect(page_b.locator("header h1")).to_be_visible()
    assert f"{n0 - 1} nodes" in footer_nodes(page_b)


# ============================================================ weave info sync


def test_title_edit_in_a_updates_b_header(weave, page_as, api):
    page_a = page_as("uitest-clement", weave)
    page_b = page_as("uitest-claude", weave)

    expect(page_b.locator("header h1")).to_have_text("ui-test weave")

    open_tab(page_a, "info")
    title_input = page_a.locator(".tab-body .field input")
    expect(title_input).to_have_value("ui-test weave")
    title_input.fill("renamed live by uitest-clement")
    title_input.press("Enter")  # blur → PATCH

    wait_until(
        lambda: get_weave(api, weave)["title"] == "renamed live by uitest-clement",
        "PATCH should persist the new title",
    )
    # B's header follows over WS within ~5s, no reload
    expect(page_b.locator("header h1")).to_have_text(
        "renamed live by uitest-clement", timeout=6000
    )


# ============================================================ WS reconnect


def test_ws_reconnect_resyncs_missed_changes(weave, page_as, api):
    """Kill B's network (set_offline), mutate the weave, restore: B must
    reconnect and resync (onopen → refetch in state.svelte.ts). Skips
    gracefully if Chromium's offline emulation doesn't affect the open WS."""
    page_b = page_as("uitest-claude", weave)
    root1 = get_weave(api, weave)["roots"][1]
    n0 = len(get_weave(api, weave)["nodes"])
    expect(page_b.locator("header .conn")).to_have_attribute("data-state", "live")
    assert f"{n0} nodes" in footer_nodes(page_b)

    page_b.context.set_offline(True)
    page_b.wait_for_timeout(500)

    # mutate while B is dark: 2 generated nodes
    api.post(
        f"/weaves/{weave}/gen",
        json={"node_id": root1, "cursor": "uitest-clement", "params": {"n": 2}},
    ).raise_for_status()
    wait_until(
        lambda: len(get_weave(api, weave)["nodes"]) == n0 + 2,
        "API-side generation should land while B is offline",
        deadline=8.0,
    )
    page_b.wait_for_timeout(1500)  # give the WS time to notice the outage

    conn_state = page_b.locator("header .conn").get_attribute("data-state")
    saw_while_offline = f"{n0 + 2} nodes" in footer_nodes(page_b)
    page_b.context.set_offline(False)

    if conn_state == "live" and saw_while_offline:
        pytest.skip(
            "set_offline did not kill the WS in this Chromium build — events "
            "flowed through; reconnect path not exercisable this way"
        )

    # the client noticed (reconnecting) — after restore it must resync fully.
    # reconnect backoff starts at 500ms and doubles, hence the long deadline.
    wait_until(
        lambda: f"{n0 + 2} nodes" in footer_nodes(page_b),
        "B should resync the missed nodes after WS reconnect",
        deadline=15.0,
        interval=0.3,
    )
    expect(page_b.locator("header .conn")).to_have_attribute(
        "data-state", "live", timeout=15000
    )
