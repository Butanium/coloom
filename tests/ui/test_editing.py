"""Free-form thread editor tests: the doc is a contenteditable buffer; edits are
diffed (web/src/lib/editbuffer.ts) into split/append/hybrid-edit/copy ops applied
over REST. Nothing is ever destroyed — mid-document edits strand the original as
a sibling branch. A blank weave is the same surface: typing creates the root.

Every assertion goes through the REST API (the `api` fixture), polling for the
WS round trip. Edits are driven by selecting a character range in the doc and
inserting text via execCommand('insertText') — fires a real `input` event, the
exact path a user keystroke takes.

Selector map (web/src/lib/TextPane.svelte):
  .doc                               the contenteditable thread document
  .doc [data-node-id]                one span per thread node
  .doc .token                        per-token spans (tokens nodes only)
  .doc .token.inexact                a token whose logprob is carried over (inexact)
  .editing-bar                       shown while dirty/applying (not the sync dot)
"""

import time

import pytest

WS_SETTLE_MS = 1000
DEADLINE_S = 8.0


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


def node_text(node):
    c = node["content"]
    return c["text"] if c["type"] == "snippet" else "".join(t["text"] for t in c["tokens"])


def add_node(api, weave_id, text=None, *, content=None, parent, creator_label="uitest-clement",
             move_cursor=True):
    body = {
        "parent_id": parent,
        "creator": {"type": "human", "label": creator_label},
        "move_cursor": creator_label if move_cursor else None,
    }
    if content is not None:
        body["content"] = content
        body["text"] = ""
    else:
        body["text"] = text
    r = api.post(f"/weaves/{weave_id}/nodes", json=body)
    r.raise_for_status()
    return r.json()


def set_cursor(api, weave_id, node_id, name="uitest-clement"):
    r = api.put(f"/weaves/{weave_id}/cursors/{name}", json={"node_id": node_id, "moved_by": name})
    r.raise_for_status()


def wait_until(page, predicate, deadline_s=DEADLINE_S, interval_ms=200):
    end = time.monotonic() + deadline_s
    val = predicate()
    while not val and time.monotonic() < end:
        page.wait_for_timeout(interval_ms)
        val = predicate()
    return val


def tokens_node_of_thread(thread):
    for i, node in enumerate(thread["nodes"]):
        if node["content"]["type"] == "tokens":
            return i, node
    raise AssertionError("thread has no tokens node")


def collect_pageerrors(page):
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    return errors


def wait_for_doc(page, predicate_text, deadline_s=DEADLINE_S):
    """Wait until the doc's textContent satisfies a substring/predicate check."""
    return wait_until(
        page,
        lambda: predicate_text(page.locator(".doc").text_content() or ""),
        deadline_s=deadline_s,
    )


def edit_replace(page, start, end, text):
    """Select doc chars [start, end) and insert `text` (replacing the selection).
    Fires a real input event via execCommand — the keystroke code path."""
    page.locator(".doc").click()  # focus the editable
    page.evaluate(
        """([start, end, text]) => {
            const doc = document.querySelector('.doc');
            const walker = document.createTreeWalker(doc, NodeFilter.SHOW_TEXT);
            let acc = 0, sNode = null, sOff = 0, eNode = null, eOff = 0, n;
            while ((n = walker.nextNode())) {
                const len = n.textContent.length;
                if (sNode === null && acc + len >= start) { sNode = n; sOff = start - acc; }
                if (eNode === null && acc + len >= end) { eNode = n; eOff = end - acc; break; }
                acc += len;
            }
            const range = document.createRange();
            range.setStart(sNode, sOff);
            range.setEnd(eNode, eOff);
            const sel = document.getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
            doc.focus();
            document.execCommand('insertText', false, text);
        }""",
        [start, end, text],
    )


def type_at_end(page, text):
    """Place the caret at the very end of the doc and type `text`."""
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
    r = api.post("/weaves", json={"title": "ui-test blank editing weave"})
    r.raise_for_status()
    wid = r.json()["id"]
    yield wid
    api.delete(f"/weaves/{wid}")


