"""merge_with_parent: non-destructive merge of a node N into its parent P.

A NEW node M takes concat(P, N) and N's children; N (and P, when N was P's
only live child) is soft-deleted — restorable. See docs/events-api.md
("Merge with parent") for the binding semantics."""

import pytest
from coloom.config import ColoomConfig
from coloom.models import HumanCreator, ModelCreator, Snippet, Token, Tokens
from coloom.server.app import create_app
from coloom.store import Conflict, NotFound, WeaveStore
from fastapi.testclient import TestClient


@pytest.fixture
def store(tmp_path):
    s = WeaveStore(tmp_path / "merge.sqlite")
    yield s
    s.close()


def make_tokens(*texts: str) -> Tokens:
    return Tokens(tokens=[Token(text=t, logprob=-0.5) for t in texts])


def chain(store, wid, *contents, parent=None):
    """Add a chain of nodes; returns the node list."""
    nodes = []
    for content in contents:
        node = store.add_node(wid, content, parent_id=parent)
        nodes.append(node)
        parent = node.id
    return nodes


# ------------------------------------------------------------ case (a): in place


def test_in_place_merge_with_children(store):
    w = store.create_weave()
    r, p, n = chain(store, w.id, Snippet(text="R"), Snippet(text="P"), Snippet(text="N"))
    c1 = store.add_node(w.id, Snippet(text="c1"), parent_id=n.id)
    c2 = store.add_node(w.id, Snippet(text="c2"), parent_id=n.id)

    merged, deleted, moved = store.merge_with_parent(w.id, n.id)
    assert merged.text == "PN"
    assert merged.parents == [r.id]
    assert merged.children == [c1.id, c2.id]  # order preserved
    assert merged.metadata["merged_from"] == [p.id, n.id]
    assert deleted == [p.id, n.id]  # deletion-op root first
    assert moved == []
    for gone in (p.id, n.id):
        with pytest.raises(NotFound):
            store.get_node(w.id, gone)
    # thread content through the merged node is unchanged
    assert store.get_thread_content(w.id, c1.id) == "RPNc1"
    weave = store.get_weave(w.id)
    assert set(weave.nodes) == {r.id, merged.id, c1.id, c2.id}
    assert weave.roots == [r.id]


def test_in_place_merge_of_root_parent_makes_merged_a_root(store):
    w = store.create_weave()
    p, n = chain(store, w.id, Snippet(text="P"), Snippet(text="N"))
    merged, deleted, _ = store.merge_with_parent(w.id, n.id)
    assert merged.parents == []
    assert store.get_weave(w.id).roots == [merged.id]
    assert deleted == [p.id, n.id]


def test_merge_leaf_node(store):
    w = store.create_weave()
    _, _, n = chain(store, w.id, Snippet(text="R"), Snippet(text="P"), Snippet(text="N"))
    merged, _, _ = store.merge_with_parent(w.id, n.id)
    assert merged.children == []


def test_double_merge_deep_chain(store):
    w = store.create_weave()
    r, a, b, c = chain(
        store, w.id, Snippet(text="R"), Snippet(text="A"),
        Snippet(text="B"), Snippet(text="C"),
    )
    m1, deleted1, _ = store.merge_with_parent(w.id, c.id)
    assert m1.text == "BC" and deleted1 == [b.id, c.id]
    # m1 is now A's only live child → merging it folds the whole chain
    m2, deleted2, _ = store.merge_with_parent(w.id, m1.id)
    assert m2.text == "ABC" and deleted2 == [a.id, m1.id]
    assert m2.parents == [r.id]
    assert store.get_thread_content(w.id, m2.id) == "RABC"


# ------------------------------------------------------------ case (b): sibling


def test_sibling_merge_leaves_parent_and_siblings_untouched(store):
    w = store.create_weave()
    r, p = chain(store, w.id, Snippet(text="R"), Snippet(text="P"))
    n = store.add_node(w.id, Snippet(text="N"), parent_id=p.id)
    s = store.add_node(w.id, Snippet(text="S"), parent_id=p.id)
    c1 = store.add_node(w.id, Snippet(text="c1"), parent_id=n.id)

    merged, deleted, _ = store.merge_with_parent(w.id, n.id)
    assert merged.text == "PN"
    assert merged.parents == [r.id]  # new sibling of P under the grandparent
    assert merged.children == [c1.id]  # N's children migrated
    assert deleted == [n.id]  # only N absorbed
    assert store.get_node(w.id, p.id).children == [s.id]  # P keeps the others
    assert store.get_node(w.id, s.id).text == "S"
    assert store.get_thread_content(w.id, c1.id) == "RPNc1"
    assert store.get_thread_content(w.id, s.id) == "RPS"


