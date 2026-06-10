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
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

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
from coloom.setups import ModelSetup, SamplerSetup
from pydantic import TypeAdapter

_CONTENT = TypeAdapter(NodeContent)
_CREATOR = TypeAdapter(Creator)

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
    metadata   TEXT NOT NULL DEFAULT '{}'
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
"""

# Additive column migrations for databases created before the column existed.
# (CREATE TABLE IF NOT EXISTS won't touch an existing table.)
_MIGRATIONS: list[tuple[str, str, str]] = [
    # (table, column, ALTER statement)
    ("profiles", "active",
     "ALTER TABLE profiles ADD COLUMN active INTEGER NOT NULL DEFAULT 1"),
]


class WeaveStoreError(Exception):
    pass


class NotFound(WeaveStoreError):
    pass


class Conflict(WeaveStoreError):
    """A mutation is blocked by a referential constraint (e.g. deleting a model
    setup still referenced by a sampler)."""

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
            "SELECT * FROM nodes WHERE weave_id = ?", (weave_id,)
        ).fetchall()
        edge_rows = self._conn.execute(
            "SELECT parent_id, child_id FROM edges WHERE weave_id = ?"
            " ORDER BY parent_id, position",
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
                "SELECT * FROM nodes WHERE id = ? AND weave_id = ?", (node_id, weave_id)
            ).fetchone()
            if row is None:
                raise NotFound(f"node {node_id!r} not found in weave {weave_id!r}")
            parent_rows = self._conn.execute(
                "SELECT parent_id FROM edges WHERE child_id = ? ORDER BY position",
                (node_id,),
            ).fetchall()
            child_rows = self._conn.execute(
                "SELECT child_id FROM edges WHERE parent_id = ? ORDER BY position",
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

    def remove_node(self, weave_id: str, node_id: str) -> list[str]:
        """Remove a node and its whole subtree. Returns removed node ids.
        Cursors pointing into the doomed subtree relocate to the removed node's
        parent (or are deleted if it was a root)."""
        with self._tx() as c:
            node = self.get_node(weave_id, node_id)
            doomed = self._collect_subtree(weave_id, node_id)
            stranded = [
                cur for cur in self.list_cursors(weave_id).values()
                if cur.node_id in set(doomed)
            ]
            qmarks = ",".join("?" * len(doomed))
            c.execute(
                f"DELETE FROM edges WHERE weave_id = ? AND"
                f" (parent_id IN ({qmarks}) OR child_id IN ({qmarks}))",
                (weave_id, *doomed, *doomed),
            )
            c.execute(f"DELETE FROM nodes WHERE id IN ({qmarks})", tuple(doomed))
            refuge = node.parents[0] if node.parents else None
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
            self._log_event(c, weave_id, "node_removed", {"node_ids": doomed})
        return doomed

    def _collect_subtree(self, weave_id: str, node_id: str) -> list[str]:
        out, stack = [], [node_id]
        seen = set()
        while stack:
            nid = stack.pop()
            if nid in seen:
                continue
            seen.add(nid)
            out.append(nid)
            rows = self._conn.execute(
                "SELECT child_id FROM edges WHERE parent_id = ?", (nid,)
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

    # ------------------------------------------------------------ setups

    def create_model_setup(self, setup: ModelSetup) -> ModelSetup:
        with self._tx() as c:
            c.execute(
                "INSERT INTO model_setups (id, name, base_url, api_key, api_key_env,"
                " model, params) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    setup.id,
                    setup.name,
                    setup.base_url,
                    setup.api_key,
                    setup.api_key_env,
                    setup.model,
                    json.dumps(setup.params),
                ),
            )
        return setup

    def get_model_setup(self, setup_id: str) -> ModelSetup:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM model_setups WHERE id = ?", (setup_id,)
            ).fetchone()
        if row is None:
            raise NotFound(f"model setup {setup_id!r} not found")
        return self._row_to_model_setup(row)

    def list_model_setups(self) -> list[ModelSetup]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM model_setups ORDER BY name, id"
            ).fetchall()
        return [self._row_to_model_setup(r) for r in rows]

    def update_model_setup(
        self, setup_id: str, fields: dict[str, Any]
    ) -> ModelSetup:
        """Partial update: only keys present in `fields` change. Re-validates the
        api_key/api_key_env mutual exclusion on the merged result."""
        with self._tx() as c:
            current = self.get_model_setup(setup_id)
            merged = current.model_copy(update=fields)
            ModelSetup.model_validate(merged.model_dump())  # re-run validators
            c.execute(
                "UPDATE model_setups SET name = ?, base_url = ?, api_key = ?,"
                " api_key_env = ?, model = ?, params = ? WHERE id = ?",
                (
                    merged.name,
                    merged.base_url,
                    merged.api_key,
                    merged.api_key_env,
                    merged.model,
                    json.dumps(merged.params),
                    setup_id,
                ),
            )
        return merged

    def delete_model_setup(self, setup_id: str) -> None:
        with self._tx() as c:
            self.get_model_setup(setup_id)
            (refs,) = c.execute(
                "SELECT COUNT(*) FROM sampler_setups WHERE model_setup_id = ?",
                (setup_id,),
            ).fetchone()
            if refs:
                raise Conflict(
                    f"model setup {setup_id!r} is referenced by {refs} sampler(s)"
                )
            c.execute("DELETE FROM model_setups WHERE id = ?", (setup_id,))

    def create_sampler_setup(self, setup: SamplerSetup) -> SamplerSetup:
        with self._tx() as c:
            self.get_model_setup(setup.model_setup_id)  # validate the reference
            c.execute(
                "INSERT INTO sampler_setups (id, name, model_setup_id, params)"
                " VALUES (?, ?, ?, ?)",
                (
                    setup.id,
                    setup.name,
                    setup.model_setup_id,
                    json.dumps(setup.params),
                ),
            )
        return setup

    def get_sampler_setup(self, setup_id: str) -> SamplerSetup:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sampler_setups WHERE id = ?", (setup_id,)
            ).fetchone()
        if row is None:
            raise NotFound(f"sampler setup {setup_id!r} not found")
        return self._row_to_sampler_setup(row)

    def list_sampler_setups(self) -> list[SamplerSetup]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sampler_setups ORDER BY name, id"
            ).fetchall()
        return [self._row_to_sampler_setup(r) for r in rows]

    def update_sampler_setup(
        self, setup_id: str, fields: dict[str, Any]
    ) -> SamplerSetup:
        with self._tx() as c:
            current = self.get_sampler_setup(setup_id)
            merged = current.model_copy(update=fields)
            if "model_setup_id" in fields:
                self.get_model_setup(merged.model_setup_id)  # validate the reference
            c.execute(
                "UPDATE sampler_setups SET name = ?, model_setup_id = ?, params = ?"
                " WHERE id = ?",
                (
                    merged.name,
                    merged.model_setup_id,
                    json.dumps(merged.params),
                    setup_id,
                ),
            )
        return merged

    def delete_sampler_setup(self, setup_id: str) -> None:
        with self._tx() as c:
            self.get_sampler_setup(setup_id)
            c.execute("DELETE FROM sampler_setups WHERE id = ?", (setup_id,))

    @staticmethod
    def _row_to_model_setup(row: sqlite3.Row) -> ModelSetup:
        return ModelSetup(
            id=row["id"],
            name=row["name"],
            base_url=row["base_url"],
            api_key=row["api_key"],
            api_key_env=row["api_key_env"],
            model=row["model"],
            params=json.loads(row["params"]),
        )

    @staticmethod
    def _row_to_sampler_setup(row: sqlite3.Row) -> SamplerSetup:
        return SamplerSetup(
            id=row["id"],
            name=row["name"],
            model_setup_id=row["model_setup_id"],
            params=json.loads(row["params"]),
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
        """Events with seq > since, oldest first. `weave_id=None` = all weaves."""
        with self._lock:
            if weave_id is None:
                rows = self._conn.execute(
                    "SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?",
                    (since, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM events WHERE weave_id = ? AND seq > ?"
                    " ORDER BY seq LIMIT ?",
                    (weave_id, since, limit),
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
