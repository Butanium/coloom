"""Ctrl+Z undo (undo.svelte.ts + the soft-delete/restore endpoints).

Deletions ask no confirmation; the inverse op lands on an undo stack and a
toast offers an "undo" button. Ctrl+Z (rebindable action 'undo') pops the
stack for the open weave. In-editor TEXT undo stays deferred: Ctrl+Z while
typing must stay with the browser.

Every mutation is verified through the REST API. Identity: "uitest-clement".
Requires the soft-delete backend (DELETE → {deleted_node_ids, moved_cursors},
POST .../restore) — docs/events-api.md.
"""

import time

import pytest
from playwright.sync_api import expect

CLIENT_SYNC = 1.0


def poll(fn, *, timeout=8.0, interval=0.2, desc="condition"):
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


def find_by_prefix(w, prefix: str) -> str:
    ids = [nid for nid, n in w["nodes"].items() if ntext(n).startswith(prefix)]
    assert len(ids) == 1, f"prefix {prefix!r} matched {len(ids)} nodes"
    return ids[0]


def park_cursor(api, wid, page, node_id, name="uitest-clement"):
    api.put(
        f"/weaves/{wid}/cursors/{name}",
        json={"node_id": node_id, "moved_by": name},
    ).raise_for_status()
    page.wait_for_timeout(int(CLIENT_SYNC * 1000))


def test_delete_then_ctrl_z_restores_subtree_and_cursor(page_as, weave, api):
    """Delete a subtree via the Delete key, Ctrl+Z brings the WHOLE subtree
    back (bookmarks intact) and re-parks the relocated cursor on the victim."""
    page = page_as("uitest-clement", weave)
    w = weave_json(api, weave)
    root1 = find_by_prefix(w, "The loom hummed")
    victim = w["nodes"][root1]["children"][0]  # bookmarked, has 3 children
    subtree = [victim, *w["nodes"][victim]["children"]]
    assert len(subtree) >= 3
    assert w["nodes"][victim]["bookmarked"] is True

    park_cursor(api, weave, page, victim)
    page.keyboard.press("Delete")
    poll(
        lambda: all(nid not in weave_json(api, weave)["nodes"] for nid in subtree),
        desc="subtree soft-deleted",
    )
    # server relocated the stranded cursor to the parent
    poll(
        lambda: get_cursors(api, weave)["uitest-clement"]["node_id"] == root1,
        desc="cursor relocated to parent",
    )

    page.keyboard.press("Control+z")
    poll(
        lambda: all(nid in weave_json(api, weave)["nodes"] for nid in subtree),
        desc="subtree restored after Ctrl+Z",
    )
    w2 = weave_json(api, weave)
    assert w2["nodes"][victim]["bookmarked"] is True, "bookmark lost across undo"
    assert victim in w2["bookmarks"], "weave bookmark list lost the restored node"
    # undo re-parks the cursor where it sat before the delete
    poll(
        lambda: get_cursors(api, weave)["uitest-clement"]["node_id"] == victim,
        desc="cursor re-parked on the restored node",
    )


def test_toast_undo_button_restores(page_as, weave, api):
    """The post-delete toast's "undo" button restores the deletion."""
    page = page_as("uitest-clement", weave)
    w = weave_json(api, weave)
    root2 = find_by_prefix(w, "Chapter 2.")
    victim = w["nodes"][root2]["children"][0]

    park_cursor(api, weave, page, victim)
    page.keyboard.press("Delete")
    poll(
        lambda: victim not in weave_json(api, weave)["nodes"],
        desc="node soft-deleted",
    )
    undo_btn = page.get_by_test_id("toast-action")
    expect(undo_btn).to_have_text("undo")
    undo_btn.click()
    poll(
        lambda: victim in weave_json(api, weave)["nodes"],
        desc="node restored via the toast undo button",
    )
    # the undo toast is consumed; a second click can't double-restore
    expect(page.get_by_test_id("toast-action")).to_have_count(0)


def test_two_deletes_two_ctrl_z_restore_in_reverse_order(page_as, weave, api):
    """The undo stack is LIFO: two deletions, two Ctrl+Z — the second restores
    the FIRST deletion too."""
    page = page_as("uitest-clement", weave)
    w = weave_json(api, weave)
    root1 = find_by_prefix(w, "The loom hummed")
    root2 = find_by_prefix(w, "Chapter 2.")
    victim_a = w["nodes"][root1]["children"][1]
    victim_b = w["nodes"][root2]["children"][1]

    park_cursor(api, weave, page, victim_a)
    page.keyboard.press("Delete")
    poll(lambda: victim_a not in weave_json(api, weave)["nodes"], desc="A deleted")
    park_cursor(api, weave, page, victim_b)
    # NOTE: this park races the delete-triggered snapshot refetch — it used to
    # expose the stale-snapshot-overwrites-newer-patch bug (fixed in
    # state.svelte.ts refetchWeave: in-flight patches re-apply on top)
    page.keyboard.press("Delete")
    poll(lambda: victim_b not in weave_json(api, weave)["nodes"], desc="B deleted")

    page.keyboard.press("Control+z")  # undoes B (most recent)
    poll(lambda: victim_b in weave_json(api, weave)["nodes"], desc="B restored")
    assert victim_a not in weave_json(api, weave)["nodes"], (
        "first Ctrl+Z must only undo the most recent deletion"
    )
    page.keyboard.press("Control+z")  # undoes A
    poll(lambda: victim_a in weave_json(api, weave)["nodes"], desc="A restored")


def test_ctrl_z_while_typing_stays_native(page_as, weave, api):
    """While focus is in the contenteditable doc, Ctrl+Z is NOT intercepted
    (in-editor text undo is the browser's): a pending node-restore must NOT
    fire from a typing context."""
    page = page_as("uitest-clement", weave)
    w = weave_json(api, weave)
    root2 = find_by_prefix(w, "Chapter 2.")
    victim = w["nodes"][root2]["children"][0]

    park_cursor(api, weave, page, victim)
    page.keyboard.press("Delete")
    poll(
        lambda: victim not in weave_json(api, weave)["nodes"],
        desc="node soft-deleted",
    )

    # focus the doc and press Ctrl+Z from inside the text context
    doc = page.locator(".doc")
    doc.click()
    page.keyboard.press("Control+z")
    page.wait_for_timeout(1500)
    assert victim not in weave_json(api, weave)["nodes"], (
        "Ctrl+Z from the text pane must not trigger the node-restore undo"
    )

    # leaving the text context, Ctrl+Z works again
    page.evaluate(
        "() => document.activeElement instanceof HTMLElement && document.activeElement.blur()"
    )
    page.keyboard.press("Control+z")
    poll(
        lambda: victim in weave_json(api, weave)["nodes"],
        desc="node restored once focus left the text pane",
    )
