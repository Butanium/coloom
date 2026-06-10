"""Round-trip tests for the SQLite weave store."""

import pytest
from coloom.models import (
    HumanCreator,
    ModelCreator,
    Snippet,
    Token,
    Tokens,
)
from coloom.store import NotFound, WeaveStore, WeaveStoreError


@pytest.fixture
def store(tmp_path):
    s = WeaveStore(tmp_path / "test.sqlite")
    yield s
    s.close()


def make_tokens(*texts: str) -> Tokens:
    return Tokens(tokens=[Token(text=t, logprob=-0.5) for t in texts])


def test_create_and_list_weaves(store):
    info = store.create_weave(title="My weave", description="desc")
    assert store.get_weave_info(info.id).title == "My weave"
    assert [w.id for w in store.list_weaves()] == [info.id]


def test_get_missing_raises(store):
    with pytest.raises(NotFound):
        store.get_weave_info("nope")
    w = store.create_weave()
    with pytest.raises(NotFound):
        store.get_node(w.id, "nope")
    with pytest.raises(NotFound):
        store.get_cursor(w.id, "nope")


def test_add_nodes_tree_shape(store):
    w = store.create_weave()
    root = store.add_node(
        w.id, Snippet(text="Once upon a time"), HumanCreator(label="clem")
    )
    a = store.add_node(w.id, make_tokens(" there", " was"), parent_id=root.id)
    b = store.add_node(w.id, Snippet(text=" the end."), parent_id=root.id)

    weave = store.get_weave(w.id)
    assert weave.roots == [root.id]
    assert weave.nodes[root.id].children == [a.id, b.id]  # sibling order preserved
    assert weave.nodes[a.id].parents == [root.id]
    assert weave.nodes[a.id].text == " there was"
    assert weave.nodes[b.id].creator.type == "unknown"
    assert weave.nodes[root.id].creator.label == "clem"


def test_persistence_roundtrip(tmp_path):
    path = tmp_path / "w.sqlite"
    s1 = WeaveStore(path)
    w = s1.create_weave(title="persist")
    root = s1.add_node(
        w.id,
        Tokens(tokens=[Token(text="Hi", logprob=-0.1, token_id=42, entropy=1.2)]),
        ModelCreator(label="gpt4-base", seed=7, raw_request={"prompt": "x"}),
        move_cursor="agent",
    )
    s1.close()

    s2 = WeaveStore(path)
    weave = s2.get_weave(w.id)
    node = weave.nodes[root.id]
    assert node.content.tokens[0].token_id == 42
    assert node.content.tokens[0].entropy == 1.2
    assert node.creator.seed == 7
    assert node.creator.raw_request == {"prompt": "x"}
    assert weave.cursors["agent"].node_id == root.id
    s2.close()


def test_cursors_and_threads(store):
    w = store.create_weave()
    root = store.add_node(w.id, Snippet(text="a"))
    mid = store.add_node(w.id, Snippet(text="b"), parent_id=root.id)
    leaf = store.add_node(w.id, Snippet(text="c"), parent_id=mid.id)
    sibling = store.add_node(w.id, Snippet(text="z"), parent_id=root.id)

    cur = store.set_cursor(w.id, "clem", leaf.id)
    assert cur.node_id == leaf.id
    thread = store.get_cursor_thread(w.id, "clem")
    assert [n.id for n in thread] == [root.id, mid.id, leaf.id]
    assert "".join(n.text for n in thread) == "abc"

    # two independent cursors coexist
    store.set_cursor(w.id, "agent", sibling.id)
    assert set(store.list_cursors(w.id)) == {"clem", "agent"}
    agent_thread = store.get_cursor_thread(w.id, "agent")
    assert "".join(n.text for n in agent_thread) == "az"

    # anyone can move anyone's cursor; moved_by records who
    moved = store.set_cursor(w.id, "clem", sibling.id, moved_by="agent")
    assert moved.moved_by == "agent"
    assert store.get_cursor(w.id, "clem").node_id == sibling.id

    store.delete_cursor(w.id, "agent")
    assert set(store.list_cursors(w.id)) == {"clem"}
    with pytest.raises(NotFound):
        store.get_cursor(w.id, "agent")