def test_sibling_merge_under_root_parent_makes_merged_a_root(store):
    w = store.create_weave()
    p = store.add_node(w.id, Snippet(text="P"))
    n = store.add_node(w.id, Snippet(text="N"), parent_id=p.id)
    store.add_node(w.id, Snippet(text="S"), parent_id=p.id)
    merged, _, _ = store.merge_with_parent(w.id, n.id)
    assert merged.parents == []
    assert store.get_weave(w.id).roots == [p.id, merged.id]


def test_deleted_sibling_does_not_block_in_place_merge(store):
    """An already-soft-deleted sibling belongs to an earlier op: N still counts
    as P's only live child, and the earlier op survives the merge's restore."""
    w = store.create_weave()
    _, p = chain(store, w.id, Snippet(text="R"), Snippet(text="P"))
    n = store.add_node(w.id, Snippet(text="N"), parent_id=p.id)
    s = store.add_node(w.id, Snippet(text="S"), parent_id=p.id)
    store.remove_node(w.id, s.id)

    merged, deleted, _ = store.merge_with_parent(w.id, n.id)
    assert deleted == [p.id, n.id]  # in place; S keeps its own op marker
    store.restore_node(w.id, p.id)
    assert store.get_node(w.id, p.id).children == [n.id]
    with pytest.raises(NotFound):
        store.get_node(w.id, s.id)  # S's earlier deletion survives


# ------------------------------------------------------------ content matrix


def test_tokens_plus_tokens_preserves_logprobs(store):
    w = store.create_weave()
    p_tokens = Tokens(
        tokens=[Token(text="a", logprob=-0.1), Token(text="b", logprob=-0.2)]
    )
    n_tokens = Tokens(
        tokens=[
            Token(
                text="c",
                logprob=-0.3,
                entropy=1.5,
                top_logprobs=[{"text": "c", "logprob": -0.3}],
            )
        ]
    )
    _, p, n = chain(store, w.id, Snippet(text="R"), p_tokens, n_tokens)
    merged, _, _ = store.merge_with_parent(w.id, n.id)
    assert isinstance(merged.content, Tokens)
    assert [t.text for t in merged.content.tokens] == ["a", "b", "c"]
    assert [t.logprob for t in merged.content.tokens] == [-0.1, -0.2, -0.3]
    assert merged.content.tokens[2].entropy == 1.5
    assert merged.content.tokens[2].top_logprobs[0].logprob == -0.3


@pytest.mark.parametrize(
    "p_content,n_content",
    [
        (make_tokens("P"), Snippet(text="N")),
        (Snippet(text="P"), make_tokens("N")),
        (Snippet(text="P"), Snippet(text="N")),
    ],
)
def test_any_snippet_involved_degrades_to_snippet(store, p_content, n_content):
    w = store.create_weave()
    _, _, n = chain(store, w.id, Snippet(text="R"), p_content, n_content)
    merged, _, _ = store.merge_with_parent(w.id, n.id)
    assert isinstance(merged.content, Snippet)
    assert merged.text == "PN"


def test_creator_comes_from_parent(store):
    w = store.create_weave()
    p = store.add_node(w.id, Snippet(text="P"), HumanCreator(label="clem"))
    n = store.add_node(
        w.id, Snippet(text="N"), ModelCreator(label="gpt"), parent_id=p.id
    )
    merged, _, _ = store.merge_with_parent(w.id, n.id)
    assert merged.creator == HumanCreator(label="clem")


# ------------------------------------------------------ cursors & bookmarks


