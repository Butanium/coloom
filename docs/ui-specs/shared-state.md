# Tapestry-Loom behavioral spec — shared editor state, keyboard actions, weave ops, color/text encodings

Ground truth: `src/editor/shared/mod.rs` and `src/editor/shared/weave.rs` in
`~/projects2/weird-personas/Tapestry-Loom` (branch `new-format`), plus the modules they delegate
to: `src/settings/shortcuts.rs`, `src/settings/mod.rs`, `src/settings/inference/mod.rs`,
`src/settings/inference/openai.rs`, `tapestry-weave/src/v0.rs`,
`universal-weave/src/dependent/legacy_dependent.rs`, `universal-weave/src/dependent.rs`, and
`Getting Started.md`. All line refs are into the Tapestry-Loom repo.

This file covers the cross-view services every subview consumes. The per-view specs
(canvas.md, lists.md, textedit.md, shell-menus-graph.md) reference the behaviors defined here.

---

## 1. State taxonomy

User-facing taxonomy (Getting Started.md:134-154):

| Class | Contents | Lives in |
|---|---|---|
| Shared + persistent | node contents, parent/child links, ids, active/bookmarked flags, node metadata, weave metadata | the weave file |
| Shared + temporary | hovered position, "cursor" position, collapsed/expanded set, current inference parameters | editor's `SharedState`, lost on close |
| Local + temporary | scroll position, zoom, mouse pointer position, subview positioning/sizes | each subview |

`SharedState` fields (mod.rs:51-76):

| Field | Type | Meaning |
|---|---|---|
| `inference` | `InferenceParameters` | per-editor model list + request counts + recursion depth (reset to settings default per editor, Getting Started.md:39) |
| `cursor_node` / `last_cursor_node` | `NodeIndex` | next-frame write buffer / committed value (see §1.2) |
| `hovered_node` / `last_hovered_node` | `NodeIndex` | same double-buffer for hover |
| `last_changed_node` | `Option<Ulid>` | "the node that changed this frame" — auto-scroll target (see §1.4) |
| `has_cursor_node_changed`, `has_hover_node_changed`, `has_weave_changed`, `has_weave_layout_changed`, `has_opened_changed`, `has_theme_changed` | `bool` | one-frame dirty flags views poll to invalidate caches |
| `opened` | `HashMap<Ulid, bool>` | per-node expand/collapse overrides (see §8) |
| `requests` / `responses` | maps/vecs | in-flight generation requests + arrived results |
| `seriation_requests` / `seriation_responses` | maps/vecs | in-flight seriation (embedding-order) requests |
| `last_activated_hovered` | `bool` | edge detector for the hold-to-activate gesture (§3, ActivateHovered) |

### 1.1 NodeIndex

`NodeIndex` (mod.rs:78-100) is the type of both cursor and hover position:

- `WithinNode(id, byte_offset)` — a caret position *inside* a node's content. The offset is a
  **byte offset into the node's content bytes** (it is fed directly to byte-based `split_node`).
- `Node(id)` — the node as a whole.
- `None` — no position.

### 1.2 Double-buffering (frame protocol)

Views call `set_cursor_node` / `set_hovered_node` (mod.rs:652-657) during a frame; these write
the *next* values. At the end of `SharedState::update` (mod.rs:464-487) the buffers are
committed: `last_* = *`, and `has_*_changed` is set if the value differed. Getters
(`get_cursor_node`, `get_hovered_node`, mod.rs:643-648) always return the **committed**
(previous-frame) value. All shortcut handlers also operate on `last_cursor_node` /
`last_hovered_node`.

**Hover is reset to `None` every frame** (mod.rs:472) — whichever view is hovered must
re-assert it each frame (immediate mode). There is no debounce/timing anywhere: hover state is
exactly "pointer is over the element this frame".

### 1.3 Cursor invariants (mod.rs:473-502)

Enforced every update, in order:

1. If the cursor's node no longer exists in the weave → cursor = `None` (mod.rs:473-477).
2. If cursor is `None` and the weave has an active node → cursor = the active node itself
   (`get_active_thread_first`; the thread is built leaf-first, dependent.rs:679-690, so "first"
   = the active node, not the root) (mod.rs:478-482).
