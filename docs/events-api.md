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

## Generation retries (`gen_retrying`)

Upstream `/v1/completions` calls retry on **transient** failures only — httpx
transport errors (connect/read timeouts, refused connections) and HTTP
408/429/5xx — with exponential backoff + jitter (0.5s · 2^attempt, capped at
8s). Non-transient statuses (400/401/403/404/422, …) fail immediately, zero
retries. Default budget: **5 retries** (6 upstream attempts), server-wide;
override with a `retries` key anywhere in the params merge chain
(template/generator/request params, later wins; `retries: 0` disables). It is
a **coloom-internal param**: stripped from the body before it goes upstream
(and from the node's `creator.raw_request`).

Each retry attempt emits a weave-scoped event *before* the backoff sleep:

```
gen_retrying {gen_id, requester, node_id, generator, generator_id, n,
              attempt: 1-based, max: <retry budget>, error: <short reason>}
```

Same `gen_id` (and the rest of the `gen_started` payload) as the generation
it belongs to, so clients can label in-flight placeholders "retrying k/max".
Feed ordering is always `gen_started` < `gen_retrying`* < `gen_finished`. The
terminal outcome is unchanged: success or exhaustion/non-transient failure
ends in the usual `gen_finished` (`node_ids` vs `error`), and the HTTP
response stays 201/502.

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

## Merge with parent

```
POST /weaves/{wid}/nodes/{N}/merge-with-parent  →  200
{
  merged_node_id:   string,
  merged_node:      Node,          // convenience: M without a refetch
  deleted_node_ids: string[],      // deletion-op root first (DELETE shape)
  moved_cursors:    [{name, from, to}, ...]
}
```

Non-destructive: a **new** node M is created with content concat(P, N) —
Tokens+Tokens keeps every token (logprobs intact, both halves were sampled
against this exact prefix); any Snippet involved degrades to a Snippet of the
concatenated text. M carries `metadata.merged_from: [P_id, N_id]` and P's
creator. N's children migrate to M (edge re-point, the same mechanic as
split). Then:

- **N was P's only live child** ("in place"): M hangs under P's parent (a new
  root if P was one); P **and** N are soft-deleted in one deletion op rooted
  at P → `deleted_node_ids: [P, N]`. Cursors sitting on P or N move to M
  (their thread content is unchanged); M inherits a bookmark from either.
- **P has other live children** ("sibling"): M becomes a new sibling of P
  under P's parent; only N is absorbed → `deleted_node_ids: [N]`. P, its
  other children, and their cursors/bookmarks are untouched; cursors on N
  move to M; M inherits N's bookmark only. (An already-soft-deleted sibling
  doesn't count as a live child — it keeps its own earlier deletion op.)

Errors: merging a root → **409** (no parent); unknown/deleted node → **404**.
Event: `node_merged {node_id: N, parent_id: P, merged_node_id, deleted_node_ids,
in_place: bool}`, origin-stamped as usual. CLI: `coloom merge <node_id>`.

### Undo semantics (binding, mind the asymmetry)

`POST .../nodes/{deleted_node_ids[0]}/restore` brings the absorbed node(s)
back via the normal restore path. But **child migration is an edge edit (like
split) and is NOT reversed**: N's former children stay under M, and M
coexists with the restored originals. Consequences for undo UIs:

- N was a leaf → restore yields the exact original shape, plus M lingering as
  a sibling (or extra root). Deleting M then is safe and completes the undo.
- N had children → after restore, N is back but childless; the children live
  under M. Do **not** delete M here — that would cascade-soft-delete the
  migrated children. Leave M in place.