# ---------------------------------------------------------------- blank weave


def test_blank_weave_typing_creates_root_then_coalesces(blank_weave, page_as, api):
    """Typing into a completely empty weave creates ONE root snippet node (human,
    my label, my cursor moved onto it); continued typing coalesces into that SAME
    node — no chain of one-batch nodes, no duplicated text in the doc."""
    page = page_as("uitest-clement", blank_weave)
    errors = collect_pageerrors(page)
    assert get_weave(api, blank_weave)["nodes"] == {}, "blank weave is not blank"

    page.locator(".doc").click()
    page.keyboard.type("Once upon")

    def root_made():
        w = get_weave(api, blank_weave)
        return w if len(w["nodes"]) == 1 else None

    after = wait_until(page, root_made)
    assert after, f"typing into the blank weave created no node; errors={errors}"
    ((nid, node),) = after["nodes"].items()
    assert node["content"] == {"type": "snippet", "text": "Once upon"}
    assert node["creator"]["type"] == "human"
    assert node["creator"]["label"] == "uitest-clement"
    assert node["parents"] == [], "root node must have no parents"
    assert after["roots"] == [nid]
    assert get_cursor(api, blank_weave)["node_id"] == nid, "cursor not moved to the root"

    # the doc shows the text exactly once (the typed stray DOM was cleaned up)
    assert wait_until(
        page, lambda: (page.locator(".doc").text_content() or "") == "Once upon",
        deadline_s=5,
    ), f"doc text diverged: {page.locator('.doc').text_content()!r}"

    # continue typing -> coalesces into the SAME node
    page.wait_for_timeout(WS_SETTLE_MS)
    type_at_end(page, " a time")

    def grew():
        w = get_weave(api, blank_weave)
        n = w["nodes"].get(nid)
        return w if n and node_text(n) == "Once upon a time" else None

    after2 = wait_until(page, grew)
    assert after2, f"continued typing did not coalesce into the root; errors={errors}"
    assert len(after2["nodes"]) == 1, "coalescing must not create a second node"
    assert errors == [], f"page errors during blank-weave typing: {errors}"


# ---------------------------------------------------------------- append coalescing


def test_append_typing_coalesces_into_one_growing_human_node(weave, page_as, api):
    """Typing at the thread end: the first append (leaf is a MODEL node) creates a
    new human snippet node; subsequent typing into that same human leaf COALESCES
    (updateNode grows the one node, no chain of one-keystroke nodes)."""
    page = page_as("uitest-clement", weave)
    errors = collect_pageerrors(page)
    before = get_weave(api, weave)
    n_before = len(before["nodes"])

    type_at_end(page, " FIRST")
    # one new human node appended under the model leaf
    new_node = wait_until(
        page,
        lambda: next(
            (n for nid, n in get_weave(api, weave)["nodes"].items()
             if nid not in before["nodes"]
             and n["creator"]["type"] == "human"
             and node_text(n).strip().startswith("FIRST")),
            None,
        ),
    )
    assert new_node, f"first append created no human node; errors={errors}"
    after_first = get_weave(api, weave)
    assert len(after_first["nodes"]) == n_before + 1, "first append should add exactly one node"

    # keep typing into the SAME growing node -> coalesce (no extra node)
    type_at_end(page, " SECOND")
    grew = wait_until(
        page,
        lambda: "SECOND" in node_text(get_weave(api, weave)["nodes"].get(new_node["id"], new_node)),
    )
    assert grew, "second append did not coalesce into the growing human node"
    after_second = get_weave(api, weave)
    assert len(after_second["nodes"]) == n_before + 1, (
        "coalescing must NOT create a new node per keystroke-batch"
    )
    grown = after_second["nodes"][new_node["id"]]
    assert grown["content"]["type"] == "snippet"
    assert "FIRST" in grown["content"]["text"] and "SECOND" in grown["content"]["text"]
    assert errors == [], f"page errors during append: {errors}"