3. If the cursor changed this frame → **every node on the thread root→cursor is force-opened**
   (mod.rs:494-502).

### 1.4 `last_changed_node` resolution (mod.rs:466-487, 523-525)

Per frame, in priority order (later wins):

1. cleared to `None`;
2. if hover changed → the new hovered node;
3. if a generated node arrived and nothing else set it → the new node's id (mod.rs:523-525);
4. if cursor changed → the new cursor node.

Views use this as the auto-scroll/centering target.

### 1.5 Other flags

- `has_opened_changed` is **delayed one frame**: `set_open`/`toggle_open` set
  `next_opened_updated`, which is committed to `has_opened_changed` at the *end* of update
  (mod.rs:503-504, 635-642).
- `has_theme_changed` fires when **any** field of `UISettings` changed (whole-struct `!=`,
  mod.rs:488-493) — UI scale, color toggles, min opacity, sorting mode, all of it.
- `has_weave_changed` / `has_weave_layout_changed` are read-and-clear flags on the weave wrapper
  (weave.rs:183-192): *every* mutating op sets `changed`; structural ops (add/remove/split/
  merge/sort/set_active_content) additionally set `layout_changed`; bookmark and active-status
  changes set only `changed` (weave.rs:194-342). Metadata edits set neither (weave.rs:44).
- If any flag is set, a repaint is requested (mod.rs:619-627).

---

## 2. Active path semantics (single-active-pointer model)

The weave keeps **one global `active` node pointer** (legacy_dependent.rs). The "active thread"
is that node plus its ancestors to the root (dependent.rs:679-690). Rules:

- `set_node_active_status(id, true)` deactivates the previously active node and points `active`
  at `id` (legacy_dependent.rs:309-330). Activating an interior node therefore *truncates* the
  active text to that node's thread.
- `set_node_active_status(id, false)` on the active node clears the pointer entirely (no
  fallback) (legacy_dependent.rs:322-324).
- `add_node` with `active: true` does the same deactivate-then-point (legacy_dependent.rs:286-292).
- Removing the active node (or an ancestor of it — removal is recursive) moves the pointer to
  the removed node's parent; if no parent, pointer becomes `None`
  (legacy_dependent.rs:190-197).
- Merging an active child into its parent transfers activity to the parent
  (legacy_dependent.rs:481-484). Splitting keeps activity (and bookmark) on the **left/prefix**
  half (legacy_dependent.rs:423-432).

**Cursor vs active**: the cursor is a separate, editor-local position. They interact only via
the fallback rule §1.3.2 and via the navigation shortcuts, which set both.

> **coloom divergence**: there is no single active path — each participant has a named cursor
> and the "thread" is derived per-cursor. Everywhere this spec says "set active + set cursor",
> coloom reads "move this participant's cursor"; "active thread" reads "the acting
> participant's cursor thread". The fallback rules (§1.3.1-2, deletion → parent) must apply
> per-cursor and be enforced server-side, for every cursor pointing into a deleted subtree.

---

## 3. Keyboard shortcuts

Source of defaults: shortcuts.rs:54-141. Handlers: mod.rs:174-462 and settings/mod.rs:492-512.
`COMMAND` = Ctrl on Windows/Linux, Cmd on macOS (Getting Started.md:192). Matching is
**exact-modifier** (`matches_exact`, shortcuts.rs:744-765): Ctrl+Shift+Space does *not* trigger
a Ctrl+Space binding. All shortcuts are consume-on-press (fire once per keypress) **except
ActivateHovered**, which is hold-style: it is "pressed" while the exact modifiers match AND
exactly one key is down AND it is the bound key (shortcuts.rs:776-784). All shortcuts are
suppressed while a modal layer is open (shortcuts.rs:456-458). Shortcut→action priority is the
order listed in settings; in the settings view shortcuts rank below other input handlers,
elsewhere above (Getting Started.md:194-196).

`—` = unbound by default (user-configurable).