def test_cursors_on_absorbed_nodes_migrate_to_merged(store):
    w = store.create_weave()
    _, p, n = chain(store, w.id, Snippet(text="R"), Snippet(text="P"), Snippet(text="N"))
    c1 = store.add_node(w.id, Snippet(text="c1"), parent_id=n.id)
    store.set_cursor(w.id, "on-p", p.id)
    store.set_cursor(w.id, "on-n", n.id)
    store.set_cursor(w.id, "on-child", c1.id)

    merged, _, moved = store.merge_with_parent(w.id, n.id)
    assert {m["name"]: (m["from"], m["to"]) for m in moved} == {
        "on-p": (p.id, merged.id),
        "on-n": (n.id, merged.id),
    }
    cursors = store.list_cursors(w.id)
    assert cursors["on-p"].node_id == merged.id
    assert cursors["on-n"].node_id == merged.id
    assert cursors["on-child"].node_id == c1.id  # untouched
    # the migrated cursor's thread content is unchanged
    thread = store.get_cursor_thread(w.id, "on-n")
    assert "".join(node.text for node in thread) == "RPN"


def test_sibling_merge_keeps_parent_cursor(store):
    w = store.create_weave()
    p = store.add_node(w.id, Snippet(text="P"))
    n = store.add_node(w.id, Snippet(text="N"), parent_id=p.id)
    store.add_node(w.id, Snippet(text="S"), parent_id=p.id)
    store.set_cursor(w.id, "on-p", p.id)
    store.set_cursor(w.id, "on-n", n.id)

    merged, _, moved = store.merge_with_parent(w.id, n.id)
    assert [m["name"] for m in moved] == ["on-n"]
    assert store.list_cursors(w.id)["on-p"].node_id == p.id


@pytest.mark.parametrize("bookmark_parent,bookmark_node", [(True, False), (False, True)])
def test_in_place_merge_inherits_bookmarks(store, bookmark_parent, bookmark_node):
    w = store.create_weave()
    _, p, n = chain(store, w.id, Snippet(text="R"), Snippet(text="P"), Snippet(text="N"))
    if bookmark_parent:
        store.set_bookmarked(w.id, p.id, True)
    if bookmark_node:
        store.set_bookmarked(w.id, n.id, True)
    merged, _, _ = store.merge_with_parent(w.id, n.id)
    assert merged.bookmarked


def test_sibling_merge_inherits_only_nodes_bookmark(store):
    w = store.create_weave()
    p = store.add_node(w.id, Snippet(text="P"))
    n = store.add_node(w.id, Snippet(text="N"), parent_id=p.id)
    store.add_node(w.id, Snippet(text="S"), parent_id=p.id)
    store.set_bookmarked(w.id, p.id, True)  # P stays live and keeps it
    merged, _, _ = store.merge_with_parent(w.id, n.id)
    assert not merged.bookmarked
    assert store.get_node(w.id, p.id).bookmarked


# ------------------------------------------------------------ errors


def test_merge_root_raises_conflict(store):
    w = store.create_weave()
    root = store.add_node(w.id, Snippet(text="R"))
    with pytest.raises(Conflict, match="root"):
        store.merge_with_parent(w.id, root.id)


def test_merge_missing_or_deleted_raises_not_found(store):
    w = store.create_weave()
    _, n = chain(store, w.id, Snippet(text="R"), Snippet(text="N"))
    with pytest.raises(NotFound):
        store.merge_with_parent(w.id, "nope")
    store.remove_node(w.id, n.id)
    with pytest.raises(NotFound):
        store.merge_with_parent(w.id, n.id)


# ------------------------------------------------------------ undo round-trip


def test_undo_in_place_merge_of_leaf_restores_original_shape(store):
    w = store.create_weave()
    r, p, n = chain(store, w.id, Snippet(text="R"), Snippet(text="P"), Snippet(text="N"))
    merged, deleted, _ = store.merge_with_parent(w.id, n.id)
    restored = store.restore_node(w.id, deleted[0])
    assert set(restored) == {p.id, n.id}
    assert store.get_node(w.id, p.id).children == [n.id]
    assert store.get_thread_content(w.id, n.id) == "RPN"
    # M coexists after undo (documented), as a second child of R
    assert store.get_node(w.id, r.id).children == [p.id, merged.id]