# ---------------------------------------------------------------- mid-node hybrid edit


def test_mid_node_edit_of_model_tokens_makes_hybrid_sibling(weave, page_as, api):
    """Edit a range entirely inside a model Tokens node: the original branch stays
    intact (head + tail = original), a NEW hybrid sibling appears under the head
    with model attribution + edited_by metadata, a logprob-null middle token, and
    inexact-flagged preserved suffix tokens. Cursor moves to the hybrid."""
    page = page_as("uitest-clement", weave)
    errors = collect_pageerrors(page)
    thread = get_thread(api, weave)
    tok_idx_in_path, tok_node = tokens_node_of_thread(thread)
    before = get_weave(api, weave)
    orig_tokens = [t["text"] for t in tok_node["content"]["tokens"]]
    orig_text = "".join(orig_tokens)

    # doc offset where the tokens node starts = length of all earlier nodes
    base = sum(len(node_text(n)) for n in thread["nodes"][:tok_idx_in_path])
    # replace tokens [3,5) (middle of the node) with a marker, leaving a real
    # token suffix to preserve
    start = base + sum(len(t) for t in orig_tokens[:3])
    end = base + sum(len(t) for t in orig_tokens[:5])
    edit_replace(page, start, end, "XMID")

    def settled():
        now = get_weave(api, weave)
        fresh = set(now["nodes"]) - set(before["nodes"])
        head = now["nodes"].get(tok_node["id"])
        if head and len(fresh) >= 2:
            return now, fresh
        return None

    result = wait_until(page, settled)
    assert result, f"mid-node edit produced no split+hybrid; errors={errors}"
    after, fresh = result

    # the head keeps the node id + the prefix tokens (token-aligned)
    head = after["nodes"][tok_node["id"]]
    assert head["content"]["type"] == "tokens"
    assert [t["text"] for t in head["content"]["tokens"]] == orig_tokens[:3], (
        "head not truncated at the prefix token boundary"
    )

    # the ORIGINAL branch survives: the split tail under the head reconstructs the
    # original node content
    tails = [
        after["nodes"][c]
        for c in head["children"]
        if c in fresh and node_text(after["nodes"][c]) == "".join(orig_tokens[3:])
    ]
    assert len(tails) == 1, "original suffix branch (split tail) not intact under head"

    # the HYBRID node: sibling of the tail, model attribution, edited_by metadata
    hybrids = [
        after["nodes"][c]
        for c in head["children"]
        if c in fresh
        and after["nodes"][c]["metadata"].get("edited_by") == "uitest-clement"
    ]
    assert len(hybrids) == 1, f"hybrid edited node not found under head; errors={errors}"
    hybrid = hybrids[0]
    assert hybrid["creator"]["type"] == "model", "hybrid lost the model attribution"
    assert hybrid["metadata"].get("edited_from") == tok_node["id"]
    assert hybrid["content"]["type"] == "tokens"
    htoks = hybrid["content"]["tokens"]
    # first token = the edited middle: logprob null, no top_logprobs
    assert htoks[0]["text"] == "XMID"
    assert htoks[0]["logprob"] is None, "edited middle token should have null logprob"
    assert htoks[0]["top_logprobs"] == []
    # the preserved suffix tokens (originally tokens [5:]) carried + flagged inexact
    suffix = htoks[1:]
    assert [t["text"] for t in suffix] == orig_tokens[5:], "suffix tokens not preserved"
    assert all(t.get("inexact") is True for t in suffix), "suffix tokens not flagged inexact"

    # cursor followed the hybrid (it is the deepest new node, no downstream)
    assert get_cursor(api, weave)["node_id"] == hybrid["id"], "cursor did not follow the hybrid"

    # the new branch's text reflects the edit; the head+tail still spell the original
    assert node_text(head) + node_text(tails[0]) == orig_text
    assert errors == [], f"page errors during mid-node edit: {errors}"

    # inexact tokens render with the dotted-underline class in the doc
    assert wait_until(
        page, lambda: page.locator(".doc .token.inexact").count() >= 1, deadline_s=4
    ), "inexact tokens not visually marked in the doc"