| Action | Default | Effect (all read the committed `last_cursor_node`/`last_hovered_node`) | Ref |
|---|---|---|---|
| GenerateAtCursor | Cmd/Ctrl+Space | `WithinNode(n, 0)` → generate children of *n's parent* (i.e. siblings of n). `WithinNode(n, i>0)` → split n at byte i, then generate children of n (prefix half) — "complete from the caret". `Node(n)` → generate children of n. `None` → no-op. | mod.rs:174-190 |
| ToggleNodeBookmarked | — | Toggle bookmark flag of cursor node. | mod.rs:192-200 |
| AddChild | — | New empty Snippet node (no model, no metadata) under cursor node; inherits parent's `active` flag; if parent was active, the new node becomes active and the cursor moves to it. **If cursor is `None`: creates a new ROOT, `active=true`, cursor moves to it** (this is how an empty weave gets its first node). | mod.rs:202-239 |
| AddSibling | — | New empty node with same parent as cursor node (root sibling if cursor is a root); inherits cursor node's `active`; becomes cursor if active. No cursor → no-op. | mod.rs:241-263 |
| DeleteCurrent | — | Remove cursor node **and its entire subtree** (removal is always recursive, legacy_dependent.rs:183-206); cursor → parent (stays if root deleted: then fallback §1.3.2 applies). | mod.rs:265-275 |
| DeleteChildren | — | Remove all children (subtrees) of cursor node. | mod.rs:277-288 |
| DeleteSiblings | — | Remove all *other* children of cursor node's parent (other roots if cursor is a root). | mod.rs:290-316 |
| DeleteSiblingsAndCurrent | — | Cursor → parent, then remove all of parent's children including current (or all roots). | mod.rs:318-338 |
| MergeWithParent | — | `merge_with_parent(cursor)` (§7); on success cursor → parent. | mod.rs:340-350 |
| SplitAtCursor | Cmd/Ctrl+S | Only when cursor is `WithinNode(n, i)`: split n at byte i (§6). | mod.rs:352-356 |
| ActivateHovered | — (hold) | **While held**: the hovered node is set active every frame (re-routing the active path live as the pointer moves). **On release** (or when the key state stops matching): cursor jumps to the last hovered node. Implemented via `last_activated_hovered` edge detection. | mod.rs:420-434 |
| MoveToParent | Cmd/Ctrl+← | Cursor → parent AND parent set active (re-routes active path). | mod.rs:358-367 |
| MoveToChild | Cmd/Ctrl+→ | Cursor → **first** child AND child set active. | mod.rs:369-378 |
| MoveToPreviousSibling | Cmd/Ctrl+↑ | Cursor → previous sibling in parent's child order (roots order for roots) AND set active. Clamps at first (saturating_sub; no wrap). | mod.rs:380-398 |
| MoveToNextSibling | Cmd/Ctrl+↓ | Cursor → next sibling AND set active. Past-end → no-op (no wrap). | mod.rs:400-418 |
| ToggleNodeCollapsed | — | Toggle open/collapsed of cursor node. | mod.rs:436-440 |
| CollapseChildren | — | Set every child of cursor node closed. | mod.rs:442-451 |
| ExpandChildren | — | Set every child of cursor node open. | mod.rs:453-462 |
| CollapseAllVisibleInactive / ExpandAllVisible | — | Handled per-view (tree view), not in shared state. | shortcuts.rs:731,733 |
| ResetParameters / ParameterPreset1-10 | presets 1-5: Cmd/Ctrl+1…5 | Reset / apply inference parameter presets (menu subview). | shortcuts.rs:91-115 |
| ToggleColors | — | Toggle `show_model_colors`. | settings/mod.rs:493-495 |
| ToggleColorOverride | — | Toggle `override_model_colors`; on first enable initializes the override color to the theme hyperlink color. | settings/mod.rs:497-503 |
| ToggleProbabilities | — | Toggle `show_token_probabilities` (token shading). | settings/mod.rs:505-507 |
| ToggleAutoScroll | Cmd/Ctrl+D | Toggle `auto_scroll`. | settings/mod.rs:509-511 |
| FitToCursor / FitToWeave | Cmd/Ctrl+9 / Cmd/Ctrl+0 | Camera fit commands consumed by canvas/graph views. | shortcuts.rs:128-135 |
| CloseFocusedTab | Cmd/Ctrl+W | Closes the focused dock tab. | shortcuts.rs:136-139 |