def test_undo_in_place_merge_with_children_keeps_children_under_merged(store):
    w = store.create_weave()
    _, p, n = chain(store, w.id, Snippet(text="R"), Snippet(text="P"), Snippet(text="N"))
    c1 = store.add_node(w.id, Snippet(text="c1"), parent_id=n.id)
    merged, deleted, _ = store.merge_with_parent(w.id, n.id)
    store.restore_node(w.id, deleted[0])
    # the originals are back, but child migration (an edge edit, like split)
    # is not reversed: c1 stays under M
    assert store.get_node(w.id, n.id).children == []
    assert store.get_node(w.id, merged.id).children == [c1.id]
    assert store.get_thread_content(w.id, c1.id) == "RPNc1"


def test_undo_sibling_merge_restores_node_under_parent(store):
    w = store.create_weave()
    _, p = chain(store, w.id, Snippet(text="R"), Snippet(text="P"))
    n = store.add_node(w.id, Snippet(text="N"), parent_id=p.id)
    s = store.add_node(w.id, Snippet(text="S"), parent_id=p.id)
    merged, deleted, _ = store.merge_with_parent(w.id, n.id)
    assert deleted == [n.id]
    store.restore_node(w.id, n.id)
    assert store.get_node(w.id, p.id).children == [n.id, s.id]
    assert store.get_node(w.id, merged.id).text == "PN"  # M coexists


def test_merge_event_payload(store):
    w = store.create_weave()
    _, p, n = chain(store, w.id, Snippet(text="R"), Snippet(text="P"), Snippet(text="N"))
    merged, deleted, _ = store.merge_with_parent(w.id, n.id)
    events = store.get_events(weave_id=w.id)
    (merge_event,) = [e for e in events if e["type"] == "node_merged"]
    assert merge_event["payload"]["node_id"] == n.id
    assert merge_event["payload"]["parent_id"] == p.id
    assert merge_event["payload"]["merged_node_id"] == merged.id
    assert merge_event["payload"]["deleted_node_ids"] == deleted
    assert merge_event["payload"]["in_place"] is True


# ------------------------------------------------------------ REST endpoint


@pytest.fixture
def client(tmp_path):
    store = WeaveStore(tmp_path / "merge-api.sqlite")
    with TestClient(create_app(store, ColoomConfig())) as c:
        yield c
    store.close()


def api_chain(client, *texts):
    wid = client.post("/weaves", json={"title": "m"}).json()["id"]
    ids, parent = [], None
    for text in texts:
        resp = client.post(
            f"/weaves/{wid}/nodes", json={"text": text, "parent_id": parent}
        )
        assert resp.status_code == 201
        parent = resp.json()["id"]
        ids.append(parent)
    return wid, ids


def test_merge_endpoint_response_shape(client):
    wid, (r, p, n) = api_chain(client, "R", "P", "N")
    client.put(f"/weaves/{wid}/cursors/me", json={"node_id": n})
    resp = client.post(
        f"/weaves/{wid}/nodes/{n}/merge-with-parent",
        headers={"X-Coloom-Client": "tab-1"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["merged_node_id"] == body["merged_node"]["id"]
    assert body["merged_node"]["content"] == {"type": "snippet", "text": "PN"}
    assert body["deleted_node_ids"] == [p, n]
    assert body["moved_cursors"] == [
        {"name": "me", "from": n, "to": body["merged_node_id"]}
    ]
    # event on the feed, origin-stamped for echo absorption
    events = client.get(f"/events?weave_id={wid}").json()["events"]
    (merge_event,) = [e for e in events if e["type"] == "node_merged"]
    assert merge_event["payload"]["merged_node_id"] == body["merged_node_id"]
    assert merge_event["payload"]["origin"] == "tab-1"
    # undo via the existing restore path
    restore = client.post(f"/weaves/{wid}/nodes/{p}/restore")
    assert restore.status_code == 200
    assert set(restore.json()["restored_node_ids"]) == {p, n}


def test_merge_endpoint_root_is_409_and_missing_is_404(client):
    wid, (root,) = api_chain(client, "R")
    assert (
        client.post(f"/weaves/{wid}/nodes/{root}/merge-with-parent").status_code
        == 409
    )
    assert (
        client.post(f"/weaves/{wid}/nodes/nope/merge-with-parent").status_code == 404
    )