# ---------------------------------------------------------------- downstream copy


def test_mid_thread_edit_copies_downstream_node_with_provenance(weave, page_as, api):
    """Edit a node that has a downstream thread node: the downstream node is
    re-created on the new branch as a copy (creator preserved, copied_from
    provenance, tokens flagged inexact), and the cursor lands on the deepest copy.
    The original chain remains untouched as a sibling."""
    page = page_as("uitest-clement", weave)
    errors = collect_pageerrors(page)
    thread = get_thread(api, weave)
    # uitest-clement's thread is [human-root-snippet, model-tokens]. Build a downstream
    # node so the EDITED node (the root snippet) has a thread node after it.
    root = thread["nodes"][0]
    _, tok_node = tokens_node_of_thread(thread)
    # put the cursor on the root snippet so the model-tokens node is downstream
    set_cursor(api, weave, root["id"])
    assert wait_for_doc(page, lambda t: node_text(root)[:20] in t), "doc did not follow cursor"
    # the rendered thread is now just the root (cursor at root). To have a real
    # DOWNSTREAM node, edit the FIRST node while a child is on-thread: move cursor
    # to the model tokens node, which makes [root, tokens] the thread; edit inside
    # root so `tokens` is downstream.
    set_cursor(api, weave, tok_node["id"])
    assert wait_for_doc(page, lambda t: "".join(
        x["text"] for x in tok_node["content"]["tokens"]
    )[:10] in t), "doc did not rebuild on cursor move"

    before = get_weave(api, weave)
    root_text = node_text(root)
    # edit a range inside the root snippet (offsets 4..8 -> mid 'loom hummed')
    edit_replace(page, 4, 8, "WOVE")

    def settled():
        now = get_weave(api, weave)
        fresh = set(now["nodes"]) - set(before["nodes"])
        # expect: split tail of root + new edited snippet + a copied tokens node
        copied = [
            now["nodes"][nid] for nid in fresh
            if now["nodes"][nid]["metadata"].get("copied_from") == tok_node["id"]
        ]
        return (now, fresh, copied[0]) if copied else None

    result = wait_until(page, settled)
    assert result, f"downstream copy not produced; errors={errors}"
    after, fresh, copy = result

    # the copy preserves the original creator + flags its tokens inexact
    assert copy["creator"]["type"] == tok_node["creator"]["type"]
    assert copy["content"]["type"] == "tokens"
    assert [t["text"] for t in copy["content"]["tokens"]] == [
        t["text"] for t in tok_node["content"]["tokens"]
    ], "copied token text diverged from the source"
    assert all(t.get("inexact") is True for t in copy["content"]["tokens"]), (
        "copied tokens not flagged inexact"
    )

    # original node untouched (still in the weave with its original content)
    assert node_text(after["nodes"][tok_node["id"]]) == "".join(
        t["text"] for t in tok_node["content"]["tokens"]
    ), "original downstream node was mutated (should be copied, not moved)"

    # cursor landed on the deepest new node = the copy
    assert get_cursor(api, weave)["node_id"] == copy["id"], "cursor did not land on the copy"
    # the edited middle is in the doc
    assert wait_for_doc(page, lambda t: "WOVE" in t), "edited text not rendered"
    # the head (root id) keeps the kept prefix of the original snippet
    assert after["nodes"][root["id"]]["content"]["text"] == root_text[:4]
    assert errors == [], f"page errors during downstream-copy edit: {errors}"


# ---------------------------------------------------------------- tail deletion