Shift-click variants (implemented in views, listed here for completeness, Getting
Started.md:198-204): shift-clicking a *generation* button marks the **parent** of the generated
nodes active; shift-clicking a *node-creation* button marks the **created** node active.

---

## 4. Color resolution

### 4.1 Node color — `get_node_color` (mod.rs:1049-1070)

```
if !settings.show_model_colors            → None
else if settings.override_model_colors && override_color set:
    node has a model                      → override_color   (uniform "AI" color)
    node has no model (human)             → None
else:
    node.model.metadata["color"] parses as hex → that color
    no model / no color / bad hex         → None
```

`None` falls back at render time to the theme's default widget text color
(`visuals.widgets.inactive.text_color()`, mod.rs:1150). So in Tapestry-Loom **human-authored
nodes are always theme-default-colored**; model nodes carry their model's label color.
Defaults: `show_model_colors=true`, `override=false`, override color initialized to the theme
hyperlink color when first enabled (settings/mod.rs:88-90, 282).

> **coloom divergence**: every node has a `Creator` (Human or Model); humans get their own
> attribution color rather than "no color".

### 4.2 Token probability → opacity — `get_token_color` (mod.rs:1017-1047)

Applies only when `show_token_probabilities` is on AND the token has `probability` metadata;
otherwise the token renders in the node color unchanged.

```
prob_component = 1 − ln(1 / clamp(p, f32::EPSILON, 1)) / 10        # = 1 + ln(p)/10
conf_component = if show_token_confidence && token has confidence && confidence_k:
                     ln(1 / clamp(e^(−confidence), f64::EPSILON, 1)) / (ln(k) + 2)
                     # ≈ min(confidence, 36.04) / (ln(k) + 2)
                 else 1.0
opacity = clamp( min(conf_component, prob_component),
                 minimum_token_opacity/100, 1.0 )
```

Reference points for `prob_component`: p=1.0→1.0, p=0.61→0.95, p=0.37→0.90, p=0.05→0.70,
p≤e^−10≈4.5e−5→0 (then clamped up to the floor). Settings: `minimum_token_opacity` default
65 (slider 20–80, settings/mod.rs:93,297-303); `show_token_confidence` default true.

Application: `change_color_opacity` (mod.rs:1356-1364) takes the node color's RGB (forced
opaque) and sets unmultiplied alpha = opacity. Web equivalent: `rgba(r g b / opacity)`.

---

## 5. Node text rendering

Both renderers emit monospace text with one colored section per token (snippet = one section).
Section byte ranges are clamped to char boundaries (`floor_char_boundary`,
mod.rs:1196-1199) because token boundaries can split UTF-8 sequences.

Invalid UTF-8 is replaced per invalid chunk with **U+001A (SUB)** — a custom lossy decode
(mod.rs:1372-1400), *not* U+FFFD.

### 5.1 `render_node_text_or_first_token_bytes` (mod.rs:1141-1238) — used by tree/list rows

- Tokens: per-token sections, each colored by §4.2 over the node color.
- **Special case** (mod.rs:1168-1192): a node containing exactly one token whose bytes are
  invalid UTF-8 and whose boundaries are unmodified (`original_length` absent or == byte len)
  renders the token's *byte debug representation* (e.g. `[240, 159]`) in the theme's
  `noninteractive` (dimmer) text color — this is how raw byte-level loom tokens display.

### 5.2 `render_node_text_or_empty` (mod.rs:1240-1354) — used where empty nodes must be visible

- Same as above but **no** byte-debug special case; instead, if the rendered text is empty
  (empty snippet, or all tokens empty), renders the literal placeholder **"No text"** in the
  proportional Body font *sized to match the monospace font size* (visually distinct from
  content), colored with the first token's computed color (or the node color if no tokens)
  (mod.rs:1252-1253, 1285-1313, 1319-1351).

### 5.3 Tooltips

- **Node metadata tooltip** (mod.rs:859-898): max width = theme tooltip width; model label in
  the model color (plain if no color); `confidence` rendered as `confidence: x.xx (k = K, n = N)`
  (n omitted if absent; hidden entirely if `confidence_k` absent); all other metadata as
  `key: value` lines except `confidence_k`/`confidence_n`; node creation time derived from the
  ULID, formatted locale-date + 12-hour time (`%x %r`, mod.rs:1366-1370); debug builds append
  the ULID.
