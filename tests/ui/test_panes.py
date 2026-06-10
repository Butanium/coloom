"""Adversarial UI tests for the sidebar panes: Bookmarks / Activity / Info
(+ the Editor stats footer).

Every UI-driven mutation is verified through the REST API, not just the DOM.
Expected behavior per the component sources (BookmarksPane.svelte,
ActivityFeed.svelte, InfoPane.svelte, Editor.svelte) and docs/ui-specs/lists.md.

Run: cd /home/c.dumas/projects2/coloom && uv run pytest tests/ui/test_panes.py -q
"""

import os
import re
import threading
import time

import httpx
from playwright.sync_api import expect

API = os.environ.get("COLOOM_API", "http://localhost:4444")

# ---------------------------------------------------------------- helpers


def node_text(node: dict) -> str:
    c = node["content"]
    if c["type"] == "snippet":
        return c["text"]
    return "".join(t["text"] for t in c["tokens"])


def bookmark_snippet(node: dict) -> str:
    """What BookmarksPane.snippet() renders for a node."""
    text = node_text(node).strip()
    return "(no text)" if text == "" else text


def norm_ws(s: str) -> str:
    return " ".join(s.split())


def get_weave(api: httpx.Client, wid: str) -> dict:
    r = api.get(f"/weaves/{wid}")
    r.raise_for_status()
    return r.json()


def get_cursors(api: httpx.Client, wid: str) -> dict:
    r = api.get(f"/weaves/{wid}/cursors")
    r.raise_for_status()
    return r.json()


def open_tab(page, label: str) -> None:
    """Click a sidebar tab ('tree'|'children'|'marks'|'activity'|'info')."""
    page.locator(".sidebar .tabs").get_by_role("button", name=label, exact=True).click()
    page.wait_for_timeout(150)


def canvas_transform(page) -> str:
    tf = page.locator(".canvas svg > g").first.get_attribute("transform")
    assert tf is not None, "canvas <g> has no transform attribute"
    return tf


def wait_until(predicate, message: str, deadline: float = 6.0, interval: float = 0.15):
    """Poll `predicate` until truthy; raise with `message` on timeout.

    WS round-trips (event -> push -> client refetch) take ~100-800ms; generations
    on the fake backend up to ~1.5s, hence the generous default deadline.
    """
    end = time.monotonic() + deadline
    while True:
        result = predicate()
        if result:
            return result
        if time.monotonic() > end:
            raise AssertionError(f"timed out after {deadline}s: {message}")
        time.sleep(interval)


# ============================================================ bookmarks tab


def test_bookmarks_tab_lists_seeded_bookmarks(weave, page_as, api):
    w = get_weave(api, weave)
    assert len(w["bookmarks"]) == 2, "seed should have exactly 2 bookmarks"

    page = page_as("uitest-clement", weave)
    open_tab(page, "marks")
    rows = page.locator(".tab-body li")
    expect(rows).to_have_count(2, timeout=4000)

    # rows render in bookmark order, with the node's (trimmed) text
    for i, bid in enumerate(w["bookmarks"]):
        expected = bookmark_snippet(w["nodes"][bid])
        actual = rows.nth(i).locator(".text").text_content()
        assert actual == expected, (
            f"bookmark row {i}: expected {expected!r}, got {actual!r}"
        )


def test_bookmark_click_moves_my_cursor_and_centers_canvas(weave, page_as, api):
    w = get_weave(api, weave)
    start = get_cursors(api, weave)["uitest-clement"]["node_id"]
    # pick the bookmark my cursor is NOT already on, so the move is observable
    target_idx, target = next(
        (i, b) for i, b in enumerate(w["bookmarks"]) if b != start
    )

    page = page_as("uitest-clement", weave)
    open_tab(page, "marks")
    tf_before = canvas_transform(page)

    row = page.locator(".tab-body li").nth(target_idx).locator("button.row")
    expect(row).to_be_visible()
    row.click()

    # API: uitest-clement's cursor moved to the bookmarked node, moved_by=uitest-clement
    cur = wait_until(
        lambda: (lambda c: c if c["node_id"] == target else None)(
            get_cursors(api, weave)["uitest-clement"]
        ),
        f"uitest-clement's cursor should move to bookmark {target[:8]}",
    )
    assert cur["moved_by"] == "uitest-clement"

    # DOM: the canvas centered on the node (view transform changed)
    wait_until(
        lambda: canvas_transform(page) != tf_before,
        "canvas transform should change (center-node on bookmark click)",
    )