def test_tail_deletion_moves_cursor_without_deleting(weave, page_as, api):
    """Deleting from the end of the doc never deletes a node: it splits at the new
    end (if mid-node) and moves the cursor up; downstream survives as a branch."""
    page = page_as("uitest-clement", weave)
    errors = collect_pageerrors(page)
    thread = get_thread(api, weave)
    _, tok_node = tokens_node_of_thread(thread)
    before = get_weave(api, weave)
    n_before = len(before["nodes"])
    full = page.locator(".doc").text_content()
    orig_tokens = [t["text"] for t in tok_node["content"]["tokens"]]

    # delete the last ~4 tokens' worth of text (a prefix of the buffer remains)
    keep = len(full) - sum(len(t) for t in orig_tokens[-4:])
    edit_replace(page, keep, len(full), "")

    def settled():
        now = get_weave(api, weave)
        # nothing removed; a split may have added the tail node
        if len(now["nodes"]) < n_before:
            return None  # something was deleted -> fail
        head = now["nodes"][tok_node["id"]]
        # the head should now hold fewer tokens (split at the new end)
        if len(head["content"]["tokens"]) < len(orig_tokens):
            cur = get_cursor(api, weave)["node_id"]
            if cur == tok_node["id"]:
                return now
        return None

    result = wait_until(page, settled)
    assert result, f"tail deletion did not split+move cursor (or deleted a node); errors={errors}"
    after = result
    assert len(after["nodes"]) >= n_before, "tail deletion destroyed a node (must not)"

    # the split tail still carries the deleted suffix tokens (nothing destroyed)
    head = after["nodes"][tok_node["id"]]
    tail_ids = [c for c in head["children"] if c not in before["nodes"]]
    assert tail_ids, "no split tail preserving the deleted suffix"
    tail = after["nodes"][tail_ids[0]]
    assert node_text(head) + node_text(tail) == "".join(orig_tokens), (
        "head+tail no longer reconstruct the original node (suffix lost)"
    )
    assert errors == [], f"page errors during tail deletion: {errors}"


# ---------------------------------------------------------------- caret survives re-render


def test_caret_survives_a_rerender_after_edit(weave, page_as, api):
    """After an edit applies and the WS refetch rebuilds spans, the caret is
    restored to its character offset (not lost / not jumped to the end)."""
    page = page_as("uitest-clement", weave)
    errors = collect_pageerrors(page)
    before = get_weave(api, weave)

    type_at_end(page, " CARETMARK")
    assert wait_for_doc(page, lambda t: "CARETMARK" in t), "edit never applied"
    # let the WS refetch rebuild the doc
    page.wait_for_timeout(WS_SETTLE_MS)

    # caret offset = somewhere inside the just-typed text (end of doc region).
    caret_off = page.evaluate(
        """() => {
            const doc = document.querySelector('.doc');
            const sel = document.getSelection();
            if (!sel || sel.rangeCount === 0) return -1;
            const range = sel.getRangeAt(0);
            if (!doc.contains(range.startContainer)) return -1;
            const r = document.createRange();
            r.selectNodeContents(doc);
            r.setEnd(range.startContainer, range.startOffset);
            return r.toString().length;
        }"""
    )
    full_len = len(page.locator(".doc").text_content() or "")
    assert caret_off != -1, "caret was lost after the re-render (selection left the doc)"
    # the caret should be at/near where we were typing (end), not reset to 0
    assert caret_off >= full_len - len(" CARETMARK"), (
        f"caret not restored near the edit point (at {caret_off}/{full_len})"
    )
    assert errors == [], f"page errors during caret-survival: {errors}"


# ---------------------------------------------------------------- emoji code points