- **Token tooltip** (mod.rs:900-915, 979-1015): the token's Rust-debug-quoted string (or byte
  array if invalid UTF-8) in monospace — only shown if boundaries unmodified; then
  `probability: xx.xx%`; `confidence: x.xx (k = K)`; if `original_length != len` a
  warning-colored line `modified_boundaries: true`; `token_id` shown only when boundaries
  unmodified; `model_id`, `confidence_k`, `counterfactual` never shown raw.
- **Counterfactual tooltip** (mod.rs:935-977): if the token has `counterfactual` metadata
  (JSON list of (base64 bytes, metadata), v0.rs:484-507), shows a horizontally scrollable row
  of buttons, one per alternative with `probability`, labeled `"tok"\n(xx.xx%)` monospace.
  Clicking returns the alternative's index to the caller (the text editor performs the swap).

---

## 6. Split

`split_node(id, at)` — `at` is a **byte offset** into the node's content
(legacy_dependent.rs:411-453, v0.rs:27-44, 89-146):

| Aspect | Behavior |
|---|---|
| No-op cases | `at == 0` or `at >= content byte length` → returns false, nothing changes (v0.rs:90-92, 96-98, 106-108) |
| Identity | **Left/prefix keeps the original id**, parent link, `active` and `bookmarked` flags, and gets exactly one child: the new node. Right/suffix gets a **new id generated with the original node's ULID timestamp** (so it time-sorts beside it; weave.rs:221-229), inherits the original's children, `active=false`, `bookmarked=false`. |
| Content metadata | Node-level `metadata` and `model` are **cloned onto both halves** (v0.rs:32-38) — this is what keeps the halves re-mergeable. |
| Token content | Split at a token boundary just partitions the token list. A mid-token split duplicates that token's bytes into both halves and **strips `token_id` from both** pieces (boundaries now modified; v0.rs:121-140). Other per-token metadata (probability, counterfactual, original_length…) is cloned to the left piece and retained on the right piece. |
| Cursor/active | Active/bookmark stay with the prefix; the editor cursor is unaffected unless the caller moves it. |
| `split_out_token(id, i)` | (weave.rs:230-307) isolates token #i into its own node via up to two splits; returns `(prefix_id?, token_node_id, suffix_id?)` — prefix is `None` when i==0, suffix `None` when i is the last token. Used for per-token looming / counterfactual swaps. |

`GenerateAtCursor` and `SplitAtCursor` feed `WithinNode.byte_offset` straight into this.

## 7. Merge

- **Gate** — `is_mergeable_with_parent(id)` (v0.rs:468-478): node has a parent AND the parent
  has **exactly one child** AND node-level `metadata` maps are equal AND `model` fields are
  equal AND content kinds match (Snippet+Snippet or Tokens+Tokens; v0.rs:46-49, 67-74,
  171-182). Views use this to enable/disable merge affordances.
- **Op** — `merge_with_parent(id)` (legacy_dependent.rs:455-502): contents are concatenated
  into the **parent** (parent id survives, child id disappears); parent inherits the child's
  children; if the child was active, the parent becomes active; **the child's bookmark is
  dropped** (legacy_dependent.rs:490) while the parent's is kept. Returns the parent id;
  incompatible contents or multi-child parents are restored unchanged and return None.

## 8. Collapse/open state

- `opened: HashMap<Ulid, bool>` of explicit overrides; unset nodes default to
  `settings.opened_by_default` (default **false** = collapsed) (mod.rs:629-634,
  settings/mod.rs:96).
- `set_open`/`toggle_open` (mod.rs:635-642) take effect immediately for `is_open` queries but
  `has_opened_changed` is observed one frame later (§1.5).
- Cursor movement force-opens the whole thread to the cursor (§1.3.3).
- The map is never garbage-collected; deleted node ids just linger harmlessly.

## 9. Node dedup (on every add)

`TapestryWeave::add_node` (v0.rs:292-326):