def test_set_cursor_requires_existing_node(store):
    w = store.create_weave()
    with pytest.raises(NotFound):
        store.set_cursor(w.id, "clem", "nope")


def test_add_node_move_cursor(store):
    w = store.create_weave()
    root = store.add_node(w.id, Snippet(text="a"), move_cursor="clem")
    child = store.add_node(
        w.id, Snippet(text="b"), parent_id=root.id, move_cursor="clem"
    )
    assert store.get_cursor(w.id, "clem").node_id == child.id


def test_remove_node_cascades_and_relocates_cursors(store):
    w = store.create_weave()
    root = store.add_node(w.id, Snippet(text="a"))
    mid = store.add_node(w.id, Snippet(text="b"), parent_id=root.id)
    leaf = store.add_node(w.id, Snippet(text="c"), parent_id=mid.id)
    store.set_cursor(w.id, "clem", leaf.id)

    removed = store.remove_node(w.id, mid.id)
    assert set(removed) == {mid.id, leaf.id}
    weave = store.get_weave(w.id)
    assert set(weave.nodes) == {root.id}
    # cursor relocated to the removed subtree's parent
    assert weave.cursors["clem"].node_id == root.id
    assert weave.nodes[root.id].children == []

    # removing a root deletes cursors pointing into its subtree
    removed = store.remove_node(w.id, root.id)
    assert set(removed) == {root.id}
    assert store.list_cursors(w.id) == {}


def test_split_snippet(store):
    w = store.create_weave()
    root = store.add_node(w.id, Snippet(text="hello world"))
    child = store.add_node(w.id, Snippet(text="!"), parent_id=root.id)
    store.set_cursor(w.id, "clem", child.id)
    store.set_cursor(w.id, "agent", root.id)

    head, tail = store.split_node(w.id, root.id, 5)
    assert head.id == root.id and head.text == "hello"
    assert tail.text == " world"
    assert head.children == [tail.id]
    assert tail.children == [child.id]
    # cursor on a deeper node is untouched; its thread now includes the tail
    clem_thread = store.get_cursor_thread(w.id, "clem")
    assert [n.id for n in clem_thread] == [root.id, tail.id, child.id]
    assert "".join(n.text for n in clem_thread) == "hello world!"
    # cursor on the split node moved to the tail (thread content unchanged)
    assert store.get_cursor(w.id, "agent").node_id == tail.id


def test_split_tokens_at_token_boundary(store):
    w = store.create_weave()
    node = store.add_node(w.id, make_tokens("a", "b", "c"))
    head, tail = store.split_node(w.id, node.id, 2)
    assert [t.text for t in head.content.tokens] == ["a", "b"]
    assert [t.text for t in tail.content.tokens] == ["c"]
    assert tail.content.tokens[0].logprob == -0.5  # logprobs preserved


def test_split_out_of_range(store):
    w = store.create_weave()
    node = store.add_node(w.id, Snippet(text="ab"))
    with pytest.raises(WeaveStoreError):
        store.split_node(w.id, node.id, 0)
    with pytest.raises(WeaveStoreError):
        store.split_node(w.id, node.id, 2)


def test_bookmarks(store):
    w = store.create_weave()
    n = store.add_node(w.id, Snippet(text="x"))
    store.set_bookmarked(w.id, n.id, True)
    assert store.get_weave(w.id).bookmarks == [n.id]
    store.set_bookmarked(w.id, n.id, False)
    assert store.get_weave(w.id).bookmarks == []