def test_emoji_snippet_edit_is_codepoint_correct(weave, page_as, api):
    """A snippet split must use Python CODE-POINT offsets (an emoji is 2 UTF-16
    units, 1 code point). Edit just before an emoji and verify the split halves
    keep the emoji intact (no torn surrogate)."""
    page = page_as("uitest-clement", weave)
    errors = collect_pageerrors(page)
    thread = get_thread(api, weave)
    _, tok_node = tokens_node_of_thread(thread)

    # build a fresh single-snippet thread with an emoji so offsets are controlled
    emoji_node = add_node(
        api, weave, "AB😀CD downstream tail here", parent=tok_node["id"]
    )
    # a downstream node so the edit is a mid-thread edit that exercises a snippet
    # split (and a copy), not a pure tail edit
    down = add_node(api, weave, "TAILNODE", parent=emoji_node["id"])
    set_cursor(api, weave, down["id"])
    assert wait_for_doc(page, lambda t: "AB😀CD" in t and "TAILNODE" in t), (
        "doc did not render the emoji thread"
    )
    before = get_weave(api, weave)

    # offsets are UTF-16 in the buffer. 'AB😀CD...' -> A=0 B=1 😀=2,3 C=4 D=5.
    # Replace 'B😀C' (utf16 [base+1, base+5)) with 'Z' -> exercises an emoji that
    # straddles the edit boundary; the kept prefix 'A' and suffix 'D...' must be
    # code-point-correct (no lone surrogate).
    base = page.evaluate(
        """(needle) => (document.querySelector('.doc').textContent || '').indexOf(needle)""",
        "AB😀CD",
    )
    assert base >= 0
    edit_replace(page, base + 1, base + 5, "Z")

    def settled():
        now = get_weave(api, weave)
        fresh = set(now["nodes"]) - set(before["nodes"])
        return now if len(fresh) >= 1 else None

    after = wait_until(page, settled)
    assert after, f"emoji edit produced nothing; errors={errors}"

    # the head (emoji node id) keeps exactly 'A' (1 code point), no torn surrogate
    head = after["nodes"][emoji_node["id"]]
    assert head["content"]["type"] == "snippet"
    assert head["content"]["text"] == "A", (
        f"emoji-adjacent split is not code-point-correct: head={head['content']['text']!r}"
    )
    # the doc text is well-formed (no replacement char / lone surrogate)
    doc_text = page.locator(".doc").text_content() or ""
    assert "�" not in doc_text, "torn surrogate / replacement char in the doc"
    assert "AZD" in doc_text, f"edited snippet not spelled 'AZD...': {doc_text!r}"
    assert errors == [], f"page errors during emoji edit: {errors}"


# ---------------------------------------------------------------- multiline newlines


def test_multiline_insert_preserves_newlines(weave, page_as, api):
    """Inserting multiline text mid-document keeps its newlines in the weave.

    Regression: the browser splits multiline contenteditable input into block
    <div>s; reading docEl.textContent (or Range.toString()) joins them WITHOUT the
    '\\n', silently dropping the paragraph breaks. applyEdit must reconstruct them
    (docInnerText). Drives execCommand insertText with embedded newlines — exactly
    what a plaintext paste lands as in Chromium."""
    page = page_as("uitest-clement", weave)
    errors = collect_pageerrors(page)
    before = get_weave(api, weave)
    n_before = len(before["nodes"])

    multiline = "MLA\nMLB\nMLC"
    # insert mid-root-snippet (offset 12) so it's a mid-thread edit, not a tail append
    page.evaluate(
        """([off, text]) => {
            const doc = document.querySelector('.doc');
            const walker = document.createTreeWalker(doc, NodeFilter.SHOW_TEXT);
            let acc = 0, n, tn = null, to = 0;
            while ((n = walker.nextNode())) {
                const len = n.textContent.length;
                if (acc + len >= off) { tn = n; to = off - acc; break; }
                acc += len;
            }
            const r = document.createRange();
            r.setStart(tn, to); r.collapse(true);
            const sel = document.getSelection();
            sel.removeAllRanges(); sel.addRange(r);
            doc.focus();
            document.execCommand('insertText', false, text);
        }""",
        [12, multiline],
    )

    # the newlines must survive verbatim into some node's stored text
    def newlines_kept():
        now = get_weave(api, weave)
        return any(multiline in node_text(n) for n in now["nodes"].values())

    assert wait_until(page, newlines_kept), (
        f"multiline insert dropped its newlines from the weave; errors={errors}"
    )
    after = get_weave(api, weave)
    assert len(after["nodes"]) >= n_before, "multiline insert destroyed a node"
    assert errors == [], f"page errors during multiline insert: {errors}"
