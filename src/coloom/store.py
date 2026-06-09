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

_CONTENT = TypeAdapter(NodeContent)
_CREATOR = TypeAdapter(Creator)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS weaves (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created     TEXT NOT NULL,
    active_path TEXT NOT NULL DEFAULT '[]',
    metadata    TEXT NOT NULL DEFAULT '{}'
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
"""


class WeaveStoreError(Exception):
    pass


class NotFound(WeaveStoreError):
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
                "INSERT INTO weaves (id, title, description, created, active_path, metadata)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    info.id,
                    info.title,
                    info.description,
                    info.created.isoformat(),
                    json.dumps(info.active_path),
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
            c.execute("DELETE FROM edges WHERE weave_id = ?", (weave_id,))
            c.execute("DELETE FROM nodes WHERE weave_id = ?", (weave_id,))
            c.execute("DELETE FROM weaves WHERE id = ?", (weave_id,))
            self._log_event(c, weave_id, "weave_deleted", {})

    def get_weave(self, weave_id: str) -> Weave:
        """Full snapshot: nodes with linked edges, roots, active path, bookmarks."""
        with self._lock:
            info = self.get_weave_info(weave_id)
            nodes = {n.id: n for n in self._load_nodes(weave_id)}
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
            active_path=info.active_path,
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
            active_path=json.loads(row["active_path"]),
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
        set_active: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> Node:
        """Add a node under `parent_id` (or as a new root if None)."""
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
            if set_active:
                path = self._path_to_root(weave_id, node.id)
                self._set_active_path(c, weave_id, path)
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
        """Remove a node and its whole subtree. Returns removed node ids."""
        with self._tx() as c:
            self.get_node(weave_id, node_id)
            doomed = self._collect_subtree(weave_id, node_id)
            info = self.get_weave_info(weave_id)
            qmarks = ",".join("?" * len(doomed))
            c.execute(
                f"DELETE FROM edges WHERE weave_id = ? AND"
                f" (parent_id IN ({qmarks}) OR child_id IN ({qmarks}))",
                (weave_id, *doomed, *doomed),
            )
            c.execute(f"DELETE FROM nodes WHERE id IN ({qmarks})", tuple(doomed))
            if any(n in info.active_path for n in doomed):
                cut = min(
                    info.active_path.index(n) for n in doomed if n in info.active_path
                )
                self._set_active_path(c, weave_id, info.active_path[:cut])
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

    def split_node(self, weave_id: str, node_id: str, at: int) -> tuple[Node, Node]:
        """Split a node's content at `at` (token index for Tokens, char offset for
        Snippet). The original node keeps the head (its id, parents, bookmarks stay);
        a new node takes the tail and inherits the children. Returns (head, tail)."""
        with self._tx() as c:
            node = self.get_node(weave_id, node_id)
            head_content, tail_content = self._split_content(node.content, at)
            tail = Node(
                content=tail_content, creator=node.creator, metadata=dict(node.metadata)
            )
            info = self.get_weave_info(weave_id)
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
            if node_id in info.active_path:
                i = info.active_path.index(node_id)
                new_path = (
                    info.active_path[: i + 1] + [tail.id] + info.active_path[i + 1 :]
                )
                self._set_active_path(c, weave_id, new_path)
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

    # ------------------------------------------------------------ active path

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

    def _set_active_path(
        self, c: sqlite3.Connection, weave_id: str, path: list[str]
    ) -> None:
        c.execute(
            "UPDATE weaves SET active_path = ? WHERE id = ?",
            (json.dumps(path), weave_id),
        )
        self._log_event(c, weave_id, "active_changed", {"active_path": path})

    def set_active(self, weave_id: str, node_id: str | None) -> list[str]:
        """Set the active path to root→node (None clears it). Returns the path."""
        with self._tx() as c:
            path: list[str] = []
            if node_id is not None:
                self.get_node(weave_id, node_id)
                path = self._path_to_root(weave_id, node_id)
            else:
                self.get_weave_info(weave_id)
            self._set_active_path(c, weave_id, path)
        return path

    def get_active_thread(self, weave_id: str) -> list[Node]:
        with self._lock:
            info = self.get_weave_info(weave_id)
            return [self.get_node(weave_id, nid) for nid in info.active_path]

    def get_active_content(self, weave_id: str) -> str:
        return "".join(n.text for n in self.get_active_thread(weave_id))

    def get_thread_content(self, weave_id: str, node_id: str) -> str:
        """Concatenated text along root→node (first-parent walk)."""
        with self._lock:
            self.get_node(weave_id, node_id)
            path = self._path_to_root(weave_id, node_id)
            return "".join(self.get_node(weave_id, nid).text for nid in path)

    # ------------------------------------------------------------ events

    def _log_event(
        self, c: sqlite3.Connection, weave_id: str, type_: str, payload: dict[str, Any]
    ) -> int:
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