def test_unbookmark_button_removes_bookmark_and_row(weave, page_as, api):
    w = get_weave(api, weave)
    first = w["bookmarks"][0]

    page = page_as("uitest-clement", weave)
    open_tab(page, "marks")
    cursors_before = get_cursors(api, weave)

    row = page.locator(".tab-body li").first
    # the ✕ is revealed on hover — interact like a human: hover, see it, click it
    row.hover()
    unmark = row.get_by_label("remove bookmark")
    expect(unmark).to_be_visible()
    unmark.click()

    # API: node un-bookmarked and gone from the weave bookmark list
    wait_until(
        lambda: api.get(f"/weaves/{weave}/nodes/{first}").json()["bookmarked"] is False,
        "node.bookmarked should become false after clicking ✕",
    )
    assert first not in get_weave(api, weave)["bookmarks"]

    # DOM: the row disappears (1 bookmark left)
    expect(page.locator(".tab-body li")).to_have_count(1, timeout=4000)

    # the ✕ must NOT also jump my cursor (it sits next to the jump button)
    assert get_cursors(api, weave)["uitest-clement"]["node_id"] == (
        cursors_before["uitest-clement"]["node_id"]
    ), "clicking the un-bookmark button must not move my cursor"


# ============================================================ activity tab


def test_activity_shows_seeded_history(weave, page_as, api):
    page = page_as("uitest-clement", weave)
    open_tab(page, "activity")
    feed = page.locator(".tab-body")

    # the seed runs 5 generations -> 5 gen_started + 5 gen_finished entries
    expect(feed.locator("li", has_text="is weaving at")).to_have_count(5, timeout=4000)
    expect(
        feed.locator("li").filter(has_text=re.compile(r"done: \d+ branch"))
    ).to_have_count(5)
    # two roots were added (the loom opening + "Chapter 2")
    expect(feed.locator("li", has_text="added a root")).to_have_count(2)
    # branches under parents
    assert feed.locator("li", has_text="added a branch under").count() >= 1
    # two bookmarks were set
    expect(feed.locator("li", has_text="bookmarked")).to_have_count(2)
    # one split
    expect(feed.locator("li", has_text="split")).to_have_count(1)
    # weave creation is the oldest entry
    expect(feed.locator("li", has_text="weave created")).to_have_count(1)


def test_activity_feed_grows_live_with_claude_attribution(weave, page_as, api):
    page = page_as("uitest-clement", weave)
    open_tab(page, "activity")
    feed = page.locator(".tab-body")

    weaving = feed.locator("li", has_text="is weaving at")
    n_before = weaving.count()
    done_one = feed.locator("li", has_text="done: 1 branch at")  # n=1 unique to this test
    expect(done_one).to_have_count(0)

    # uitest-claude generates (via API, at uitest-claude's own cursor) while uitest-clement watches
    errors: list[Exception] = []

    def gen_as_claude():
        try:
            with httpx.Client(base_url=API, timeout=30) as c:
                c.post(
                    f"/weaves/{weave}/gen",
                    json={"cursor": "uitest-claude", "params": {"n": 1}},
                ).raise_for_status()
        except Exception as e:  # surfaced below; thread must not die silently
            errors.append(e)

    t = threading.Thread(target=gen_as_claude)
    t.start()
    try:
        # 'uitest-claude is weaving at …' appears live (no reload), attributed to uitest-claude
        expect(weaving).to_have_count(n_before + 1, timeout=6000)
        expect(weaving.first.locator(".actor")).to_have_text("uitest-claude")
        # …then 'uitest-claude done: 1 branch at …'
        expect(done_one).to_have_count(1, timeout=8000)
        expect(done_one.first.locator(".actor")).to_have_text("uitest-claude")
    finally:
        t.join(timeout=15)
    assert not errors, f"generation request failed: {errors}"


def test_activity_look_here_phrasing(weave, page_as, api):
    w = get_weave(api, weave)
    target = w["roots"][1]  # the "Chapter 2" root

    page = page_as("uitest-clement", weave)
    open_tab(page, "activity")

    # uitest-claude moves CLEMENT's cursor — the "look here" gesture
    api.put(
        f"/weaves/{weave}/cursors/uitest-clement",
        json={"node_id": target, "moved_by": "uitest-claude"},
    ).raise_for_status()

    entry = page.locator(".tab-body li.lookhere", has_text="moved uitest-clement's cursor to")
    expect(entry).to_have_count(1, timeout=6000)
    expect(entry.first.locator(".actor")).to_have_text("uitest-claude")
    # sanity through the API: the gesture really landed
    cur = get_cursors(api, weave)["uitest-clement"]
    assert cur["node_id"] == target and cur["moved_by"] == "uitest-claude"


