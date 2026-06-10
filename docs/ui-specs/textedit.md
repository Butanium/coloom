# Tapestry-Loom Text Editor view — behavioral spec

Source of truth: `Tapestry-Loom/src/editor/textedit.rs` (all bare `textedit.rs:N` refs), plus the
helpers it calls in `src/editor/shared/mod.rs` (`shared.rs:N`), `src/editor/shared/weave.rs`
(`weave.rs:N`), `tapestry-weave/src/v0.rs` (`v0.rs:N`), and
`universal-weave/src/dependent/legacy_dependent.rs` (`dep.rs:N`). User-facing description:
`Getting Started.md` ("Editor of active text within the document. Allows you to quickly change the
cursor position without changing the active nodes").

The view is **one editable multiline text buffer** showing the concatenated bytes of the active
thread (root → leaf). It is not a list of widgets: a single `<textarea>`-like editor with
(a) per-snippet colored highlighting, (b) node-boundary marker rects painted *behind* the text,
(c) per-snippet invisible hover hitboxes laid on top for hover-state + tooltips. Edits to the
buffer are diffed back into the weave wholesale.

---

## 1. Data model of the rendered buffer

Built in `update_contents` (textedit.rs:354–414), from the active thread in **root→leaf order**
(`get_active_thread` walks leaf→root, textedit reverses it; textedit.rs:367–374, dep.rs:252–258).

Per node:

| Node content | Snippets emitted | `token_index` field |
|---|---|---|
| `Snippet(bytes)` | 1 snippet, length = byte len (textedit.rs:379–386) | `None` |
| `Tokens([(bytes, meta)…])` | 1 snippet **per token** (textedit.rs:387–403) | `Some(byte offset of this token's start within the node)` (textedit.rs:388–399) |

A `Snippet` record is `(byte_len, node_id, color, Option<token_start_offset>)` (textedit.rs:47).
Also maintained: `node_snippets: node_id → Vec<document-absolute byte Range>` (one range per
snippet/token; textedit.rs:33, 383–402), used for caret mapping.

**Invalid UTF-8**: the document text is built from the raw bytes; every invalid byte is replaced
1:1 by U+001A SUBSTITUTE (chosen because it is exactly 1 UTF-8 byte, so byte offsets stay aligned;
textedit.rs:49–57, 407–413). On write-back, any U+001A byte at a position within the original
buffer length is replaced by the original byte before diffing (textedit.rs:451–455) — so merely
having the substitution char in the buffer does not count as an edit.

**Rebuild triggers**: the buffer is rebuilt from the weave when it's empty, and it's cleared
(forcing rebuild next frame) whenever the weave changed or the theme/interface settings changed
(textedit.rs:99–102, 117–119). Net effect: after every user edit (which mutates the weave), the
buffer round-trips through the weave on the following frame.

---

## 2. Visual encodings

### 2.1 Node color

`get_node_color` (shared.rs:1049–1070):

```
if !settings.show_model_colors            → None  (use default text color)
else if settings.override_model_colors && override set:
    node has a model → the override color, else None
else → parse node.model.metadata["color"] as hex → that color, else None
```

Fallback (`None`) = theme inactive-widget text color (textedit.rs:118, 134). Human-typed nodes
have `model = None`, so they always render in the default text color.
Defaults: `show_model_colors = true`, `override_model_colors = false` (settings/mod.rs:88–90).

### 2.2 Token opacity (probability shading)

`get_token_color` (shared.rs:1017–1047). Only for `Tokens` nodes, only if
`settings.show_token_probabilities` (default true) and the token has parseable metadata
`probability` p ∈ string. Otherwise token gets the plain node color.

```
conf_term = if show_token_confidence (default true) and metadata has confidence c and confidence_k k:
                ln( 1 / clamp(exp(-c), ε, 1) ) / ( ln(k) + 2 )        # == c / (ln k + 2) for c ≥ 0
            else 1.0

prob_term = 1 - ln(1 / clamp(p, ε, 1)) / 10                           # == 1 + ln(p)/10
            # p=1.0 → 1.0;  p≈0.37 → 0.9;  p=0.01 → 0.54;  p ≤ e^-10 ≈ 4.5e-5 → 0

opacity   = min(conf_term, prob_term).clamp(minimum_token_opacity/100, 1.0)
```

`minimum_token_opacity` default **65.0** (%), user slider range 20–80 (settings/mod.rs:93, 299).
Applied by taking the node color's **opaque RGB** and setting alpha = opacity, unmultiplied
(`change_color_opacity`, shared.rs:1356–1364). So shading is pure alpha against the editor
background; higher probability / higher confidence ⇒ more opaque text.

### 2.3 Hover highlighting (in the text itself)

`calculate_highlighting` (textedit.rs:462–543) reads the shared hovered-node signal:

| Shared hover state | Effect on this buffer |
|---|---|
| `Node(h)` and snippet's node == h | snippet background = theme `widgets.hovered.weak_bg_fill` (textedit.rs:516–519) |
| `WithinNode(h, pos)` and snippet's node == h | background as above on **every** snippet of that node; additionally the one token whose node-relative byte range contains `pos` gets an **underline** with theme `widgets.hovered.bg_stroke` (textedit.rs:506–515). Underline only if the snippet is a token (`token_index.is_some()`). |
| `None` | nothing |

So: hovering anywhere over a node (in *any* view) highlights the whole node's background in the
editor; the specific hovered token additionally gets underlined.

**Desync guard**: while the user is mid-edit (buffer no longer byte-identical to the weave-derived
snippets), highlighting walks snippets only while `snippet bytes == buffer bytes` at the same
range; at the first mismatch it stops and paints the entire rest of the buffer in the default
color (textedit.rs:497–502, 534–541). Section byte ranges are floored to char boundaries
(textedit.rs:526–529).

### 2.4 Node boundary markers

Computed in `calculate_boundaries_and_update_scroll` (textedit.rs:824–879), painted **before**
(i.e. under) the text as one triangle mesh (`render_rects`, textedit.rs:161, 881–927).

- One marker at the **start of every node** (whenever the snippet's node differs from the
  previous snippet's node) — including the first node (textedit.rs:851–860).
- Geometry: a thin vertical caret-like rect at the position of the node's first character:
  x = first-char x ± `boundary_width/2` on each side, y = that character's row top → row top +
  row height (textedit.rs:847–849 plus the "start" rect built in
  `absolute_snippet_positions`, textedit.rs:592–598).
- `boundary_width` = theme `widgets.hovered.fg_stroke.width` (textedit.rs:841; egui dark default
  1.5 px). Color = theme `widgets.inactive.bg_fill` (textedit.rs:839) — a subdued gray tick, not
  per-node colored. (A stronger color for token boundaries exists only as commented-out code.)
- **Skipped entirely when there are fewer than 2 snippets** (textedit.rs:832–834).
- Marker rect cache is recomputed when: cache empty · weave/theme changed · the text was edited
  this frame · the TextEdit rect changed (resize/reflow) · or an auto-scroll target exists
  (textedit.rs:197–224). After recomputing a non-empty cache, request a repaint
  (textedit.rs:221–223).

### 2.5 Font & frame

- Monospace, at **1.1 ×** the theme monospace size, forced for the whole view
  (textedit.rs:125–127, 157–159).
- TextEdit is frameless, fills available width and height, code-editor mode (no tab-focus-leave),
  wraps at available width, breaks on newline (textedit.rs:140–144, 163–169).
- Whole thing inside a vertical scroll area (`auto_shrink(false)`, scroll animation off), with an
  outer margin of `menu_spacing / 2` on all sides (textedit.rs:150–156).

Paint order (bottom → top): boundary-marker mesh → text with per-section background/underline →
(egui caret/selection, drawn by the TextEdit itself) → tooltips.

---

## 3. Hit-testing geometry (snippet → screen rects)

Three layered helpers compute absolute glyph positions from the laid-out galley:

- `absolute_galley_character_positions` (textedit.rs:743–822): iterates rows/glyphs, invoking a
  callback with (byte offset, row index, char top-left, char bottom-right). Each newline / final
  row end yields one extra synthetic 1-byte position at the row's right edge (textedit.rs:806–820).
- `absolute_snippet_positions` (textedit.rs:545–622): one bounding rect per snippet (union of its
  chars; used for boundary markers + scroll targets).
- `absolute_snippet_row_positions` (textedit.rs:624–741): **one rect per (snippet × visual row)**
  — a snippet wrapped across rows produces a separate hitbox per row; the callback's `increment`
  flag is true only on the segment where the snippet actually ends (used to advance the
  token-index counter exactly once per snippet; textedit.rs:295–298).

These per-row rects are the hover hitboxes (`ui.interact(bounds, Id::new((node_id, seq_index)),
hover-only)`, textedit.rs:236–240). They are recomputed **every frame** from the current galley
(no caching), so they track wrapping and scrolling exactly.

---

## 4. Interaction grammar

| Gesture | Where | Effect |
|---|---|---|
| Click / arrow keys / any caret move | text | Moves the egui text caret. On caret change (and only if the buffer wasn't edited this frame): map caret → (node, within-node byte offset) and write the shared **cursor node** = `WithinNode(node, off)` (or `Node(node)` if mapping has no ranges) (textedit.rs:316–346). **Does NOT change the active path** (Getting Started.md: the editor changes cursor position *without* changing active nodes). No selection-specific behavior: only the first (sorted) end of the selection is used (textedit.rs:317). |
| Typing / paste / cut / delete | text | egui edits the buffer; on `changed()`: whole-buffer diff into the weave (§6), then shared cursor set to `None` (textedit.rs:313–315, 458) — which the shared-state frame update immediately re-snaps to the active leaf (shared.rs:478–482). |
| Hover (pointer over a snippet's row-rect) | text | Sets shared **hovered node** = `WithinNode(node, token_start_off)` for token snippets, `Node(node)` for plain snippets (textedit.rs:247–256). This is re-asserted every frame the pointer stays inside; the shared state clears hover each frame if nobody re-asserts it (shared.rs:467–472). |
| Hover dwell | text | Tooltip appears after the theme tooltip delay (`style.interaction.tooltip_delay`, egui default 0.5 s) per egui's standard "pointer still" rule. Extra rule: an **already-open tooltip stays open while scrolling happened within the last `tooltip_delay` seconds** (workaround so interacting with the scrollable counterfactual row doesn't dismiss it; textedit.rs:258–267). While the tooltip is being rendered, the hovered-node signal is re-asserted from inside it (textedit.rs:269–278), so moving the pointer into the tooltip keeps the node highlight alive. |
| Click counterfactual button (inside token tooltip) | tooltip | Splits out the token and adds an alternative-token sibling branch — exact semantics §5.3. |
| Right-click | text | Nothing (context menu is an acknowledged TODO blocked on egui APIs, textedit.rs:42–43). |
| Ctrl+F | — | Not implemented (TODO, textedit.rs:45). |
| Scroll | view | Plain vertical scroll. Scroll position is local per-view state. |

There are no double-click/middle-click/drag/modifier behaviors in this view beyond the native
text-editing ones egui provides (double-click word select, drag select, etc.).

### 4.1 Keyboard shortcuts that consume the editor's cursor

The editor's main contribution is publishing `WithinNode(node, byte_offset)`; global shortcuts
(handled in shared state, configurable) then act on it. The two that use the *offset*:

- **GenerateAtCursor** (shared.rs:174–190): `WithinNode(n, 0)` → generate children of n's
  *parent*; `WithinNode(n, i>0)` → `split_node(n, i)` then generate children of n (now truncated
  to the first i bytes) — i.e. **generate exactly at the caret**, preserving the rest of n as the
  split-off child; `Node(n)` → generate children of n.
- **SplitAtCursor** (shared.rs:352–356): `WithinNode(n, i)` → `split_node(n, i)`.

(Other cursor-consuming shortcuts — bookmark toggle, add child/sibling, delete, merge, move —
operate on the node id only and live in the shared-state spec, shared.rs:192–434.)

---

## 5. Tooltips

`render_tooltip` (textedit.rs:943–1003). Content depends on the node and snippet type:

| Case | Tooltip contents (top → bottom) |
|---|---|
| Snippet node | node metadata block only |
| Tokens node, valid token index | counterfactual button row (if the token has `counterfactual` metadata) → separator → token block → separator → node metadata block |
| Tokens node, index out of range | node metadata block only (textedit.rs:997–999) |

### 5.1 Token block (shared.rs:900–915, 979–1015)

- Token text, Rust-debug-quoted (`"\n"` style escapes visible), monospace — shown **only if**
  `original_length` metadata is absent or equals the token's current byte length (i.e. hidden for
  tokens whose boundaries were modified by a split).
- Then, per metadata key in stored order:
  - `probability` → `probability: XX.XX%` (value × 100, 2 decimals)
  - `confidence` → `confidence: X.XX (k = K)` — only shown if `confidence_k` is also present
  - `original_length` → if ≠ current byte length: `modified_boundaries: true` in the theme
    **warn color**; if equal: nothing
  - `token_id` → `token_id: N` — shown **only if** `original_length` is present AND equals the
    current token byte length (i.e. token ids are hidden once boundaries changed)
  - `model_id`, `confidence_k`, `counterfactual` → never shown
  - anything else → `key: value`

### 5.2 Node metadata block (shared.rs:859–898)

- Model label, colored by the model's hex `color` metadata if parseable (plain label otherwise).
  Absent for model-less (human) nodes.
- Node metadata in stored order: `confidence` → `confidence: X.XX (k = K, n = N)` (n omitted if
  absent; whole line omitted if `confidence_k` absent); `confidence_k`/`confidence_n` hidden;
  others → `key: value`.
- Node creation time, derived **from the node ULID's embedded timestamp**, formatted as locale
  date + 12-hour time (`%x %r`, local timezone; shared.rs:894, 1366–1370).
- Debug builds additionally show the ULID string (shared.rs:896–897).

### 5.3 Counterfactual row & click semantics

Storage: token metadata key `counterfactual` = JSON array of `[base64url-no-pad(token bytes),
{metadata}]` pairs (v0.rs:484–506) — the top-k alternatives at that position, each with at least
`probability`.

Rendering (shared.rs:935–977): a **horizontally scrollable** row of buttons (scroll animation
off), one button per alternative that has a parseable `probability`; alternatives without it are
skipped entirely. Button label, monospace, two lines: debug-quoted token text (raw byte debug for
invalid UTF-8), then `(XX.XX%)`.

Clicking alternative `j` on token `i` of node `n` (textedit.rs:963–996):

1. **Split out the token** — `split_out_token(n, i)` (weave.rs:230–307) isolates token `i` into
   its own node via up to two byte-boundary splits, returning `(head, middle, tail)`:
   - `head` = node holding tokens `0..i` (this is `n` itself if `i > 0`; if `i == 0`, head = n's
     parent, possibly None);
   - `middle` = node holding exactly token `i`;
   - `tail` = node holding tokens `i+1..` (None if `i` was the last token).
   Split mechanics (`split_node`, dep.rs:411–453 + v0.rs:27–45): the **original node id keeps the
   left part** (and its active/bookmarked flags and parent); a **new ULID with the original
   node's timestamp** is minted for the right part, which becomes the original's only child and
   inherits all the original's children; the right part starts `active=false, bookmarked=false`;
   both parts share clones of the node metadata and model. (Splitting *inside* a token would
   strip `token_id` from both halves, v0.rs:126–137 — not hit here since splits are at token
   boundaries.)
2. **Add the alternative as a sibling of `middle`** (textedit.rs:983–994): new node with
   - fresh ULID (now), `from = head`, no children, `bookmarked = false`;
   - content = `Tokens([ (alt_token_bytes, alt_metadata + counterfactual: <the full original
     list>) ])` — one token, carrying its probability etc., **plus the same counterfactual list**
     so the new branch's token also shows the button row;
   - `metadata` and `model` **cloned from the original node** — the alternative is attributed to
     the same model, not to the human;
   - `active` = true **iff node `n` was on the active thread** (checked before the split,
     textedit.rs:974–977). Since making a node active makes it *the* active leaf
     (dep.rs:286–292), the active text then ends with the chosen alternative — the original token
     and everything after it (`middle` + `tail` + former descendants) remain in the tree as an
     inactive sibling branch.
   `add_node` dedup (v0.rs:303–326): if an identical node already exists (same content, metadata,
   model — e.g. clicking the same alternative twice), the new node is discarded and the existing
   duplicate is activated instead.

---

## 6. Whole-buffer edit diffing (`set_active_content`)

On any buffer change: substitution bytes restored (§1), then `weave.set_active_content(bytes, {})`
(textedit.rs:445–459 → weave.rs:318–328 → v0.rs:334–442). Contract: afterwards, the active
thread's concatenated content equals the buffer exactly (v0.rs:333).

Algorithm (single pass, root → leaf):

1. Walk active-thread nodes while each node's full content matches the buffer at the running
   offset; consume them (v0.rs:353–363).
2. At the first mismatching node: extend the match byte-by-byte through the common prefix
   (v0.rs:365–372). If the prefix is non-empty, **split that node at the prefix end**
   (v0.rs:376–390) — prefix stays under the original id (keeping it on-thread), the suffix plus
   all original descendants survive as an inactive child branch. Stop walking.
3. The last fully/prefix-matched node is set active (= new leaf); if nothing matched at all, the
   old active leaf is deactivated (active thread becomes empty) (v0.rs:398–403).
4. **Scratch-node coalescing** (v0.rs:405–422): if that last matched node has ≤1 child, its child
   (if any) is a leaf, it's not bookmarked, has **no model**, and its metadata equals the metadata
   being applied (empty here) — the node is *removed* and the offset rolled back over its content,
   so its bytes get re-appended as part of step 5. Effect: consecutive keystrokes keep mutating
   one human node instead of growing a chain of one-keystroke nodes.
5. If buffer bytes remain past the matched offset, append them as **one new `Snippet` node**:
   child of the last matched node (root node if none), `active=true`, `model=None`, empty
   metadata (v0.rs:424–439). Subject to the same `add_node` dedup as §5.3.

Consequences worth keeping when reimplementing: mid-document edits never destroy downstream
nodes — they split and strand them as an inactive branch; pure deletions from the end just move
the active leaf up (possibly splitting one node); typing at the end grows/mutates a single
human-attributed node.

---

## 7. Caret ↔ node mapping

### 7.1 Caret → node (`calculate_cursor` textedit.rs:415–444 + `calculate_cursor_index` 929–941)

- Caret char index → byte index (`byte_index_from_char_index`).
- Scan snippets accumulating lengths; the snippet whose cumulative end ≥ byte index claims the
  caret. **At an exact snippet boundary the later snippet wins** (the loop records the earlier
  one, continues, and the next iteration overwrites and breaks; textedit.rs:428–436).
- Node-relative offset = document byte index − node's first range start, clamped via
  `.min(node's last range end)` (textedit.rs:937 — note: the clamp bound is the *document-absolute*
  end of the node's last range, an apparent oddity in the source; harmless for in-range carets).
- Published as `WithinNode(node, rel_offset)`; if the node has no recorded ranges, `Node(node)`;
  if no caret exists, falls back to the active **leaf** node (textedit.rs:437–441); empty
  thread → `None`.
- Recomputed only when the caret actually moved since the last frame AND the buffer wasn't edited
  this frame (textedit.rs:316–346).

### 7.2 Node → caret (external cursor changes)

When some *other* view moves the shared cursor node (textedit.rs:172–190): if the buffer wasn't
edited this frame or last, the caret is set to **the end of the document** (char count) — an
acknowledged placeholder (`TODO: Rewrite this to properly change the cursor position`,
textedit.rs:173). The actual "bring it into view" behavior comes from auto-scroll (§8), not the
caret. A web reimplementation should place the caret at the start of the target node's range
instead of the end of the document.

---

## 8. Auto-scroll

Trigger condition, evaluated when recomputing boundary rects (textedit.rs:197–217):

```
state.changed_node is Some
AND settings.interface.auto_scroll          (default true, settings/mod.rs:97)
AND pointer is NOT inside the TextEdit rect (textedit.rs:194–195)
```

`changed_node` (shared.rs:466–487) = the node whose **hover** changed this frame, else the node
whose **cursor** changed this frame, else None; it lives one frame only. So both "someone moved
the cursor in the tree" *and* "someone is hovering a node in another view" steer the editor's
scroll — but **never while your pointer is over the editor itself** (pointer-suppression: hovering
the text suppresses scroll-stealing; your own in-editor hovers also set changed_node but are
suppressed by the same rule).

Scroll target = union of *all* bounds rects belonging to the changed node (the whole node, even
if it spans rows; textedit.rs:861–872), passed to `scroll_to_rect(rect, None)` — egui scrolls the
minimum amount to bring the rect into view, no alignment forcing, no animation.

---

## 9. Shared state: reads & writes

| Signal | Read | Written |
|---|---|---|
| Hovered node (`NodeIndex`: `Node(id)` / `WithinNode(id, byte_off)` / `None`) | every frame, for text highlight (§2.3) | on pointer-in-hitbox and while tooltip shown (textedit.rs:247–256, 270–277). Cleared globally each frame unless re-asserted (shared.rs:467–472). |
| Cursor node | to detect external changes (textedit.rs:172) | on caret move (§7.1); set to `None` right after an edit (textedit.rs:458) |
| Changed node (1-frame) | as auto-scroll target (textedit.rs:201, 211–214) | never (derived in shared update from hover/cursor deltas, shared.rs:466–487) |
| `has_weave_changed` / `has_theme_changed` | to invalidate the buffer (textedit.rs:99–102) | indirectly, via weave mutations |
| Weave active thread | buffer build (§1), caret fallback (§7.1), counterfactual `active` check (§5.3) | `set_active_content` (§6), counterfactual click (§5.3) |
| Collapse/open set, in-flight requests | not used by this view | — |

Cursor invariants enforced by shared update each frame (shared.rs:473–487): cursor pointing at a
deleted node → `None`; `None` cursor → re-snapped to the active leaf; cursor change auto-expands
the path to it in tree views.

---

## 10. Edge cases

- **Empty weave / no active thread**: empty buffer; typing creates a root `Snippet` node
  (`from: None`, active) (v0.rs:424–439).
- **< 2 snippets**: no boundary markers at all (textedit.rs:832–834).
- **Mid-edit frame**: highlighting truncates at first weave/buffer divergence (§2.3); caret→node
  mapping skipped; external-cursor caret-jump skipped this frame and the next
  (textedit.rs:174, 350).
- **Token tooltip with stale index**: falls back to node metadata only (textedit.rs:997–999).
- **Counterfactual entries without `probability`**: silently skipped in the button row
  (shared.rs:947–948).
- **Tokens whose boundaries were modified** (`original_length` ≠ len): token text hidden in
  tooltip, `token_id` hidden, warn-colored `modified_boundaries: true` shown (§5.1).
- **Clicking the same counterfactual twice**: deduplicated; the existing branch is re-activated
  (v0.rs:303–326).
- **`split_out_token` on a non-Tokens node or out-of-range index**: returns None → click is a
  no-op (weave.rs:235–306, textedit.rs:973).

---

## 11. Web translation notes (coloom)

egui-specific mechanics and their one-line web equivalents:

- *Immediate mode (everything recomputed per frame)*: in Svelte, make snippets/highlights/rects
  `$derived` of (thread content, hover signal, theme); hover hitboxes become real DOM spans —
  you get per-token `<span>`s for free instead of glyph-rect math (§3 is then unnecessary;
  per-token spans inside a `contenteditable` or an overlay-div + hidden `<textarea>` pair
  replicate the layered paint order).
- *`request_repaint` flags*: irrelevant; reactivity handles it.
- *Hover re-assert-every-frame + global clear*: replace with plain `mouseenter`/`mouseleave` on
  spans writing a shared hover store; the "tooltip keeps hover alive" trick becomes "hover state
  persists while pointer is in span ∪ tooltip popover".
- *Tooltip delay + stays-open-while-scrolling workaround*: standard delayed popover anchored to
  the token span; keep it open while the pointer is inside it (the egui hack exists only because
  egui tooltips can't be hovered natively).
- *Caret APIs*: caret→node mapping = `selectionStart` byte offset against the same snippet table;
  node→caret should be done properly (place caret/scroll at the node's range) rather than copying
  the end-of-document TODO (§7.2).
- *Byte offsets*: the Rust code is byte-oriented (UTF-8) with a 1-byte substitution char. In JS,
  either keep a byte↔UTF-16 index map per snippet or store offsets in bytes server-side and
  convert at the edge; don't mix the two silently.

Where coloom's model diverges (must adapt, not copy):

- **No single active path** → there is no global "the buffer". The editor view renders **one
  cursor's thread** (root → cursor node). "Active" checks (counterfactual click §5.3, edit diff
  §6) become "on the viewed cursor's thread", and `set_active_content`'s "activate node X"
  becomes "move this participant's cursor to X". The tail-dropping behavior of counterfactual
  clicks maps cleanly: move the cursor to the new alternative node.
- **Per-participant cursors, anyone can move anyone's** → the §8 auto-scroll "changed node"
  signal generalizes: scroll when *the cursor this pane follows* moves (including moves by
  others — the "look here" gesture), with the same pointer-over-pane suppression rule, which
  matters even more with a live collaborator on the other end.
- **Creator attribution colors every node** → Tapestry's "model color else default" (§2.1)
  becomes "creator color always" (human participants get colors too, not just models); the
  token-opacity formula (§2.2) composes on top unchanged.
- **Server-canonical over REST+WS** → the whole-buffer diff (§6) should run server-side (or be
  expressed as explicit split/append/move-cursor operations) so two concurrent editors don't diff
  against stale threads; the one-frame `changed_node` becomes "node ids touched by the last WS
  event".
- Tooltip metadata: coloom stores typed `Tokens` (`logprob`, `entropy`, `top_logprobs[]`) instead
  of a string metadata map — §5.1's display rules map onto those fields (probability =
  `exp(logprob)`; counterfactual row = `top_logprobs`); the `original_length`/`token_id`-hiding
  rules apply when splits modify token boundaries.
