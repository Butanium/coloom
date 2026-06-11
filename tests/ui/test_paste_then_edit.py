"""Regression: paste multiline text, let it apply, then type mid-document.

The 2026-06-10 blank-pane bug: a real clipboard paste into an empty doc leaves
Chromium-created raw text nodes; the post-apply recovery rebuild used to
"sweep" untracked strays out of the contenteditable — but Svelte 5 uses empty
TEXT nodes (not just comments) as block anchors, and the sweep removed the
each-block's `#text ""` anchor. Every later Svelte insertion then targeted a
detached node and silently vanished: the next mid-thread edit (split path)
blanked the text pane permanently while server + local state stayed correct.
Fix: `{#key docEpoch}` wraps the whole .doc element, so recovery REPLACES the
element (fresh anchors) instead of mutating inside it. scratch evidence:
scratch/repro_paste_then_type.py --multiline --midclick.

Conventions: real clipboard paste (Ctrl+V) + real keystrokes; assertions go
through REST where applicable.
"""

import time

import pytest

IDENTITY = "uitest-vanish"
DEADLINE_S = 8.0

PASTE_MULTILINE = (
    "The loom remembers what the weaver forgets.\n\n"
    "A second paragraph, because real pasted prose has newlines.\n"
    "And a third line."
)


@pytest.fixture()
def blank_weave(api):
    r = api.post("/weaves", json={"title": "ui-test paste-then-edit weave"})
    r.raise_for_status()
    wid = r.json()["id"]
    yield wid
    api.delete(f"/weaves/{wid}")


def get_thread(api, weave_id, cursor=IDENTITY):
    r = api.get(f"/weaves/{weave_id}/cursors/{cursor}/thread")
    if r.status_code == 404:  # cursor doesn't exist yet (blank weave, pre-paste)
        return {"nodes": []}
    r.raise_for_status()
    return r.json()


def node_text(node):
    c = node["content"]
    return c["text"] if c["type"] == "snippet" else "".join(t["text"] for t in c["tokens"])


def wait_until(page, predicate, deadline_s=DEADLINE_S, interval_ms=200):
    end = time.monotonic() + deadline_s
    val = predicate()
    while not val and time.monotonic() < end:
        page.wait_for_timeout(interval_ms)
        val = predicate()
    return val


def doc_text(page):
    # innerText (not text_content): preserves the block-level newlines Chromium
    # may have introduced; matches what the editor diffs against
    return page.locator(".doc").inner_text()


def flush_edit(page):
    """Boundary-flush the locally-held edit: edits are local-until-boundary
    (no auto-apply timer); blurring the doc is the cheapest boundary."""
    page.locator(".doc").evaluate("el => el.blur()")


def place_caret(page, offset):
    """Put the caret at char `offset` of the doc (real selection, then typing)."""
    page.locator(".doc").click()
    page.evaluate(
        """(offset) => {
            const doc = document.querySelector('.doc');
            const walker = document.createTreeWalker(doc, NodeFilter.SHOW_TEXT);
            let acc = 0, n;
            while ((n = walker.nextNode())) {
                const len = n.textContent.length;
                if (acc + len >= offset) {
                    const r = document.createRange();
                    r.setStart(n, offset - acc);
                    r.collapse(true);
                    const sel = document.getSelection();
                    sel.removeAllRanges();
                    sel.addRange(r);
                    doc.focus();
                    return;
                }
                acc += len;
            }
            throw new Error('offset past end of doc');
        }""",
        offset,
    )


def test_paste_multiline_then_midthread_type_does_not_blank_the_pane(
    blank_weave, page_as, api
):
    page = page_as(IDENTITY, blank_weave)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    doc = page.locator(".doc")
    doc.click()
    page.wait_for_timeout(200)

    # REAL clipboard paste (Ctrl+V): synthetic insertText doesn't reproduce the
    # raw text nodes Chromium creates when pasting into an empty contenteditable
    page.evaluate("t => navigator.clipboard.writeText(t)", PASTE_MULTILINE)
    doc.click()
    page.keyboard.press("Control+v")
    flush_edit(page)

    # paste applies (boundary flush) and the pane re-renders canonically
    assert wait_until(
        page, lambda: get_thread(api, blank_weave)["nodes"]
    ), "pasted node never reached the server"
    page.wait_for_timeout(800)  # let the un-freeze + recovery rebuild settle
    assert doc_text(page) == PASTE_MULTILINE

    # mid-thread edit: caret inside the first sentence, type a few chars — the
    # SPLIT path (head + edited branch). This is what used to blank the pane.
    place_caret(page, 19)
    page.keyboard.type("abc", delay=60)
    flush_edit(page)
    expected = PASTE_MULTILINE[:19] + "abc" + PASTE_MULTILINE[19:]

    # server: thread = [head, edited branch]; original tail survives as sibling.
    # Poll for the FINAL state: between the split and the branch landing, the
    # thread is briefly [head, original-tail] (the split parks the cursor on
    # the tail) — also length 2, so length alone is a race.
    def split_applied():
        nodes = get_thread(api, blank_weave)["nodes"]
        if len(nodes) == 2 and "abc" in node_text(nodes[1]):
            return nodes
        return None

    thread_nodes = wait_until(page, split_applied)
    assert thread_nodes, f"split+branch never applied: {get_thread(api, blank_weave)}"
    assert node_text(thread_nodes[0]) == PASTE_MULTILINE[:19]
    assert node_text(thread_nodes[1]) == "abc" + PASTE_MULTILINE[19:]

    # THE regression: the pane must still render the full merged text (it used
    # to go permanently blank here while server + cursor state were correct)
    assert wait_until(page, lambda: doc_text(page) == expected, deadline_s=10.0), (
        f"text pane diverged after mid-thread edit: {doc_text(page)!r}"
    )

    # and the editor must still be ALIVE (the old failure detached Svelte's
    # anchors, silently eating every later render): type again at the doc end
    page.mouse.move(2, 2)
    page.wait_for_timeout(350)
    doc.click()
    page.evaluate(
        """() => {
            const doc = document.querySelector('.doc');
            const r = document.createRange();
            r.selectNodeContents(doc);
            r.collapse(false);
            const sel = document.getSelection();
            sel.removeAllRanges();
            sel.addRange(r);
            doc.focus();
        }"""
    )
    page.keyboard.type(" tail", delay=60)
    flush_edit(page)

    def tail_applied():
        nodes = get_thread(api, blank_weave)["nodes"]
        return "".join(node_text(n) for n in nodes).endswith(" tail")

    assert wait_until(page, tail_applied), "post-recovery append never applied"
    assert wait_until(
        page, lambda: doc_text(page) == expected + " tail", deadline_s=10.0
    ), f"pane wrong after post-recovery append: {doc_text(page)!r}"

    assert errors == [], f"pageerrors during the scenario: {errors}"