def test_activity_hides_plain_cursor_moves_until_a_real_event(weave, page_as, api):
    """Plain navigation (self cursor moves) is feed chatter: an entry shows only
    once a real (non-cursor) event follows it. Summons always show (covered by
    test_activity_look_here_phrasing)."""
    w = get_weave(api, weave)
    target = w["roots"][1]

    page = page_as("uitest-clement", weave)
    open_tab(page, "activity")
    feed = page.locator(".tab-body")
    moves = feed.locator("li", has_text="moved their cursor to").filter(
        has_text="uitest-claude"
    )
    n_before = moves.count()

    # uitest-claude navigates (moves their own cursor) → NO new feed entry
    api.put(
        f"/weaves/{weave}/cursors/uitest-claude",
        json={"node_id": target, "moved_by": "uitest-claude"},
    ).raise_for_status()
    page.wait_for_timeout(1200)  # give the WS event time to (not) render
    assert moves.count() == n_before, "a plain cursor move must not show on its own"

    # a real event lands right after → the preceding move becomes visible
    api.put(
        f"/weaves/{weave}/nodes/{target}/bookmark", json={"bookmarked": True}
    ).raise_for_status()
    expect(moves).to_have_count(n_before + 1, timeout=6000)


def test_activity_hover_highlights_node_and_click_moves_no_cursor(weave, page_as, api):
    page = page_as("uitest-clement", weave)
    open_tab(page, "activity")

    # newest 'added a root' entry = the "Chapter 2" root (seeded last of the two)
    entry = page.locator(".tab-body li", has_text="added a root").first
    expect(entry).to_be_visible()

    # click: centers the view but must NOT move any cursor (read-only lens)
    cursors_before = get_cursors(api, weave)
    tf_before = canvas_transform(page)
    entry.locator("button.entry").click()
    wait_until(
        lambda: canvas_transform(page) != tf_before,
        "canvas should center on the entry's node after click",
    )
    page.wait_for_timeout(800)  # allow any (incorrect) cursor mutation to land
    assert get_cursors(api, weave) == cursors_before, (
        "clicking an activity entry must not move any cursor"
    )

    # hover -> shared hoveredNodeId -> the (now on-screen, centered) canvas card
    # highlights. The canvas culls off-viewport cards, hence centering first.
    entry.hover()
    expect(entry).to_have_class(re.compile(r"\bhovered\b"))
    hovered_cards = page.locator(".canvas g.card.hovered")
    expect(hovered_cards).to_have_count(1, timeout=3000)
    assert "Chapter 2" in (hovered_cards.first.text_content() or ""), (
        "the highlighted canvas card should be the entry's node"
    )


# ============================================================ info tab


def test_info_title_edit_persists(weave, page_as, api):
    page = page_as("uitest-clement", weave)
    open_tab(page, "info")

    title_input = page.locator(".tab-body .field input")
    expect(title_input).to_have_value("ui-test weave")
    title_input.fill("renamed by panes test")
    title_input.press("Enter")  # blur -> PATCH

    wait_until(
        lambda: get_weave(api, weave)["title"] == "renamed by panes test",
        "PATCH should persist the new title",
    )
    # the header h1 follows via the WS-driven refetch
    expect(page.locator("header h1")).to_have_text("renamed by panes test", timeout=6000)


def test_info_description_edit_persists(weave, page_as, api):
    page = page_as("uitest-clement", weave)
    open_tab(page, "info")

    desc = page.locator(".tab-body .field textarea")
    expect(desc).to_have_value("seeded for UI dev/testing")
    desc.fill("rewritten description, via the info pane")
    desc.press("Control+Enter")  # blur -> PATCH

    wait_until(
        lambda: get_weave(api, weave)["description"]
        == "rewritten description, via the info pane",
        "PATCH should persist the new description",
    )


