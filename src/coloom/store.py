"""SQLite-backed weave store.

The store is the canonical state: every mutation is a transaction. One database
file can hold many weaves. Edges live in their own table so multi-parent (DAG)
nodes need no schema migration later; the API enforces tree-shape for now.

Thread-safety: one shared connection guarded by an RLock. SQLite isolation is
per-connection, so an unlocked read would see other threads' *uncommitted*
transactions (FastAPI runs sync endpoints in a threadpool) — every public read
therefore takes the lock too, and mutators do their reads inside the
transaction.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from coloom.generators import Generator, ParentRef, ResolvedGenerator, Template, resolve_chain
from coloom.models import (
    Creator,
    Cursor,
    Node,
    NodeContent,
    Snippet,
    Tokens,
    UnknownCreator,
    Weave,
    WeaveInfo,
    utcnow,
)
from pydantic import TypeAdapter

logger = logging.getLogger("coloom.store")

_CONTENT = TypeAdapter(NodeContent)
_CREATOR = TypeAdapter(Creator)

# Request-scoped mutation origin: the per-tab client id (X-Coloom-Client header),
# set by the server middleware around each request. Stamped into every logged
# event's payload as `origin`, so clients can tell their own mutations' echoes
# from remote changes (None = origin unknown: CLI, tests, old clients).
current_origin: ContextVar[str | None] = ContextVar("coloom_origin", default=None)

# Request-scoped actor profile (X-Coloom-Profile header): who is performing the
# mutation. Stamped as `by` into template/generator events so the activity feed
# can say "clément edited template gpt4-base".
current_profile: ContextVar[str | None] = ContextVar("coloom_profile", default=None)

# Template/generator events are global, not weave-scoped: they are logged under
# this sentinel weave_id (weave ids are uuid hex, never empty), broadcast to ALL
# WS subscribers regardless of their weave filter, and included in weave-filtered
# `GET /events` queries so activity feeds see them.
GLOBAL_EVENT_SCOPE = ""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS weaves (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created     TEXT NOT NULL,
    metadata    TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS cursors (
    weave_id TEXT NOT NULL REFERENCES weaves(id) ON DELETE CASCADE,
    name     TEXT NOT NULL,
    node_id  TEXT NOT NULL,
    updated  TEXT NOT NULL,
    moved_by TEXT,
    PRIMARY KEY (weave_id, name)
);
CREATE TABLE IF NOT EXISTS nodes (
    id         TEXT PRIMARY KEY,
    weave_id   TEXT NOT NULL REFERENCES weaves(id) ON DELETE CASCADE,
    content    TEXT NOT NULL,
    creator    TEXT NOT NULL,
    created    TEXT NOT NULL,
    modified   TEXT NOT NULL,
    bookmarked INTEGER NOT NULL DEFAULT 0,
    metadata   TEXT NOT NULL DEFAULT '{}',
    -- soft-delete marker: NULL = live, else the id of the node whose DELETE
    -- removed this one (the "deletion op" root). Rows + edges stay in the db;
    -- reads exclude them. Powers POST .../restore (frontend undo).
    deleted    TEXT
);
CREATE INDEX IF NOT EXISTS idx_nodes_weave ON nodes(weave_id);
CREATE TABLE IF NOT EXISTS edges (
    weave_id  TEXT NOT NULL,
    parent_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    child_id  TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    position  INTEGER NOT NULL,
    PRIMARY KEY (parent_id, child_id)
);
CREATE INDEX IF NOT EXISTS idx_edges_child ON edges(child_id);
CREATE INDEX IF NOT EXISTS idx_edges_weave ON edges(weave_id);
CREATE TABLE IF NOT EXISTS events (
    seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    weave_id TEXT NOT NULL,
    type     TEXT NOT NULL,
    payload  TEXT NOT NULL DEFAULT '{}',
    created  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_weave ON events(weave_id, seq);
CREATE TABLE IF NOT EXISTS model_setups (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    base_url    TEXT NOT NULL,
    api_key     TEXT,
    api_key_env TEXT,
    model       TEXT NOT NULL,
    params      TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS sampler_setups (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    model_setup_id TEXT NOT NULL,
    params         TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS profiles (
    name     TEXT PRIMARY KEY,
    settings TEXT NOT NULL DEFAULT '{}',
    created  TEXT NOT NULL,
    updated  TEXT NOT NULL,
    active   INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS templates (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    builtin     INTEGER NOT NULL DEFAULT 0,
    base_url    TEXT NOT NULL,
    model       TEXT NOT NULL,
    api_key     TEXT,
    api_key_env TEXT,
    params      TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS generators (
    id            TEXT PRIMARY KEY,
    profile       TEXT NOT NULL,
    name          TEXT NOT NULL,
    parent_kind   TEXT,
    parent_id     TEXT,
    base_url      TEXT,
    model         TEXT,
    api_key       TEXT,
    api_key_env   TEXT,
    params        TEXT NOT NULL DEFAULT '{}',
    migrated_from TEXT
);
CREATE INDEX IF NOT EXISTS idx_generators_profile ON generators(profile);
-- (profile, builtin-template) pairs that were seeded once: a deleted seeded
-- generator must NOT resurrect on the next boot/login re-seed.
CREATE TABLE IF NOT EXISTS generator_seeds (
    profile     TEXT NOT NULL,
    template_id TEXT NOT NULL,
    PRIMARY KEY (profile, template_id)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Additive column migrations for databases created before the column existed.
# (CREATE TABLE IF NOT EXISTS won't touch an existing table.)
_MIGRATIONS: list[tuple[str, str, str]] = [
    # (table, column, ALTER statement)
    ("profiles", "active",
     "ALTER TABLE profiles ADD COLUMN active INTEGER NOT NULL DEFAULT 1"),
    ("generators", "migrated_from",
     "ALTER TABLE generators ADD COLUMN migrated_from TEXT"),
    ("nodes", "deleted",
     "ALTER TABLE nodes ADD COLUMN deleted TEXT"),
]


class WeaveStoreError(Exception):
    pass


class NotFound(WeaveStoreError):
    pass


class Conflict(WeaveStoreError):
    """A mutation is blocked by a referential constraint."""

    pass


class BadReference(WeaveStoreError):
    """A body reference is invalid: unknown parent, cross-profile parent,
    inheritance cycle. Maps to HTTP 400."""

    pass


class Forbidden(WeaveStoreError):
    """Mutation of a read-only row (builtin template). Maps to HTTP 403."""

    pass


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


class WeaveStore:
    def __init__(self, db_path: str | Path = ":memory:"):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.RLock()
        with self._tx():
            self._conn.executescript(_SCHEMA)
            for table, column, alter in _MIGRATIONS:
                cols = {
                    r["name"]
                    for r in self._conn.execute(f"PRAGMA table_info({table})")
                }
                if column not in cols:
                    self._conn.execute(alter)
        self._migrate_setups_to_generators()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    # ------------------------------------------------------------ weaves

    def create_weave(
        self,
        title: str = "Untitled weave",
        description: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> WeaveInfo:
        info = WeaveInfo(title=title, description=description, metadata=metadata or {})
        with self._tx() as c:
            c.execute(
                "INSERT INTO weaves (id, title, description, created, metadata)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    info.id,
                    info.title,
                    info.description,
                    info.created.isoformat(),
                    json.dumps(info.metadata),
                ),
            )
            self._log_event(c, info.id, "weave_created", {"title": info.title})
        return info

    def list_weaves(self) -> list[WeaveInfo]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM weaves ORDER BY created"
            ).fetchall()
            return [self._row_to_info(r) for r in rows]

    def get_weave_info(self, weave_id: str) -> WeaveInfo:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM weaves WHERE id = ?", (weave_id,)
            ).fetchone()
        if row is None:
            raise NotFound(f"weave {weave_id!r} not found")
        return self._row_to_info(row)

    def delete_weave(self, weave_id: str) -> None:
        with self._tx() as c:
            self.get_weave_info(weave_id)
            c.execute("DELETE FROM cursors WHERE weave_id = ?", (weave_id,))
            c.execute("DELETE FROM edges WHERE weave_id = ?", (weave_id,))
            c.execute("DELETE FROM nodes WHERE weave_id = ?", (weave_id,))
            c.execute("DELETE FROM weaves WHERE id = ?", (weave_id,))
            self._log_event(c, weave_id, "weave_deleted", {})

    def update_weave_info(
        self,
        weave_id: str,
        title: str | None = None,
        description: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> WeaveInfo:
        """Partial update of weave-level fields (None = leave unchanged)."""
        with self._tx() as c:
            info = self.get_weave_info(weave_id)
            if title is not None:
                info.title = title
            if description is not None:
                info.description = description
            if metadata is not None:
                info.metadata = metadata
            c.execute(
                "UPDATE weaves SET title = ?, description = ?, metadata = ?"
                " WHERE id = ?",
                (info.title, info.description, json.dumps(info.metadata), weave_id),
            )
            self._log_event(
                c, weave_id, "weave_updated", {"title": info.title}
            )
        return info

    def log_activity(
        self, weave_id: str, type_: str, payload: dict[str, Any]
    ) -> None:
        """Log a non-mutation event into the change feed (e.g. gen_started) —
        powers presence indicators and the activity timeline."""
        with self._tx() as c:
            self.get_weave_info(weave_id)
            self._log_event(c, weave_id, type_, payload)

    def get_weave(self, weave_id: str) -> Weave:
        """Full snapshot: nodes with linked edges, roots, cursors, bookmarks."""
        with self._lock:
            info = self.get_weave_info(weave_id)
            nodes = {n.id: n for n in self._load_nodes(weave_id)}
            cursors = self.list_cursors(weave_id)
        roots = [
            n.id
            for n in sorted(nodes.values(), key=lambda n: (n.created, n.id))
            if not n.parents
        ]
        bookmarks = [n.id for n in nodes.values() if n.bookmarked]
        return Weave(
            id=info.id,
            title=info.title,
            description=info.description,
            created=info.created,
            nodes=nodes,
            roots=roots,
            cursors=cursors,
            bookmarks=bookmarks,
            metadata=info.metadata,
        )

    @staticmethod
    def _row_to_info(row: sqlite3.Row) -> WeaveInfo:
        return WeaveInfo(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            created=_dt(row["created"]),
            metadata=json.loads(row["metadata"]),
        )

    # ------------------------------------------------------------ nodes

    def _load_nodes(self, weave_id: str) -> list[Node]:
        node_rows = self._conn.execute(
            "SELECT * FROM nodes WHERE weave_id = ? AND deleted IS NULL",
            (weave_id,),
        ).fetchall()
        # exclude edges touching soft-deleted nodes (a live parent may have a
        # deleted child at a deletion boundary; the reverse can't happen — a
        # deleted node's descendants are all deleted)
        edge_rows = self._conn.execute(
            "SELECT e.parent_id, e.child_id FROM edges e"
            " JOIN nodes pn ON pn.id = e.parent_id"
            " JOIN nodes cn ON cn.id = e.child_id"
            " WHERE e.weave_id = ? AND pn.deleted IS NULL AND cn.deleted IS NULL"
            " ORDER BY e.parent_id, e.position",
            (weave_id,),
        ).fetchall()
        parents: dict[str, list[str]] = {}
        children: dict[str, list[str]] = {}
        for e in edge_rows:
            children.setdefault(e["parent_id"], []).append(e["child_id"])
            parents.setdefault(e["child_id"], []).append(e["parent_id"])
        return [self._row_to_node(r, parents, children) for r in node_rows]

    @staticmethod
    def _row_to_node(
        row: sqlite3.Row,
        parents: dict[str, list[str]],
        children: dict[str, list[str]],
    ) -> Node:
        return Node(
            id=row["id"],
            parents=parents.get(row["id"], []),
            children=children.get(row["id"], []),
            content=_CONTENT.validate_json(row["content"]),
            creator=_CREATOR.validate_json(row["creator"]),
            created=_dt(row["created"]),
            modified=_dt(row["modified"]),
            bookmarked=bool(row["bookmarked"]),
            metadata=json.loads(row["metadata"]),
        )

    def get_node(self, weave_id: str, node_id: str) -> Node:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM nodes WHERE id = ? AND weave_id = ?"
                " AND deleted IS NULL",
                (node_id, weave_id),
            ).fetchone()
            if row is None:
                raise NotFound(f"node {node_id!r} not found in weave {weave_id!r}")
            parent_rows = self._conn.execute(
                "SELECT e.parent_id FROM edges e JOIN nodes n ON n.id = e.parent_id"
                " WHERE e.child_id = ? AND n.deleted IS NULL ORDER BY e.position",
                (node_id,),
            ).fetchall()
            child_rows = self._conn.execute(
                "SELECT e.child_id FROM edges e JOIN nodes n ON n.id = e.child_id"
                " WHERE e.parent_id = ? AND n.deleted IS NULL ORDER BY e.position",
                (node_id,),
            ).fetchall()
        return self._row_to_node(
            row,
            {node_id: [r["parent_id"] for r in parent_rows]},
            {node_id: [r["child_id"] for r in child_rows]},
        )

    def add_node(
        self,
        weave_id: str,
        content: NodeContent,
        creator: Creator | None = None,
        parent_id: str | None = None,
        move_cursor: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Node:
        """Add a node under `parent_id` (or as a new root if None).
        `move_cursor` (a cursor name) moves that cursor to the new node."""
        node = Node(
            content=content,
            creator=creator or UnknownCreator(),
            parents=[parent_id] if parent_id else [],
            metadata=metadata or {},
        )
        with self._tx() as c:
            self.get_weave_info(weave_id)
            if parent_id is not None:
                self.get_node(weave_id, parent_id)
            self._insert_node(c, weave_id, node)
            if parent_id is not None:
                self._insert_edge(c, weave_id, parent_id, node.id)
            self._log_event(
                c,
                weave_id,
                "node_added",
                {"node_id": node.id, "parent_id": parent_id},
            )
            if move_cursor is not None:
                self._upsert_cursor(c, weave_id, move_cursor, node.id, move_cursor)
        return node

    def _insert_node(self, c: sqlite3.Connection, weave_id: str, node: Node) -> None:
        c.execute(
            "INSERT INTO nodes (id, weave_id, content, creator, created, modified,"
            " bookmarked, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                node.id,
                weave_id,
                node.content.model_dump_json(),
                node.creator.model_dump_json(),
                node.created.isoformat(),
                node.modified.isoformat(),
                int(node.bookmarked),
                json.dumps(node.metadata),
            ),
        )

    def _insert_edge(
        self, c: sqlite3.Connection, weave_id: str, parent_id: str, child_id: str
    ) -> None:
        (pos,) = c.execute(
            "SELECT COALESCE(MAX(position) + 1, 0) FROM edges WHERE parent_id = ?",
            (parent_id,),
        ).fetchone()
        c.execute(
            "INSERT INTO edges (weave_id, parent_id, child_id, position)"
            " VALUES (?, ?, ?, ?)",
            (weave_id, parent_id, child_id, pos),
        )

    def remove_node(
        self, weave_id: str, node_id: str
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """SOFT-delete a node and its whole (live) subtree: rows and edges stay
        in the db, marked with the deletion-op root id; reads exclude them.
        Restorable via `restore_node` (frontend undo). Returns
        (deleted node ids, root first; cursor relocations as {name, from, to} —
        `from` lets a client opt to put cursors back on undo).
        Cursors pointing into the doomed subtree relocate to the removed node's
        parent (or are deleted if it was a root → `to: None`)."""
        with self._tx() as c:
            node = self.get_node(weave_id, node_id)  # 404s if already deleted
            doomed = self._collect_subtree(weave_id, node_id)
            stranded = [
                cur for cur in self.list_cursors(weave_id).values()
                if cur.node_id in set(doomed)
            ]
            qmarks = ",".join("?" * len(doomed))
            c.execute(
                f"UPDATE nodes SET deleted = ? WHERE id IN ({qmarks})",
                (node_id, *doomed),
            )
            refuge = node.parents[0] if node.parents else None
            moved: list[dict[str, Any]] = []
            for cur in stranded:
                if refuge is None:
                    c.execute(
                        "DELETE FROM cursors WHERE weave_id = ? AND name = ?",
                        (weave_id, cur.name),
                    )
                    self._log_event(
                        c, weave_id, "cursor_removed", {"name": cur.name}
                    )
                else:
                    self._upsert_cursor(c, weave_id, cur.name, refuge, None)
                moved.append({"name": cur.name, "from": cur.node_id, "to": refuge})
            self._log_event(c, weave_id, "node_removed", {"node_ids": doomed})
        return doomed, moved

    def restore_node(self, weave_id: str, node_id: str) -> list[str]:
        """Un-soft-delete: undoes the deletion op(s) needed to make `node_id`
        visible again — its own op plus every deleted ancestor's op on the
        first-parent path to the root (so the restored subtree is reachable,
        never an orphan island). Nodes deleted by *other* ops nested inside
        (deleted earlier, separately) stay deleted — undo layering holds.
        Cursors do NOT move back. Returns the restored node ids."""
        with self._tx() as c:
            row = c.execute(
                "SELECT deleted FROM nodes WHERE id = ? AND weave_id = ?",
                (node_id, weave_id),
            ).fetchone()
            if row is None:
                raise NotFound(f"node {node_id!r} not found in weave {weave_id!r}")
            if row["deleted"] is None:
                raise Conflict(f"node {node_id!r} is not deleted")
            ops: set[str] = set()
            current: str | None = node_id
            seen: set[str] = set()
            while current is not None and current not in seen:
                seen.add(current)
                r = c.execute(
                    "SELECT deleted FROM nodes WHERE id = ?", (current,)
                ).fetchone()
                if r is not None and r["deleted"] is not None:
                    ops.add(r["deleted"])
                pr = c.execute(
                    "SELECT parent_id FROM edges WHERE child_id = ?"
                    " ORDER BY position LIMIT 1",
                    (current,),
                ).fetchone()
                current = pr["parent_id"] if pr is not None else None
            qmarks = ",".join("?" * len(ops))
            restored = [
                r["id"]
                for r in c.execute(
                    f"SELECT id FROM nodes WHERE weave_id = ?"
                    f" AND deleted IN ({qmarks})",
                    (weave_id, *ops),
                ).fetchall()
            ]
            assert node_id in restored
            c.execute(
                f"UPDATE nodes SET deleted = NULL WHERE weave_id = ?"
                f" AND deleted IN ({qmarks})",
                (weave_id, *ops),
            )
            self._log_event(c, weave_id, "node_restored", {"node_ids": restored})
        return restored

    def _collect_subtree(self, weave_id: str, node_id: str) -> list[str]:
        """Live (non-deleted) subtree, depth-first, `node_id` first. Stops at
        already-soft-deleted children: they belong to an earlier deletion op."""
        out, stack = [], [node_id]
        seen = set()
        while stack:
            nid = stack.pop()
            if nid in seen:
                continue
            seen.add(nid)
            out.append(nid)
            rows = self._conn.execute(
                "SELECT e.child_id FROM edges e JOIN nodes n ON n.id = e.child_id"
                " WHERE e.parent_id = ? AND n.deleted IS NULL",
                (nid,),
            ).fetchall()
            stack.extend(r["child_id"] for r in rows)
        return out

    def set_bookmarked(self, weave_id: str, node_id: str, bookmarked: bool) -> None:
        self.get_node(weave_id, node_id)
        with self._tx() as c:
            c.execute(
                "UPDATE nodes SET bookmarked = ?, modified = ? WHERE id = ?",
                (int(bookmarked), utcnow().isoformat(), node_id),
            )
            self._log_event(
                c,
                weave_id,
                "node_updated",
                {"node_id": node_id, "bookmarked": bookmarked},
            )

    def update_node_content(
        self,
        weave_id: str,
        node_id: str,
        content: NodeContent,
        metadata: dict[str, Any] | None = None,
    ) -> Node:
        """Replace a node's content wholesale (free-form thread editing: the
        keystroke-coalescing path mutates one node instead of chaining new
        ones). `metadata`, when given, replaces the node's metadata too."""
        self.get_node(weave_id, node_id)  # validates weave + node
        with self._tx() as c:
            if metadata is not None:
                c.execute(
                    "UPDATE nodes SET content = ?, metadata = ?, modified = ?"
                    " WHERE id = ?",
                    (
                        content.model_dump_json(),
                        json.dumps(metadata),
                        utcnow().isoformat(),
                        node_id,
                    ),
                )
            else:
                c.execute(
                    "UPDATE nodes SET content = ?, modified = ? WHERE id = ?",
                    (content.model_dump_json(), utcnow().isoformat(), node_id),
                )
            self._log_event(
                c,
                weave_id,
                "node_updated",
                {"node_id": node_id, "content_changed": True},
            )
        return self.get_node(weave_id, node_id)

    def split_node(self, weave_id: str, node_id: str, at: int) -> tuple[Node, Node]:
        """Split a node's content at `at` (token index for Tokens, char offset for
        Snippet). The original node keeps the head (its id, parents, bookmarks stay);
        a new node takes the tail and inherits the children. Returns (head, tail).
        Cursors on the split node move to the tail so their thread content is
        unchanged."""
        with self._tx() as c:
            node = self.get_node(weave_id, node_id)
            head_content, tail_content = self._split_content(node.content, at)
            tail = Node(
                content=tail_content, creator=node.creator, metadata=dict(node.metadata)
            )
            c.execute(
                "UPDATE nodes SET content = ?, modified = ? WHERE id = ?",
                (head_content.model_dump_json(), utcnow().isoformat(), node_id),
            )
            self._insert_node(c, weave_id, tail)
            # move children: re-point their edges from head to tail
            c.execute(
                "UPDATE edges SET parent_id = ? WHERE parent_id = ?",
                (tail.id, node_id),
            )
            self._insert_edge(c, weave_id, node_id, tail.id)
            for cur in self.list_cursors(weave_id).values():
                if cur.node_id == node_id:
                    self._upsert_cursor(c, weave_id, cur.name, tail.id, None)
            self._log_event(
                c,
                weave_id,
                "node_split",
                {"node_id": node_id, "new_node_id": tail.id, "at": at},
            )
            return self.get_node(weave_id, node_id), self.get_node(weave_id, tail.id)

    @staticmethod
    def _split_content(
        content: NodeContent, at: int
    ) -> tuple[NodeContent, NodeContent]:
        """Token index for Tokens, char offset for Snippet; both halves non-empty."""
        if isinstance(content, Tokens):
            if not 0 < at < len(content.tokens):
                raise WeaveStoreError(
                    f"split index {at} out of range (1..{len(content.tokens) - 1})"
                )
            return Tokens(tokens=content.tokens[:at]), Tokens(
                tokens=content.tokens[at:]
            )
        if not 0 < at < len(content.text):
            raise WeaveStoreError(
                f"split offset {at} out of range (1..{len(content.text) - 1})"
            )
        return Snippet(text=content.text[:at]), Snippet(text=content.text[at:])

    @staticmethod
    def _concat_content(a: NodeContent, b: NodeContent) -> NodeContent:
        """Tokens+Tokens keeps every token (logprobs intact — both halves were
        produced against this exact prefix); any Snippet involved degrades the
        pair to a Snippet of the concatenated text."""
        if isinstance(a, Tokens) and isinstance(b, Tokens):
            return Tokens(tokens=[*a.tokens, *b.tokens])
        return Snippet(text=a.text + b.text)

    def merge_with_parent(
        self, weave_id: str, node_id: str
    ) -> tuple[Node, list[str], list[dict[str, Any]]]:
        """Merge node N into its (first) parent P, non-destructively: a NEW
        node M takes content concat(P, N) and N's children (edges re-pointed,
        the split_node mechanic); N is soft-deleted (absorbed). When N was P's
        only live child, P is absorbed too (deletion-op root P, so one
        `restore_node(P)` undoes the whole pair) and M reads as an in-place
        merge under P's parent; otherwise M is a new sibling of P and P + the
        other children stay untouched. Cursors (and bookmark flags) on absorbed
        nodes migrate to M — their thread content is unchanged.

        Undo semantics (documented in docs/events-api.md): restoring the
        deletion root brings the absorbed nodes back, but child migration is an
        edge edit (like split) and is NOT reversed — N's old children stay
        under M, and M coexists with the restored originals.

        Returns (merged node, deleted node ids [root first], moved cursors as
        {name, from, to} — the DELETE response vocabulary)."""
        with self._tx() as c:
            node = self.get_node(weave_id, node_id)
            if not node.parents:
                raise Conflict(
                    f"node {node_id!r} is a root — there is no parent to merge with"
                )
            parent = self.get_node(weave_id, node.parents[0])
            in_place = parent.children == [node_id]
            merged = Node(
                content=self._concat_content(parent.content, node.content),
                creator=parent.creator,
                bookmarked=(parent.bookmarked if in_place else False)
                or node.bookmarked,
                metadata={"merged_from": [parent.id, node.id]},
            )
            self._insert_node(c, weave_id, merged)
            grandparent = parent.parents[0] if parent.parents else None
            if grandparent is not None:
                self._insert_edge(c, weave_id, grandparent, merged.id)
            # migrate N's children to M (same edge re-point as split_node)
            c.execute(
                "UPDATE edges SET parent_id = ? WHERE parent_id = ?",
                (merged.id, node_id),
            )
            # absorb: soft-delete N (and P when merging in place); the live
            # subtree under the doom root is exactly the absorbed pair/single
            # now that N's children hang off M
            doom_root = parent.id if in_place else node_id
            doomed = self._collect_subtree(weave_id, doom_root)
            assert set(doomed) == (
                {parent.id, node_id} if in_place else {node_id}
            ), f"unexpected merge subtree {doomed!r}"
            qmarks = ",".join("?" * len(doomed))
            c.execute(
                f"UPDATE nodes SET deleted = ? WHERE id IN ({qmarks})",
                (doom_root, *doomed),
            )
            moved: list[dict[str, Any]] = []
            for cur in self.list_cursors(weave_id).values():
                if cur.node_id in set(doomed):
                    self._upsert_cursor(c, weave_id, cur.name, merged.id, None)
                    moved.append(
                        {"name": cur.name, "from": cur.node_id, "to": merged.id}
                    )
            self._log_event(
                c,
                weave_id,
                "node_merged",
                {
                    "node_id": node_id,
                    "parent_id": parent.id,
                    "merged_node_id": merged.id,
                    "deleted_node_ids": doomed,
                    "in_place": in_place,
                },
            )
            return self.get_node(weave_id, merged.id), doomed, moved

    # ------------------------------------------------------------ cursors

    def _path_to_root(self, weave_id: str, node_id: str) -> list[str]:
        """Walk first-parents up to a root; returns root→node id list."""
        path = [node_id]
        seen = {node_id}
        current = node_id
        while True:
            row = self._conn.execute(
                "SELECT parent_id FROM edges WHERE child_id = ? ORDER BY position LIMIT 1",
                (current,),
            ).fetchone()
            if row is None:
                break
            current = row["parent_id"]
            if current in seen:
                raise WeaveStoreError(f"cycle detected at node {current!r}")
            seen.add(current)
            path.append(current)
        return list(reversed(path))

    def _upsert_cursor(
        self,
        c: sqlite3.Connection,
        weave_id: str,
        name: str,
        node_id: str,
        moved_by: str | None,
    ) -> Cursor:
        cur = Cursor(name=name, node_id=node_id, moved_by=moved_by)
        c.execute(
            "INSERT INTO cursors (weave_id, name, node_id, updated, moved_by)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (weave_id, name) DO UPDATE"
            " SET node_id = excluded.node_id, updated = excluded.updated,"
            " moved_by = excluded.moved_by",
            (weave_id, name, node_id, cur.updated.isoformat(), moved_by),
        )
        self._log_event(
            c,
            weave_id,
            "cursor_moved",
            {"name": name, "node_id": node_id, "moved_by": moved_by},
        )
        return cur

    def set_cursor(
        self, weave_id: str, name: str, node_id: str, moved_by: str | None = None
    ) -> Cursor:
        """Create or move the named cursor to `node_id`. Anyone may move any
        cursor; `moved_by` records who did (for the "look here" gesture)."""
        with self._tx() as c:
            self.get_node(weave_id, node_id)  # validates weave + node
            return self._upsert_cursor(c, weave_id, name, node_id, moved_by)

    def delete_cursor(self, weave_id: str, name: str) -> None:
        with self._tx() as c:
            self.get_cursor(weave_id, name)
            c.execute(
                "DELETE FROM cursors WHERE weave_id = ? AND name = ?",
                (weave_id, name),
            )
            self._log_event(c, weave_id, "cursor_removed", {"name": name})

    def get_cursor(self, weave_id: str, name: str) -> Cursor:
        with self._lock:
            self.get_weave_info(weave_id)
            row = self._conn.execute(
                "SELECT * FROM cursors WHERE weave_id = ? AND name = ?",
                (weave_id, name),
            ).fetchone()
        if row is None:
            raise NotFound(f"cursor {name!r} not found in weave {weave_id!r}")
        return self._row_to_cursor(row)

    def list_cursors(self, weave_id: str) -> dict[str, Cursor]:
        with self._lock:
            self.get_weave_info(weave_id)
            rows = self._conn.execute(
                "SELECT * FROM cursors WHERE weave_id = ? ORDER BY name", (weave_id,)
            ).fetchall()
        return {r["name"]: self._row_to_cursor(r) for r in rows}

    @staticmethod
    def _row_to_cursor(row: sqlite3.Row) -> Cursor:
        return Cursor(
            name=row["name"],
            node_id=row["node_id"],
            updated=_dt(row["updated"]),
            moved_by=row["moved_by"],
        )

    def get_cursor_thread(self, weave_id: str, name: str) -> list[Node]:
        """Nodes along root→cursor for the named cursor."""
        with self._lock:
            cur = self.get_cursor(weave_id, name)
            path = self._path_to_root(weave_id, cur.node_id)
            return [self.get_node(weave_id, nid) for nid in path]

    def get_thread_content(self, weave_id: str, node_id: str) -> str:
        """Concatenated text along root→node (first-parent walk)."""
        with self._lock:
            self.get_node(weave_id, node_id)
            path = self._path_to_root(weave_id, node_id)
            return "".join(self.get_node(weave_id, nid).text for nid in path)

    # ------------------------------------------------------------ templates

    def _insert_template(self, c: sqlite3.Connection, t: Template) -> None:
        c.execute(
            "INSERT INTO templates (id, name, builtin, base_url, model, api_key,"
            " api_key_env, params) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                t.id,
                t.name,
                int(t.builtin),
                t.base_url,
                t.model,
                t.api_key,
                t.api_key_env,
                json.dumps(t.params),
            ),
        )

    @staticmethod
    def _row_to_template(row: sqlite3.Row) -> Template:
        return Template(
            id=row["id"],
            name=row["name"],
            builtin=bool(row["builtin"]),
            base_url=row["base_url"],
            model=row["model"],
            api_key=row["api_key"],
            api_key_env=row["api_key_env"],
            params=json.loads(row["params"]),
        )

    def _template_payload(self, t: Template) -> dict[str, Any]:
        return {"id": t.id, "name": t.name, "by": current_profile.get()}

    def create_template(self, t: Template) -> Template:
        with self._tx() as c:
            self._insert_template(c, t)
            self._log_event(
                c, GLOBAL_EVENT_SCOPE, "template_created", self._template_payload(t)
            )
        return t

    def get_template(self, template_id: str) -> Template:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM templates WHERE id = ?", (template_id,)
            ).fetchone()
        if row is None:
            raise NotFound(f"template {template_id!r} not found")
        return self._row_to_template(row)

    def list_templates(self) -> list[Template]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM templates ORDER BY builtin DESC, name, id"
            ).fetchall()
        return [self._row_to_template(r) for r in rows]

    @staticmethod
    def _merge_params(
        current: dict[str, Any], patch: dict[str, Any] | None
    ) -> dict[str, Any]:
        """PATCH params semantics: per-key merge, a key set to null removes the
        override, `params: null` wholesale clears them all."""
        if patch is None:
            return {}
        merged = {**current, **patch}
        return {k: v for k, v in merged.items() if v is not None}

    def update_template(self, template_id: str, fields: dict[str, Any]) -> Template:
        """Partial update; only keys present in `fields` change. 403 on builtins."""
        with self._tx() as c:
            current = self.get_template(template_id)
            if current.builtin:
                raise Forbidden(f"template {current.name!r} is builtin (read-only)")
            if "params" in fields:
                fields = {
                    **fields,
                    "params": self._merge_params(current.params, fields["params"]),
                }
            merged = current.model_copy(update=fields)
            merged = Template.model_validate(merged.model_dump())  # re-run validators
            c.execute(
                "UPDATE templates SET name = ?, base_url = ?, model = ?, api_key = ?,"
                " api_key_env = ?, params = ? WHERE id = ?",
                (
                    merged.name,
                    merged.base_url,
                    merged.model,
                    merged.api_key,
                    merged.api_key_env,
                    json.dumps(merged.params),
                    template_id,
                ),
            )
            self._log_event(
                c, GLOBAL_EVENT_SCOPE, "template_updated", self._template_payload(merged)
            )
        return merged

    def delete_template(self, template_id: str) -> None:
        """403 on builtins. Generators inheriting from this template are
        FLATTENED first (their resolved fields materialized, parent detached) —
        deleting a parent never changes what a child generates."""
        with self._tx() as c:
            t = self.get_template(template_id)
            if t.builtin:
                raise Forbidden(f"template {t.name!r} is builtin (read-only)")
            self._flatten_children(c, "template", template_id)
            c.execute("DELETE FROM templates WHERE id = ?", (template_id,))
            self._log_event(
                c, GLOBAL_EVENT_SCOPE, "template_deleted", self._template_payload(t)
            )

    def upsert_builtin_template(
        self,
        name: str,
        base_url: str,
        model: str,
        api_key: str | None,
        api_key_env: str | None,
        params: dict[str, Any],
    ) -> Template:
        """Boot-time import of a yaml preset as a builtin template (upsert by
        name, id stable across boots). Silent: boot config sync is not a user
        mutation, no events are logged."""
        with self._tx() as c:
            row = c.execute(
                "SELECT * FROM templates WHERE builtin = 1 AND name = ?", (name,)
            ).fetchone()
            fields = dict(
                name=name,
                builtin=True,
                base_url=base_url,
                model=model,
                api_key=api_key,
                api_key_env=api_key_env,
                params=params,
            )
            if row is None:
                t = Template(**fields)
                self._insert_template(c, t)
            else:
                t = Template(id=row["id"], **fields)
                c.execute(
                    "UPDATE templates SET base_url = ?, model = ?, api_key = ?,"
                    " api_key_env = ?, params = ? WHERE id = ?",
                    (base_url, model, api_key, api_key_env, json.dumps(params), t.id),
                )
        return t

    # ------------------------------------------------------------ generators

    def _insert_generator(self, c: sqlite3.Connection, g: Generator) -> None:
        c.execute(
            "INSERT INTO generators (id, profile, name, parent_kind, parent_id,"
            " base_url, model, api_key, api_key_env, params, migrated_from)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                g.id,
                g.profile,
                g.name,
                g.parent.kind if g.parent else None,
                g.parent.id if g.parent else None,
                g.base_url,
                g.model,
                g.api_key,
                g.api_key_env,
                json.dumps(g.params),
                g.migrated_from,
            ),
        )

    @staticmethod
    def _row_to_generator(row: sqlite3.Row) -> Generator:
        parent = None
        if row["parent_kind"] is not None:
            parent = ParentRef(kind=row["parent_kind"], id=row["parent_id"])
        return Generator(
            id=row["id"],
            profile=row["profile"],
            name=row["name"],
            parent=parent,
            base_url=row["base_url"],
            model=row["model"],
            api_key=row["api_key"],
            api_key_env=row["api_key_env"],
            params=json.loads(row["params"]),
            migrated_from=row["migrated_from"],
        )

    def _generator_payload(self, g: Generator, **extra: Any) -> dict[str, Any]:
        return {
            "id": g.id,
            "name": g.name,
            "profile": g.profile,
            "by": current_profile.get(),
            **extra,
        }

    def _validate_parent(self, gen_id: str, profile: str, parent: ParentRef | None) -> None:
        """Parent must exist; a generator parent must be same-profile; the chain
        from the parent must never reach `gen_id` (cycle). Raises BadReference."""
        if parent is None:
            return
        if parent.kind == "template":
            try:
                self.get_template(parent.id)
            except NotFound as e:
                raise BadReference(str(e))
            return
        try:
            p = self.get_generator(parent.id)
        except NotFound as e:
            raise BadReference(str(e))
        if p.profile != profile:
            raise BadReference(
                f"parent generator {parent.id!r} belongs to profile {p.profile!r},"
                f" not {profile!r}"
            )
        # cycle check: walk up from the parent; hitting gen_id means a loop
        seen = set()
        current: Generator | None = p
        while current is not None:
            if current.id == gen_id or current.id in seen:
                raise BadReference("parent chain would form a cycle")
            seen.add(current.id)
            ref = current.parent
            if ref is None or ref.kind == "template":
                return
            current = self.get_generator(ref.id)

    def _generator_chain(self, g: Generator) -> list[Generator | Template]:
        """Leaf→root chain: the generator, its ancestors, ending at a template
        (if any). Cycles raise (write-time validation should prevent them)."""
        chain: list[Generator | Template] = [g]
        seen = {g.id}
        ref = g.parent
        while ref is not None:
            if ref.kind == "template":
                chain.append(self.get_template(ref.id))
                break
            parent = self.get_generator(ref.id)
            if parent.id in seen:
                raise WeaveStoreError(f"generator cycle at {parent.id!r}")
            seen.add(parent.id)
            chain.append(parent)
            ref = parent.parent
        return chain

    def create_generator(self, g: Generator) -> Generator:
        with self._tx() as c:
            self._validate_parent(g.id, g.profile, g.parent)
            self._insert_generator(c, g)
            self._log_event(
                c, GLOBAL_EVENT_SCOPE, "generator_created", self._generator_payload(g)
            )
        return g

    def get_generator(self, generator_id: str) -> Generator:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM generators WHERE id = ?", (generator_id,)
            ).fetchone()
        if row is None:
            raise NotFound(f"generator {generator_id!r} not found")
        return self._row_to_generator(row)

    def list_generators(self, profile: str) -> list[Generator]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM generators WHERE profile = ? ORDER BY name, id",
                (profile,),
            ).fetchall()
        return [self._row_to_generator(r) for r in rows]

    def _write_generator(self, c: sqlite3.Connection, g: Generator) -> None:
        c.execute(
            "UPDATE generators SET name = ?, parent_kind = ?, parent_id = ?,"
            " base_url = ?, model = ?, api_key = ?, api_key_env = ?, params = ?"
            " WHERE id = ?",
            (
                g.name,
                g.parent.kind if g.parent else None,
                g.parent.id if g.parent else None,
                g.base_url,
                g.model,
                g.api_key,
                g.api_key_env,
                json.dumps(g.params),
                g.id,
            ),
        )

    def update_generator(self, generator_id: str, fields: dict[str, Any]) -> Generator:
        """Partial update; explicit null in `fields` clears a scalar back to
        inherited; params merge per-key (null removes the override)."""
        with self._tx() as c:
            current = self.get_generator(generator_id)
            if "profile" in fields:
                raise BadReference("a generator's profile is immutable")
            if "params" in fields:
                fields = {
                    **fields,
                    "params": self._merge_params(current.params, fields["params"]),
                }
            if "parent" in fields and fields["parent"] is not None:
                fields = {**fields, "parent": ParentRef.model_validate(fields["parent"])}
            merged = current.model_copy(update=fields)
            merged = Generator.model_validate(merged.model_dump())  # re-run validators
            if "parent" in fields:
                self._validate_parent(generator_id, merged.profile, merged.parent)
            self._write_generator(c, merged)
            self._log_event(
                c,
                GLOBAL_EVENT_SCOPE,
                "generator_updated",
                self._generator_payload(merged),
            )
        return merged

    def delete_generator(self, generator_id: str) -> None:
        """Child generators inheriting from this one are FLATTENED first."""
        with self._tx() as c:
            g = self.get_generator(generator_id)
            self._flatten_children(c, "generator", generator_id)
            c.execute("DELETE FROM generators WHERE id = ?", (generator_id,))
            self._log_event(
                c, GLOBAL_EVENT_SCOPE, "generator_deleted", self._generator_payload(g)
            )

    def _flatten_children(
        self, c: sqlite3.Connection, parent_kind: str, parent_id: str
    ) -> None:
        """Materialize the resolved fields of every direct child generator of a
        doomed parent, then detach it (parent = null). Same transaction as the
        delete: behavior is preserved exactly, inheritance is severed."""
        rows = c.execute(
            "SELECT * FROM generators WHERE parent_kind = ? AND parent_id = ?",
            (parent_kind, parent_id),
        ).fetchall()
        for row in rows:
            child = self._row_to_generator(row)
            resolved = resolve_chain(self._generator_chain(child))
            flattened = child.model_copy(
                update={
                    "parent": None,
                    "base_url": resolved.base_url,
                    "model": resolved.model,
                    "api_key": resolved.api_key,
                    "api_key_env": resolved.api_key_env,
                    "params": resolved.params,
                }
            )
            self._write_generator(c, flattened)
            self._log_event(
                c,
                GLOBAL_EVENT_SCOPE,
                "generator_updated",
                self._generator_payload(flattened, flattened_from=parent_kind),
            )

    def resolve_generator(self, generator_id: str) -> ResolvedGenerator:
        with self._lock:
            return resolve_chain(self._generator_chain(self.get_generator(generator_id)))

    # ------------------------------------------------------------ seeding

    def _derived_from_template(self, g: Generator, template_id: str) -> bool:
        return any(
            isinstance(row, Template) and row.id == template_id
            for row in self._generator_chain(g)
        )

    def seed_profile_generators(self, profile: str, log: bool = True) -> list[Generator]:
        """Give `profile` one inheriting generator per builtin template (name =
        template name). Idempotent two ways: a (profile, template) pair is never
        seeded twice (generator_seeds), and a profile that already has a
        generator derived from the template — even renamed — is left alone.
        `log=False` for boot-time seeding (no clients connected, keep the feed
        clean)."""
        created: list[Generator] = []
        with self._tx() as c:
            builtins = [t for t in self.list_templates() if t.builtin]
            if not builtins:
                return created
            seeded = {
                r["template_id"]
                for r in c.execute(
                    "SELECT template_id FROM generator_seeds WHERE profile = ?",
                    (profile,),
                ).fetchall()
            }
            existing = self.list_generators(profile)
            for t in builtins:
                if t.id in seeded:
                    continue
                if any(self._derived_from_template(g, t.id) for g in existing):
                    c.execute(
                        "INSERT OR IGNORE INTO generator_seeds (profile, template_id)"
                        " VALUES (?, ?)",
                        (profile, t.id),
                    )
                    continue
                g = Generator(
                    profile=profile,
                    name=t.name,
                    parent=ParentRef(kind="template", id=t.id),
                )
                self._insert_generator(c, g)
                c.execute(
                    "INSERT OR IGNORE INTO generator_seeds (profile, template_id)"
                    " VALUES (?, ?)",
                    (profile, t.id),
                )
                if log:
                    self._log_event(
                        c,
                        GLOBAL_EVENT_SCOPE,
                        "generator_created",
                        self._generator_payload(g, seeded=True),
                    )
                created.append(g)
        return created

    # ------------------------------------------------------------ setups migration

    def _migrate_setups_to_generators(self) -> None:
        """One-shot, idempotent migration of the legacy two-layer setups into
        templates + generators (docs/generators-api.md §Migration). The old
        tables stay on disk untouched; a meta flag marks completion. Runs
        silently (no events) at store init."""
        with self._tx() as c:
            if c.execute(
                "SELECT 1 FROM meta WHERE key = 'setups_migrated'"
            ).fetchone():
                return
            model_rows = c.execute("SELECT * FROM model_setups").fetchall()
            for r in model_rows:
                # template id = model_setup id (stable, collision-free uuids)
                self._insert_template(
                    c,
                    Template(
                        id=r["id"],
                        name=r["name"],
                        builtin=False,
                        base_url=r["base_url"],
                        model=r["model"],
                        api_key=r["api_key"],
                        api_key_env=r["api_key_env"],
                        params=json.loads(r["params"]),
                    ),
                )
            model_ids = {r["id"] for r in model_rows}
            sampler_rows = c.execute("SELECT * FROM sampler_setups").fetchall()
            profiles = [
                r["name"]
                for r in c.execute(
                    "SELECT name FROM profiles WHERE active = 1 ORDER BY name"
                ).fetchall()
            ]
            for s in sampler_rows:
                if s["model_setup_id"] not in model_ids:
                    logger.warning(
                        "skipping sampler setup %r (%s): dangling model_setup_id %r",
                        s["name"], s["id"], s["model_setup_id"],
                    )
                    continue
                for profile in profiles:
                    self._insert_generator(
                        c,
                        Generator(
                            profile=profile,
                            name=s["name"],
                            parent=ParentRef(kind="template", id=s["model_setup_id"]),
                            params=json.loads(s["params"]),
                            # lets clients map old {kind:'sampler', id} settings
                            # refs (activeGenerators) to this generator exactly
                            migrated_from=s["id"],
                        ),
                    )
            c.execute("INSERT INTO meta (key, value) VALUES ('setups_migrated', '1')")
            if model_rows or sampler_rows:
                logger.info(
                    "migrated %d model setup(s) -> templates, %d sampler setup(s)"
                    " x %d profile(s) -> generators",
                    len(model_rows), len(sampler_rows), len(profiles),
                )

    # ------------------------------------------------------------ profiles
    # Server-stored per-person client settings (keybindings, ui prefs, active
    # generators…) so a profile roams across browsers. Opaque JSON blob.

    def list_profiles(self) -> list[dict[str, Any]]:
        """Active profiles only — soft-deleted ones are hidden, not gone."""
        rows = self._conn.execute(
            "SELECT name, updated FROM profiles WHERE active = 1 ORDER BY name"
        ).fetchall()
        return [{"name": r["name"], "updated": r["updated"]} for r in rows]

    def get_profile(self, name: str) -> dict[str, Any]:
        """Returns soft-deleted profiles too (with active=False): logging in
        with the same name again must find the old settings, never lose them."""
        row = self._conn.execute(
            "SELECT name, settings, created, updated, active FROM profiles"
            " WHERE name = ?",
            (name,),
        ).fetchone()
        if row is None:
            raise NotFound(f"profile {name!r} not found")
        return {
            "name": row["name"],
            "settings": json.loads(row["settings"]),
            "created": row["created"],
            "updated": row["updated"],
            "active": bool(row["active"]),
        }

    def put_profile(self, name: str, settings: dict[str, Any]) -> dict[str, Any]:
        """Create or update; also resurrects a soft-deleted profile."""
        now = utcnow().isoformat()
        with self._tx() as c:
            c.execute(
                "INSERT INTO profiles (name, settings, created, updated, active)"
                " VALUES (?, ?, ?, ?, 1)"
                " ON CONFLICT (name) DO UPDATE"
                " SET settings = excluded.settings, updated = excluded.updated,"
                "     active = 1",
                (name, json.dumps(settings), now, now),
            )
        return self.get_profile(name)

    def delete_profile(self, name: str) -> None:
        """Soft delete: mark inactive (hidden from the list) but keep the row —
        profile settings are never destroyed."""
        now = utcnow().isoformat()
        with self._tx() as c:
            self.get_profile(name)
            c.execute(
                "UPDATE profiles SET active = 0, updated = ? WHERE name = ?",
                (now, name),
            )

    # ------------------------------------------------------------ events

    def _log_event(
        self, c: sqlite3.Connection, weave_id: str, type_: str, payload: dict[str, Any]
    ) -> int:
        origin = current_origin.get()
        if origin is not None:
            payload = {**payload, "origin": origin}
        cur = c.execute(
            "INSERT INTO events (weave_id, type, payload, created) VALUES (?, ?, ?, ?)",
            (weave_id, type_, json.dumps(payload), utcnow().isoformat()),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def last_event_seq(self) -> int:
        with self._lock:
            (seq,) = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM events"
            ).fetchone()
            return seq

    def get_events(
        self, weave_id: str | None = None, since: int = 0, limit: int = 1000
    ) -> list[dict[str, Any]]:
        """Events with seq > since, oldest first. `weave_id=None` = all weaves.
        Weave-filtered queries also include global (template/generator) events,
        which live under the empty-string scope."""
        with self._lock:
            if weave_id is None:
                rows = self._conn.execute(
                    "SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?",
                    (since, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM events WHERE (weave_id = ? OR weave_id = ?)"
                    " AND seq > ? ORDER BY seq LIMIT ?",
                    (weave_id, GLOBAL_EVENT_SCOPE, since, limit),
                ).fetchall()
        return [
            {
                "seq": r["seq"],
                "weave_id": r["weave_id"],
                "type": r["type"],
                "payload": json.loads(r["payload"]),
                "created": r["created"],
            }
            for r in rows
        ]