def test_multiple_roots(store):
    w = store.create_weave()
    r1 = store.add_node(w.id, Snippet(text="one"))
    r2 = store.add_node(w.id, Snippet(text="two"))
    assert store.get_weave(w.id).roots == [r1.id, r2.id]


def test_events_log_and_cursor(store):
    w = store.create_weave()
    n = store.add_node(w.id, Snippet(text="x"), move_cursor="clem")
    events = store.get_events(w.id)
    types = [e["type"] for e in events]
    assert types == ["weave_created", "node_added", "cursor_moved"]
    assert events[1]["payload"]["node_id"] == n.id
    assert events[2]["payload"] == {
        "name": "clem",
        "node_id": n.id,
        "moved_by": "clem",
    }

    cursor = events[-1]["seq"]
    store.set_bookmarked(w.id, n.id, True)
    newer = store.get_events(w.id, since=cursor)
    assert [e["type"] for e in newer] == ["node_updated"]


def test_concurrent_readers_never_see_torn_state(tmp_path):
    """Regression: reads used to bypass the lock and observe other threads'
    uncommitted transactions on the shared connection (torn snapshots,
    sqlite3.InterfaceError). Writer churns add/add/remove while readers
    assert snapshot consistency."""
    import threading

    store = WeaveStore(tmp_path / "stress.sqlite")
    wid = store.create_weave().id
    keeper = store.add_node(wid, Snippet(text="keeper"))
    errors: list[Exception] = []
    stop = threading.Event()

    def writer():
        try:
            for i in range(80):
                root = store.add_node(wid, Snippet(text=f"r{i}"))
                store.add_node(
                    wid, Snippet(text="c"), parent_id=root.id, move_cursor="w"
                )
                store.remove_node(wid, root.id)
        except Exception as e:  # surfaced via `errors`, not swallowed
            errors.append(e)
        finally:
            stop.set()

    def reader():
        try:
            while not stop.is_set():
                weave = store.get_weave(wid)
                for node in weave.nodes.values():
                    for cid in node.children:
                        assert cid in weave.nodes, "child edge points to missing node"
                    for pid in node.parents:
                        assert pid in weave.nodes, "parent edge points to missing node"
                for cur in weave.cursors.values():
                    assert cur.node_id in weave.nodes, (
                        "cursor references missing node"
                    )
                store.get_events(weave_id=wid)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer)] + [
        threading.Thread(target=reader) for _ in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    assert keeper.id in store.get_weave(wid).nodes
    store.close()


def test_weave_json_export(store):
    w = store.create_weave(title="exp")
    store.add_node(w.id, make_tokens("a"), ModelCreator(label="m"), move_cursor="m")
    snapshot = store.get_weave(w.id)
    as_json = snapshot.model_dump_json()
    from coloom.models import Weave

    again = Weave.model_validate_json(as_json)
    assert again == snapshot


def test_event_origin_stamping(store):
    """current_origin (request-scoped) is stamped into event payloads; without
    it, payloads carry no origin key (CLI/tests/old clients)."""
    from coloom.store import current_origin

    w = store.create_weave(title="origins")  # logged with no origin set
    token = current_origin.set("tab-abc123")
    try:
        node = store.add_node(w.id, Snippet(text="hi"), HumanCreator(label="c"),
                              move_cursor="c")
    finally:
        current_origin.reset(token)
    store.set_cursor(w.id, "c", node.id)  # back to no origin

    events = store.get_events(w.id)
    by_type = {}
    for e in events:
        by_type.setdefault(e["type"], []).append(e)
    assert "origin" not in by_type["weave_created"][0]["payload"]
    assert by_type["node_added"][0]["payload"]["origin"] == "tab-abc123"
    # the cursor upsert INSIDE add_node shares the request scope
    assert by_type["cursor_moved"][0]["payload"]["origin"] == "tab-abc123"
    # the later set_cursor ran outside the scope
    assert "origin" not in by_type["cursor_moved"][1]["payload"]