1. Insert the node normally (fails if id exists, node has children pre-set, or the parent id
   is missing — legacy_dependent.rs:267-284).
2. Find sibling **duplicates**: siblings (same parent, or fellow roots) whose entire
   `NodeContent` is `==` — content bytes AND token metadata AND node metadata AND model
   (v0.rs:76-80). Same text from a *different model* is NOT a duplicate.
3. If any: the **new node is removed**; if the new node was active, activation goes to the
   duplicate that was already on the previous active thread, else to the first duplicate found.

Generated nodes arrive `active=false`, so for them dedup is pure removal. The UI logs a debug
message when an arriving generated node was deduped away (mod.rs:579-581); no user-visible
notice. (Getting Started.md:186 documents why: identical single-token children collapse.)

## 10. Sibling sorting

A sort runs over the affected sibling set (children of the parent, or roots) **after every
generated node is added** (mod.rs:527-578), per `settings.interface.node_sorting`
(default `Model`, settings/mod.rs:99,153-160):

| Mode | Comparator |
|---|---|
| None | insertion order kept |
| Model | by model label string; nodes **without** a model sort first (`Option` ordering: None < Some) (mod.rs:529-547) |
| Confidence | descending node-metadata `confidence` (parsed f32); if either side lacks it: `a.is_some().cmp(&b.is_some())` — i.e. **missing-confidence nodes sort first** (mod.rs:548-574) |
| Seriation | fire an embedding request (below); on response, sort siblings by their index in the returned order; ids absent from the response sort first (None < Some, mod.rs:589-614) |

Sorting is in-place on the parent's child IndexSet / the roots set, so all views re-layout
(`layout_changed`).

**Seriation request assembly** (`seriate_children`, mod.rs:658-711): for each sibling whose
content is not a single token (single-token nodes are excluded, mod.rs:687-690), the embedded
text is the full thread bytes root→parent **plus** that sibling's bytes. Items are sorted by
node id before sending (inference/mod.rs:814). Response = the ids in seriated order, keyed by
the parent (`None` for roots).

**Manual "sort by id"** (`sort_children_by_id`, mod.rs:712-742, exposed as a view action): by
model label, then single-token nodes before others, then by id (ULID ≈ creation time);
single-token vs single-token keep relative order.

## 11. Generation requests

### 11.1 Assembly (mod.rs:771-809)

- No models configured → synthetic error response "No models loaded"; no client → "Client is
  not initialized" (both surface as toasts).
- Prompt content = the thread **root→parent**, one `TokensOrBytes` segment per node (order is
  reversed from the leaf-first thread, mod.rs:783-793). `parent=None` (root generation) sends
  an empty prompt.
- Fan-out: for each model in `inference.models` × that model's `requests` count → one
  independent async request (inference/mod.rs:661-712). Each response item becomes a node:
  `from = parent` (or a root if the endpoint flagged `response.root`), `active=false`,
  `bookmarked=false`, content + metadata + `model {label, metadata(color)}` from the response,
  **id timestamped from the request's creation ULID** (inference/mod.rs:750-756, 781-796).
- **Recursion** (`recursion_depth`, default 0): when a response set arrives and depth > 0, for
  each response node a new request is spawned with depth−1 and the prompt extended by that
  node's content — before the node is even committed to the weave (ids pre-assigned;
  inference/mod.rs:758-779). Request count grows exponentially (Getting Started.md:182).
- If `documents.store_counterfactual` is off (default), `counterfactual` entries are stripped
  from arriving token metadata before insert (mod.rs:514-520).
- Failures: toast `Inference failed: {error}` + warn log (mod.rs:583-587).
- `get_request_count()` = pending generation + seriation requests (drives the busy indicator);
  `cancel_requests()` drops all pending handles and queued responses (mod.rs:810-818).

### 11.2 Token-exact regeneration (when token IDs are sent instead of text)

Two gates, both must pass per *segment* (= per node on the thread):

1. **Node → `TokensAndBytes`** (mod.rs:821-857): every token in the node has `token_id`
   (u64-parseable) AND `model_id` (ULID) AND `original_length == token byte length`
   (unmodified boundaries). Any token failing → the whole segment degrades to plain bytes.
