"""ActivityFeed: expandable entries, no horizontal scroll, weave/global scope.

- clicking an entry expands it in place with full details (event type, when,
  payload key/values); clicking again collapses.
- the pane NEVER scrolls horizontally — long content wraps.
- scope toggle: "this weave" (session events, today's behavior) vs
  "all weaves" (a second unfiltered /ws + paged GET /events backfill,
  globalevents.svelte.ts); foreign-weave entries are tagged with their
  weave's title.
"""

import os
import time

import httpx
import pytest
from playwright.sync_api import expect

API = os.environ.get("COLOOM_API", "http://localhost:4444")


def poll(fn, *, timeout=8.0, interval=0.2, desc="condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    pytest.fail(f"timed out ({timeout}s) waiting for {desc}; last={last!r}")


def open_activity(page):
    page.get_by_role("button", name="activity", exact=True).click()
    expect(page.get_by_test_id("activity-pane")).to_be_visible()


def add_node(api, wid, text, parent_id=None):
    r = api.post(
        f"/weaves/{wid}/nodes",
        json={
            "text": text,
            "parent_id": parent_id,
            "creator": {"type": "human", "label": "uitest-activity-actor"},
        },
    )
    r.raise_for_status()
    return r.json()["id"]


def test_entry_click_expands_with_details(page_as, weave, api):
    page = page_as("uitest-clement", weave)
    open_activity(page)

    # a live mutation lands in the feed
    roots = api.get(f"/weaves/{weave}").json()["roots"]
    nid = add_node(api, weave, "activity expansion test branch", roots[0])
    entry = page.locator('[data-testid^="activity-entry-"]').first
    poll(lambda: entry.count() == 1, desc="feed shows the new event")

    details = page.locator('[data-testid^="activity-details-"]')
    expect(details).to_have_count(0)  # collapsed by default
    entry.click()
    expect(details).to_have_count(1)
    # full details: event type, timestamp row, and payload fields incl node_id
    expect(details).to_contain_text("node_added")
    expect(details).to_contain_text("when")
    expect(details).to_contain_text(nid)
    # click again collapses
    entry.click()
    expect(details).to_have_count(0)


def test_no_horizontal_scroll_even_expanded(page_as, weave, api):
    """Long unbroken content (ids, long node text) wraps — the pane must
    never grow a horizontal scrollbar."""
    page = page_as("uitest-clement", weave)
    open_activity(page)

    roots = api.get(f"/weaves/{weave}").json()["roots"]
    add_node(api, weave, "x" * 400, roots[0])  # 400 chars, no break points
    entry = page.locator('[data-testid^="activity-entry-"]').first
    poll(lambda: entry.count() == 1, desc="feed shows the new event")
    entry.click()  # expanded = worst case (full payload shown)
    expect(page.locator('[data-testid^="activity-details-"]')).to_have_count(1)

    overflow = page.evaluate(
        """() => {
          const pane = document.querySelector('[data-testid="activity-pane"]')
          const bad = []
          // the pane itself must not need (or clip) horizontal scrolling
          if (pane.scrollWidth > pane.clientWidth + 1)
            bad.push(['pane', pane.scrollWidth, pane.clientWidth])
          // expanded details must WRAP, not overflow
          const details = pane.querySelector('[data-testid^="activity-details-"]')
          if (details && details.scrollWidth > details.clientWidth + 1)
            bad.push(['details', details.scrollWidth, details.clientWidth])
          // no scrollable child may overflow horizontally either
          for (const el of pane.querySelectorAll('*')) {
            const ox = getComputedStyle(el).overflowX
            if ((ox === 'auto' || ox === 'scroll') && el.scrollWidth > el.clientWidth + 1)
              bad.push([el.className, el.scrollWidth, el.clientWidth])
          }
          return bad
        }"""
    )
    assert overflow == [], f"horizontal overflow in activity pane: {overflow}"


def test_global_scope_shows_foreign_weave_with_tag(page_as, weave, api):
    """'all weaves' surfaces events from OTHER weaves, tagged with the weave
    title; 'this weave' hides them."""
    other = httpx.post(
        f"{API}/weaves", json={"title": "activity-foreign-weave"}, timeout=5
    ).json()["id"]
    try:
        page = page_as("uitest-clement", weave)
        open_activity(page)

        foreign_nid = add_node(api, other, "a foreign branch appears")
        # local scope: the foreign event must NOT appear
        page.wait_for_timeout(800)
        assert page.get_by_text("activity-foreign-weave").count() == 0, (
            "foreign weave event leaked into the local scope"
        )

        page.get_by_test_id("activity-scope-global").click()
        # backfill + titles fetch: the foreign entry appears, tagged
        poll(
            lambda: page.locator(".weave-tag", has_text="activity-foreign-weave").count()
            >= 1,
            desc="foreign-weave entry with weave tag in global scope",
        )
        # live (post-backfill) foreign events stream in too
        add_node(api, other, "second foreign branch", foreign_nid)
        poll(
            lambda: page.locator(".weave-tag", has_text="activity-foreign-weave").count()
            >= 2,
            desc="live foreign event streamed into the global feed",
        )
        # events of the CURRENT weave show untagged in global scope
        assert page.locator(".weave-tag", has_text="ui-test weave").count() == 0

        # back to local: foreign entries disappear
        page.get_by_test_id("activity-scope-weave").click()
        expect(page.locator(".weave-tag")).to_have_count(0)
    finally:
        httpx.delete(f"{API}/weaves/{other}", timeout=5)