def test_info_metadata_add_and_delete(weave, page_as, api):
    # seed weaves start in the testing folder; that's their only metadata
    base = get_weave(api, weave)["metadata"]
    assert base == {"folder": "testing"}, "unexpected seed-weave metadata"

    page = page_as("uitest-clement", weave)
    open_tab(page, "info")

    page.get_by_placeholder("new key").fill("mood")
    # the folder row also has a "value" input now — scope to the add row
    page.locator(".meta-row.add").get_by_placeholder("value").fill("midnight loom")
    add_btn = page.locator(".tab-body button[title='add entry']")
    expect(add_btn).to_be_enabled()
    add_btn.click()

    wait_until(
        lambda: get_weave(api, weave)["metadata"] == {**base, "mood": "midnight loom"},
        "added metadata entry should persist via PATCH",
    )
    # the committed row appears after the pre-existing folder row, drafts reset
    # (CSS [value=] matches attributes, not live input state — go positional)
    row = page.locator(".tab-body .meta-row:not(.add)").nth(1)
    expect(row.locator("input.k")).to_have_value("mood", timeout=4000)
    expect(row.locator("input.v")).to_have_value("midnight loom")
    expect(page.get_by_placeholder("new key")).to_have_value("")

    # delete it — back to just the folder entry. Let the add's WS refetch finish
    # re-rendering rows first, then RE-RESOLVE the row (clicking a stale node
    # mid-re-render can land on the wrong row).
    page.wait_for_timeout(500)
    row = page.locator(".tab-body .meta-row:not(.add)").nth(1)
    expect(row.locator("input.k")).to_have_value("mood")
    row.get_by_label("remove entry").click()
    wait_until(
        lambda: get_weave(api, weave)["metadata"] == base,
        "removed metadata entry should persist via PATCH",
    )
    expect(page.locator(".tab-body .meta-row:not(.add)")).to_have_count(1, timeout=4000)


def test_info_stats_match_api(weave, page_as, api):
    w = get_weave(api, weave)
    nodes = list(w["nodes"].values())
    snippets = sum(1 for n in nodes if n["content"]["type"] == "snippet")
    tokens = len(nodes) - snippets
    by_creator: dict[str, int] = {}
    for n in nodes:
        label = n["creator"].get("label") or "unknown"
        by_creator[label] = by_creator.get(label, 0) + 1

    page = page_as("uitest-clement", weave)
    open_tab(page, "info")
    table = page.locator(".tab-body table")
    expect(table).to_be_visible()

    def row_text(label: str) -> str:
        # inner_text keeps cell separation (text_content glues <td>s together)
        return norm_ws(table.locator("tr", has_text=label).inner_text())

    assert row_text("nodes") == f"nodes {len(nodes)}"
    assert row_text("content") == (
        f"content {snippets} snippet{'' if snippets == 1 else 's'} · {tokens} tokens"
    )
    assert row_text("bookmarked") == f"bookmarked {len(w['bookmarks'])}"
    assert row_text("cursors") == f"cursors {len(w['cursors'])}"

    for label, count in by_creator.items():
        chip = page.locator(".tab-body .creator", has_text=f"{label}: {count}")
        expect(chip).to_have_count(1)


# ============================================================ stats footer


def test_footer_stats_match_api_and_update_after_generation(weave, page_as, api):
    w = get_weave(api, weave)
    nodes = list(w["nodes"].values())
    humans = sum(1 for n in nodes if n["creator"]["type"] == "human")
    models = sum(1 for n in nodes if n["creator"]["type"] == "model")

    page = page_as("uitest-clement", weave)
    footer = page.locator("footer")
    expect(footer.locator("span").first).to_have_text(
        f"{len(nodes)} nodes · {len(w['bookmarks'])} bookmarked · "
        f"{len(w['cursors'])} cursors"
    )
    expect(footer.locator(".attribution")).to_have_text(
        f"{humans} human · {models} model"
    )

    # generate 2 nodes via the API -> footer updates live over WS
    api.post(
        f"/weaves/{weave}/gen",
        json={"node_id": w["roots"][1], "cursor": "uitest-clement", "params": {"n": 2}},
    ).raise_for_status()

    expect(footer.locator("span").first).to_have_text(
        f"{len(nodes) + 2} nodes · {len(w['bookmarks'])} bookmarked · "
        f"{len(w['cursors'])} cursors",
        timeout=8000,
    )
    expect(footer.locator(".attribution")).to_have_text(
        f"{humans} human · {models + 2} model"
    )
    # cross-check the API agrees
    assert len(get_weave(api, weave)["nodes"]) == len(nodes) + 2