2. **Segment → raw ids** (inference/mod.rs:1256-1285): every token's `model_id` equals the
   requesting model's `tokenization_identifier` (refreshed whenever the model's settings
   change, Getting Started.md:227). Any mismatch → bytes.

Usage (openai.rs:403-457): only endpoints with `reuse_tokens` + a tokenization endpoint build a
token-id prompt — token-exact segments pass their stored ids verbatim; byte segments are sent
to the tokenization endpoint (cached) and the results are concatenated into one flat id array.
All other endpoints flatten every segment to bytes/text. This is what makes "loom over raw
tokens, including invalid-UTF-8 ones" reproduce exactly.

## 12. Edge cases checklist

- Empty weave: cursor `None`; AddChild creates an active root (§3). The text editor's
  `set_active_content` (v0.rs:334-442) also creates nodes from typed text by diffing against
  the active thread — splitting at the first divergent byte and appending a Snippet node —
  and prunes a now-empty trailing human node when safe (≤1 child, grandchildless, unbookmarked,
  model-less, same metadata; v0.rs:405-422).
- Deleting any node deletes its whole subtree; cursor and active pointer fall back to the
  parent (`None` for roots).
- Sibling navigation never wraps; MoveToChild always picks child index 0.
- Split is a no-op at offset 0 / past the end; mergeability requires single-child parent +
  identical metadata/model + same content kind; merge silently no-ops otherwise.
- Single-token nodes: excluded from seriation; sorted to the front in sort-by-id; the
  invalid-UTF-8 byte-debug rendering only applies to single-token nodes; dedup is the reason
  the docs recommend 1 request per model when doing max_tokens=1 logprob looming.
- A node deduped on arrival looks like nothing happened (debug log only).
- `modified_boundaries` (original_length ≠ len) suppresses token text + token_id in tooltips
  and disqualifies token-exact regeneration.

## 13. Web/Svelte translation notes (one line each)

| egui mechanic | Web equivalent |
|---|---|
| Hover reset to None every frame, re-asserted by hovered view (mod.rs:472) | pointerenter sets hovered, pointerleave clears it; no polling, no debounce to replicate |
| Double-buffered cursor/hover + per-frame `has_*_changed` flags | single `$state` source of truth; views react via `$derived`/`$effect` — drop the flags entirely |
| `last_changed_node` one-frame signal for auto-scroll | an event-like store `{nodeId, seq}` consumed by scroll effects (seq forces re-trigger on same node) |
| `ctx.request_repaint()` | unnecessary — Svelte reactivity repaints |
| `Promise::spawn_async` polled per frame in `update` | server-side generation; results arrive as WS `node_added` events; busy count from server presence/gen events |
| `consume_shortcut` exact-modifier matching | window keydown handler comparing `{ctrl/meta, alt, shift}` exactly; preventDefault on match |
| ActivateHovered hold-gesture (`is_shortcut_pressed` + release edge) | keydown sets a `holdActive` flag (continuously applying hover→cursor), keyup commits cursor to last hovered |
| Theme via `Visuals` + `text_color()` fallbacks | CSS custom properties; "no color" = `var(--text-default)` |
| ULID timestamp for tooltip time + split-id generation | coloom nodes carry `created_at` server-side; server mints split ids |
| Modal-open shortcut suppression | check for open dialog/focused input before handling shortcuts |

**coloom model divergences to respect when reusing this spec:**

- No single active path: "set active + move cursor" → "move my cursor" (REST), broadcast via
  WS; ActivateHovered becomes "hold to drag my cursor along hover". Deletion fallback
  (cursor→parent) must run server-side for **every** participant cursor in the deleted subtree.
- Node color: creator attribution (human vs model, per-participant) colors every node; the
  Tapestry rule "human = theme default" does not carry over, but the override/uniform mode and
  the token-opacity formula (§4.2) do.
- All weave ops (split, merge, dedup, sorting on generation) are server-canonical: the client
  sends the op, the server applies §6/§7/§9/§10 semantics and emits events; clients never
  mutate locally-first.
- Dedup-on-add and post-generation sibling sorting happen at insert time on the server, so the
  WS event order already reflects final child ordering (or emit an explicit reorder event).
