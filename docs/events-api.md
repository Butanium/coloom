# Events, global activity & node soft-delete/restore (API contract)

Backend contract for the change feed and the undo-supporting node lifecycle.
Written 2026-06-10 (team coloom-gen, backend); binding for clients. The
generators-specific event types are specced in
[`generators-api.md`](generators-api.md) — this doc owns the general feed
mechanics they ride on.

## The change feed

Every mutation appends a row to the `events` table:

```
{seq: int, weave_id: str, type: str, payload: object, created: iso8601}
```

- `seq` is a global, monotonically increasing cursor across ALL weaves.
- `payload.origin` is the round-4 `X-Coloom-Client` echo-absorption id
  (absent when the mutating request didn't send the header).
- **Scopes**: `weave_id` is the owning weave, or `""` (the global scope
  sentinel) for server-global events (template/generator mutations). No real
  weave id can be empty.

### Polling

```
GET /events?since=<seq>&weave_id=<wid>&limit=<n>
  → {events: Event[], cursor: <seq of last event, or `since` when empty>}
```

- `weave_id` given → that weave's events **plus** global-scope (`""`) rows.
- **`weave_id` omitted → ALL events across every weave** (each row carries
  its `weave_id`, so a global feed can label entries). This is the global
  activity feed for polling clients.

### WebSocket

```
/ws?weave_id=<wid>   → live events for that weave + global-scope rows
/ws                  → live events for EVERYTHING (the global subscription)
```

Same filter semantics as polling. A client showing the activity feed's
**global toggle** can either (a) keep one unfiltered `/ws` connection and
filter client-side per the toggle, or (b) open a second unfiltered socket
next to the weave-scoped one. (a) is recommended: one socket, and the
weave-scoped view is just `event.weave_id === currentWeave || weave_id === ""`.
Per-subscriber queues are bounded (512); a stalled consumer is dropped and
should resync via `GET /events?since=`.

To render "in weave X" labels, resolve `weave_id` against `GET /weaves`
(title lookup); the server deliberately doesn't denormalize titles into
event rows.

## Node soft-delete & restore (frontend undo)

Nothing is ever destroyed: `DELETE` on a node marks its subtree with a
deletion marker; rows and edges stay in the database and all reads
(weave snapshots, node GETs, threads, bookmarks) exclude marked nodes.
The only hard deletion is `DELETE /weaves/{wid}` (whole-weave).

### Delete

```
DELETE /weaves/{wid}/nodes/{id}  →  200
{
  deleted_node_ids: string[],   // the whole live subtree, deletion root first
  moved_cursors:   [{name: string, from: string, to: string|null}, ...],
  removed:         string[]     // legacy alias of deleted_node_ids (pre-soft-delete clients)
}
```

- The subtree walk stops at already-deleted boundaries: nodes deleted by an
  *earlier* op keep their own op marker and are NOT re-listed.
- Cursors sitting in the deleted subtree relocate to the deletion root's
  parent (`to: <parent-id>`); if the deletion root is a weave root, the
  cursor is removed (`to: null`, plus the usual `cursor_removed` event).
  `from` is the node the cursor sat on — the server keeps restore
  cursor-neutral, but a client can opt to put cursors back on undo
  (`PUT /weaves/{wid}/cursors/{name}` with the stored `from`).
- Deleting an already-deleted node → **404** (it's invisible).
- Event: `node_removed {node_ids}` (same type as the old hard delete —
  clients keep working unchanged; it now *means* soft-deleted).

### Restore

```
POST /weaves/{wid}/nodes/{id}/restore  →  200
{restored_node_ids: string[]}
```

Un-deletes by **deletion op**, not by raw subtree: restoring undoes the op
that deleted `id`, plus any deleted-*ancestor* ops on the path to the root
(so the restored subtree is always reachable — no orphan islands). Nodes
deleted by a separate, earlier op *nested inside* the restored subtree stay
deleted — undo layering holds:

```
delete B (inside A) → delete A → restore A   ⇒ A back, B still deleted
delete B (inside A) → delete A → restore B   ⇒ both ops undone (B must be reachable)
```

- Restore on a live node → **409**; unknown id → **404**.
- Edge order under the parent is preserved exactly (edges were never
  removed).
- Cursors do **not** move back on restore — undo restores the tree, not
  your position.
- Event: `node_restored {node_ids}`, `origin`-stamped like every event, so
  optimistic clients can absorb their own echo.
- Typical undo: `Ctrl+Z` after a delete → `POST .../nodes/{deletion
  root}/restore`; `restored_node_ids` will equal the delete's
  `deleted_node_ids`.

### Storage note

`nodes.deleted TEXT` (additive migration): `NULL` = live, else the id of the
node whose DELETE removed this row (the deletion-op root). CLI:
`coloom rm <node_id>` / `coloom restore <node_id>`.
