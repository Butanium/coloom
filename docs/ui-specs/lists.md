# Tapestry-Loom list views — behavioral spec (Tree list, Flat list, Bookmarks list)

Source of truth: `Tapestry-Loom/src/editor/lists.rs` (all line refs below are into that file unless
prefixed with another path). Helper behavior from `src/editor/shared/mod.rs` (`shared/mod.rs:`),
weave semantics from `src/editor/shared/weave.rs` and `tapestry-weave/src/v0.rs`, defaults from
`src/settings/mod.rs` and egui 0.33.3 defaults. Reference checkout:
`~/projects2/weird-personas/Tapestry-Loom` (branch `new-format`).

The three views in this file:

| View | What it shows | Source |
|---|---|---|
| **Tree list** ("Tree" subview) | Indented collapsible treelist of nodes *near the cursor node* (sliding window) | `TreeListView`, lists.rs:301–478 |
| **Flat list** ("List" subview) | Flat list of the cursor node's children (or roots if no cursor) | `ListView`, lists.rs:37–165 |
| **Bookmarks** | Flat list of all bookmarked nodes, in bookmark-insertion order | `BookmarkListView`, lists.rs:167–299 |

All three are built from one shared row widget (`render_horizontal_node_label`,
lists.rs:1008–1177) and one shared context menu (`render_node_context_menu`,
lists.rs:1179–1351). Read §2–§4 first; the per-view sections only describe what differs.

---

## 1. Shared state read/written by these views

`SharedState` (shared/mod.rs:50+) is the editor-wide shared+temporary state. Frame lifecycle in
egui: every frame, each subview's `update()` then `render()` runs; "hovered" is recomputed from
scratch each frame (set during render, committed at start of next frame, shared/mod.rs:464–476).

| Signal | Type | Lists READ it for | Lists WRITE it when |
|---|---|---|---|
| `cursor_node` | `NodeIndex` (`None`/`Node(id)`) | window centering (tree), item source (flat list), cursor stroke on row | clicking a row label/row background (lists.rs:1108–1111, 1173–1176); "Show parents"; "Show more"; create-active-node actions; generate-with-modifier |
| `hovered_node` | `NodeIndex` | row hover highlight (`is_hovered`) — note: hover set in *any* subview highlights the node in *all* subviews | whenever the pointer is over a row label or row's hover-rect (lists.rs:1103–1106, 1124–1127, 1169–1171) |
| `changed_node` (`get_changed_node()`) | `Option<Ulid>` | auto-scroll target (lists.rs:1090–1097) | never by lists directly; set by SharedState when cursor OR hovered node changed last frame (shared/mod.rs:466–489) |
| open/collapse map (`is_open`/`set_open`) | `HashMap<Ulid,bool>`, default per node = `settings.interface.opened_by_default` (default **false**, settings/mod.rs:96) | tree collapsing state | collapse-triangle click; generate (opens node); add-inactive-child (opens parent); context-menu collapse/expand-all-children; shortcuts (lists.rs:377–391) |
| `has_weave_changed` / `has_cursor_node_changed` / `has_opened_changed` | bools, true for one frame | invalidate virtual-list caches / tree window | n/a (set centrally) |
| weave itself | nodes/edges/active/bookmarked | everything | activate, bookmark toggle, add node, delete, merge, sorts |
| in-flight inference | via `state.generate_children(...)` (shared/mod.rs:771) | n/a (lists don't render in-flight indicators) | Generate button / context menu |

Cursor maintenance done centrally (matters for lists): if the cursor node is deleted, cursor falls
back to the head of the active thread (shared/mod.rs:477–484); whenever the cursor changes, **every
node on the thread through the cursor is force-opened** (shared/mod.rs:494–502).

`NodeIndex` also has a `Position` variant used by the text editor; lists only deal with
`.into_node()` (node or nothing).

---

## 2. The node row (`render_horizontal_node_label`, lists.rs:1008–1177)

One row = one node. Used identically by all three views (flags differ: `show_node_info`, button
strip content, context menu `collapsing` flag).

### 2.1 Row anatomy (left → right)

```
[indent/collapse-triangle (tree only)] [node text label-button] ............ [right-aligned overlay]
```

- The whole row is a `horizontal_wrapped` line; the **label is a frameless button** whose text is
  the node's full text (monospace), wrapping over multiple visual lines. No truncation/ellipsis —
  the full node text is laid out and wraps (lists.rs:144, 1041–1077).
- Leading space: `icon_spacing` (4px) before the label in flat/bookmark lists (lists.rs:145, 271);
  in the tree, leaf rows get `indent_compensation = icon_width + icon_spacing = 14 + 4 = 18px`
  to align with sibling rows that have a collapse triangle (lists.rs:498, 598–600).
- Bookmarks rows additionally prefix a static bookmark glyph `\u{E060}` before the label
  (lists.rs:272).
- **Right-aligned overlay** (`Layout::right_to_left` inside the row's hover-rect,
  lists.rs:1129–1160), two mutually exclusive modes:
  - **Hovered** → the action button strip (§3).
  - **Not hovered** AND `show_node_info=true` → passive info, right-aligned, order right→left:
    `icon_spacing` pad, then bookmark glyph `\u{E060}` if `node.bookmarked`, then probability
    `"{:.1}%"` (probability × 100) **only if** the node content is `Tokens` with **exactly 1
    token** that has a parseable `probability` metadata entry (lists.rs:1142–1155). `show_node_info`
    is true for tree and flat list, **false for bookmarks** (lists.rs:293 vs 158/623).

### 2.2 Hover mechanics (exact)

- The hover region (`hover_rect`) is **the full row band**: x from the row's left edge to the right
  edge of the available width, y spanning the (possibly multi-line) label's vertical extent
  (lists.rs:1113–1122). Pointer inside `hover_rect` OR over the label button ⇒ this node becomes
  the shared hovered node and the strip appears (lists.rs:1103–1106, 1124–1127).
- The button strip is rendered **inside** `hover_rect` (right-aligned overlay), so moving the
  pointer onto the buttons keeps the row hovered. No timing, debounce, or grace period — hover is
  pure pointer-in-rect, re-evaluated per frame; leaving the band swaps buttons back to passive info
  immediately.
- Hover highlight visuals: §7.
- Hovering the **label text itself** additionally shows a tooltip (egui default delay): if the node
  is a single-token `Tokens` node → token tooltip (the token string `Debug`-quoted, monospace, plus
  per-token metadata) then a separator; always followed by the node metadata tooltip — model label
  (tinted with the model color), all node metadata key/values (confidence pretty-printed as
  `confidence: {:.2} (k = …, n = …)`), and the node's creation time derived from its ULID
  (`%x %r` local format) (lists.rs:1077–1088; shared/mod.rs:859–915). Tooltip max width = 500px.

### 2.3 Click semantics on the row

| Gesture | Target | Effect | Ref |
|---|---|---|---|
| Left-click | label button, or anywhere in the row scope not consumed by a button | `weave.set_node_active_status(node, true)` **and** `cursor_node = node` ("click to activate") | lists.rs:1108–1111, 1173–1176 |
| Right-click | label button or row scope | opens context menu (§4) | lists.rs:1099–1101, 1165–1167 |
| Left-click | a hover-strip button | that button's action only (buttons consume the click; row activation does not fire) | §3 |
| Double-click / drag / scroll-on-row | — | no special handling in lists (scroll = normal scroll area) | — |

There is **no** click-to-toggle-collapse on the label; collapsing is only via the triangle (§5.4)
or context menu / shortcuts.

### 2.4 Auto-scroll to changed node (with pointer suppression)

lists.rs:1090–1097. When `settings.interface.auto_scroll` is on (default **true**,
settings/mod.rs:97), a row scrolls itself into view (instant, no animation —
`INSTANT_SCROLL`, shared/mod.rs:44) iff ALL of:

1. this node == `changed_node` (the node whose cursor- or hover-status changed last frame —
   so the view follows cursor moves made in *any* subview, and even follows hovers made in
   *other* subviews, see 3 below);
2. `is_cursor` (it's the cursor node) **OR** the pointer is **not** inside this view's clip rect
   (`contains_cursor` computed once per view render from `ui.clip_rect().contains(pointer)`,
   lists.rs:86–88, 212–214, 434–436). I.e. **hover-driven scrolling is suppressed while your mouse
   is over the view itself**; cursor-driven scrolling always happens;
3. the row's height fits in the viewport (`max_autoscroll_height >= row height`, where
   `max_autoscroll_height` is the scroll area's available height captured before content,
   lists.rs:97/223/472) **OR** any keyboard modifier is held (modifier = manual override to scroll
   to oversized rows).

### 2.5 Row visual states (summary; exact colors in §7)

| State | Test | Visual |
|---|---|---|
| hovered | `hovered_node == node` (from any view) | frame fill behind row = `widgets.hovered.weak_bg_fill`; non-active uncolored label text switches to `hovered` text color |
| active (on active thread) | `node.active` | label button rendered `selected(true)` (egui selection fill) — if the node has a model color, fill = that color at alpha 0.5 instead; text = `widgets.active` text color |
| cursor | `cursor_node == node` | button stroke = `widgets.hovered.bg_stroke` (1px gray150 outline) |
| bookmarked | `node.bookmarked` | passive `\u{E060}` glyph at right (tree/flat list only) |

Z-/paint order within a row: hover frame fill, then label button, then right overlay (buttons or
passive info) painted over the same band.

---

## 3. Hover action-button strips

Buttons are small icon buttons; each has a hover tooltip. Glyphs are PUA codepoints in the bundled
Lucide icon font (src/main.rs:146); exact Lucide names are not resolvable from source, so they are
listed by codepoint + tooltip (Getting Started.md:79 describes `\u{E5CE}` as "the speech bubble
icon with a robot in it"; Getting Started.md:98 describes `\u{E23C}` as "the bookmark icon with a
minus within it").

"**bg-click**" below = `clicked_with_open_in_background()` = **middle-click OR left-click with any
modifier held** (ctrl/shift/alt/cmd — egui-0.33.3/src/response.rs:226–228). Tooltips swap to the
modifier wording live while any modifier is held (`is_modifier_pressed`, lists.rs:690/780).

### 3.1 Tree & flat list strip (`render_horizontal_node_label_buttons_rtl`, lists.rs:773–861)

Emitted in a right-to-left layout, so **visual order left→right** is the reverse of emission:

| Visual pos (L→R) | Glyph | Tooltip (plain / modifier held) | Click | bg-click extra | Shown when |
|---|---|---|---|---|---|
| 1 (tree display-root rows only) | `\u{E042}` | "Show parents" | `cursor_node = node.from` (re-centers tree window one level up) | — | row `is_display_root` && node has a parent (lists.rs:609–617) |
| 2 | `\u{E43F}` | "Merge node with parent" | `weave.merge_with_parent(node)` | — | `is_mergeable_with_parent` (§6) |
| 3 | `\u{E5CE}` | "Generate completions" / "Generate completions & focus node" | `state.generate_children(weave, node, settings)`; always `set_open(node, true)` | also `set_node_active_status(node, true)` + `cursor_node = node` | always |
| 4 | `\u{E40C}` | "Add node" / "Add active node" | create empty Snippet child of node; `active = node.active` (**inherits parent's active status**); if resulting node is active → `cursor_node = new node`, else → `set_open(node, true)` | `active = true` (forces cursor jump) | always |
| 5 | `\u{E23C}` (bookmarked) / `\u{E23d}` (not) | "Remove bookmark" / "Bookmark node" | toggle `node.bookmarked` | — | always |
| 6 (rightmost) | `\u{E28F}` | "Delete node" | `weave.remove_node(node)` | — | always |

An LTR variant with the same buttons in mirror order exists for other subviews
(`render_horizontal_node_label_buttons_ltr`, lists.rs:683–771); lists use the RTL one.

### 3.2 Bookmarks strip (lists.rs:280–288)

Single button: `\u{E23C}` tooltip "Remove bookmark" → `set_node_bookmarked_status(node, false)`.
No other buttons; no modifier variants.

### 3.3 Empty-tree strip (§5.7)

Single `\u{E40C}` "Add node" → creates a **root** node (`from: None`, `active: true`, empty
Snippet) and sets the cursor to it (lists.rs:946–962).

---

## 4. Right-click context menu (`render_node_context_menu`, lists.rs:1179–1351)

Same menu for all three views; `collapsing=true` only in the tree (lists.rs:620) — bookmarks and
flat list pass `false` (lists.rs:156, 290). Labels with a `/` switch live while a modifier is held.
bg-click = middle-click or modifier-click of the menu item.

| # | Item | Enabled / shown when | Effect | Ref |
|---|---|---|---|---|
| 1 | Generate completions | always | same as strip Generate incl. bg-click focus behavior and `set_open(node, true)` | 1189–1199 |
| 2 | Bookmark / Remove bookmark | always (label reflects state) | toggle bookmark | 1201–1208 |
| — | separator | | | 1210 |
| 3 | Create child / Create active child | always | empty Snippet child, `active = node.active` (bg-click ⇒ true); active ⇒ cursor=new node, else open parent | 1212–1243 |
| 4 | Create sibling / Create active sibling | always | empty Snippet with `from = node.from` (sibling of a root ⇒ new root); `active = node.active` (bg-click ⇒ true); active ⇒ cursor=new node (no open-parent fallback) | 1245–1273 |
| — | separator | | | 1275 |
| 5 | Collapse all children | node has children AND `collapsing` (tree only) | `set_open(child, false)` for each direct child | 1278–1283 |
| 6 | Expand all children | same | `set_open(child, true)` for each direct child | 1285–1289 |
| — | separator (tree only, when children) | | | 1291 |
| 7 | Seriate children | node has children | `state.seriate_children` (semantic ordering of children; async) | 1294–1296 |
| 8 | Sort children by confidence | node has children | sort children desc by confidence metadata | 1298–1300 |
| 9 | Sort children by timestamp | node has children | sort children by node ULID (creation time) | 1302–1304 |
| — | separator | | | 1306 |
| 10 | Delete all children | node has children | `remove_node` every direct child (recursive subtree delete per child) | 1308–1312 |
| 11 | Delete all siblings | always | delete all other children of the parent; for a root node, deletes **all other roots** | 1315–1336 |
| 12 | Merge with parent | `node.from` exists AND `is_mergeable_with_parent` (§6) | merge node into parent. NOTE: source draws its preceding separator *inside* the clicked-branch (lists.rs:1342) — an apparent bug; treat as: separator before this item | 1338–1344 |
| — | separator | | | 1346 |
| 13 | Delete | always | `remove_node(node)` (removes its whole subtree) | 1348–1350 |

---

## 5. Tree list view (`TreeListView`, lists.rs:301–681)

### 5.1 Sliding window: display-root selection (lists.rs:415–430)

The tree does NOT render from the weave roots; it renders a window centered near the cursor:

| Condition | Display roots |
|---|---|
| cursor node exists, has a parent, and that parent has a parent (grandparent exists) **and cursor has children** | `[parent(cursor)]` — window shows cursor's parent, the cursor, its siblings, and descendants |
| same but **cursor is a leaf** | `[grandparent(cursor)]` — one extra level of upward context |
| otherwise (no cursor, cursor is a root, or cursor's parent is a root) | all weave roots |

Window re-centering is purely cursor-driven: clicking any row moves the cursor, so navigation
naturally slides the window. Explicit re-centering gestures:

- **"Show parents"** (`\u{E042}`, hover strip, display-root rows only): cursor ← parent of the
  display root ⇒ window slides up one level (lists.rs:609–617).
- **"Show more"** (depth cap, §5.2): cursor ← the capped node ⇒ window re-centers down.

### 5.2 Depth cap & "Show more" (lists.rs:636–670)

Recursion depth is capped at `settings.interface.max_tree_depth` (default **10**, slider range
3–32, settings/mod.rs:95/310), counting down from the display root. When a node with children sits
at depth 0 and is expanded, instead of its children one pseudo-row is rendered:
`"\u{E04A} Show more"` (proportional font, frameless button).

- Click → `cursor_node = the capped node` (re-centers the window so its subtree becomes visible)
  (lists.rs:894–895, 909–911 via `render_omitted_node_label` with `selection_node = node.id`).
- Hover → highlights the node's **first child** as the shared hovered node (`hover_node =
  first_child`, lists.rs:661–668, 874–891) — so other views (canvas/graph) highlight what you'd be
  jumping toward. Hover visuals: fill `hovered.weak_bg_fill` + hovered text color.

### 5.3 Row rendering & separators (lists.rs:565–681)

- Rows render depth-first. Leaf nodes: plain row with 18px indent compensation. Nodes with
  children: an egui `CollapsingState` header (triangle + row) with the children as body, indented
  by egui's standard indent.
- A hairline separator (§7.4) is drawn **above** every row except the very first row of the display
  root level (lists.rs:517, 587–589).
- Egui `CollapsingState` id = hash of `[editor_id, node_id, 0]` (lists.rs:592) — collapse state is
  per-editor, keyed by node.

### 5.4 Collapse / expand

- Triangle click toggles; the new value is written back into shared open-state
  (`state.set_open(node, is_open)`, lists.rs:673–678). Each frame the header's openness is forced
  from shared state (`collapsing.set_open(state.is_open(node))`, lists.rs:594) so all views agree.
- Default openness for never-touched nodes: `opened_by_default` (default **false** — i.e. the tree
  starts collapsed except the auto-opened cursor thread, shared/mod.rs:629–634).
- Cursor changes force-open the whole thread through the cursor (shared/mod.rs:494–502).
- Keyboard shortcuts handled in this view (lists.rs:377–391): **CollapseAllVisibleInactive** —
  `set_open(false)` for every node rendered last frame that is NOT on the cursor's thread
  (`last_active_nodes` = `get_thread_from(cursor)`, lists.rs:393–403); **ExpandAllVisible** —
  `set_open(true)` for every node rendered last frame. (Two more shortcuts live centrally:
  Collapse/ExpandChildren of the cursor node, shared/mod.rs:442–462.)

### 5.5 Virtualization (egui_virtual_list) & cache management

- Virtualization is **off by default** (`optimize_tree` default false, and it's only offered when
  `auto_scroll` is off — settings/mod.rs:322–325). Effective rule per sibling group
  (lists.rs:500–562): plain loop when `auto_scroll || !optimize_tree || group size < 10`;
  otherwise the group gets its own `VirtualList` keyed by the parent node id (`branch_identifier`;
  `Ulid::nil()` for the top level).
- When any virtual list is live, the outer ScrollArea hides its scrollbar
  (`AlwaysHidden`) as a workaround for nested-virtual-list bugs (lists.rs:441–445).
- Cache invalidation (lists.rs:329–367, 405–413): on cursor change → clear the active-thread set
  and refresh; on weave change / max-depth change / open-state change → refresh. Refresh = reset
  lists still relevant (rendered last frame, on the active thread, or top-level) and drop the rest.
  If virtualization is disabled, all lists are dropped (lists.rs:405–408).

### 5.6 Auto-scroll interplay

`auto_scroll` ON forces plain (non-virtualized) rendering of everything so `scroll_to_me` can
reach any row. `contains_cursor` (pointer-in-view) is computed once per render and threaded to
every row for the §2.4 suppression rule.

### 5.7 Empty state (lists.rs:450–454, 914–978)

Weave empty → single disabled row "No nodes" (proportional font). Hovering the row band reveals a
right-aligned `\u{E40C}` "Add node" button → creates an active empty root + cursor to it.

---

## 6. `is_mergeable_with_parent` gate

tapestry-weave/src/v0.rs:467–477 + 65–73, 171–182. True iff: node has a parent AND the parent has
**exactly one child** (this node) AND parent/node have identical `metadata` maps AND identical
`model` AND same content kind (Snippet+Snippet or Tokens+Tokens). Gates both the strip merge
button and the context-menu item.

---

## 7. Visual encodings (exact)

App theme follows system dark/light; values below are egui 0.33.3 **dark** defaults
(egui style.rs:1525+), light analogues exist. UI scale default 1.25 (settings/mod.rs:87) — all px
values are logical points pre-scale.

### 7.1 Node text color (shared/mod.rs:1240–1352)

- Base color = model label color (`node.contents.model.metadata["color"]`, hex) if
  `show_model_colors` (default true); a global `model_color_override` may replace it for all
  model-made nodes (`override_model_colors`, default false). No model / colors disabled →
  `widgets.inactive` text color (gray180).
- **Active nodes**: text color overridden to `widgets.active` text color (white) since the label
  is on a selected/filled button (lists.rs:1044–1049).
- `Tokens` nodes: one layout section per token; each token's color = base color with
  **probability-derived opacity** (shared/mod.rs:1017–1047):

  ```
  conf_term = show_token_confidence && confidence present
      ? ln(1 / clamp(exp(-confidence), ε, 1)) / (ln(confidence_k) + 2)
      : 1.0
  opacity = clamp( min(conf_term, 1 - ln(1/clamp(p, ε, 1)) / 10),
                   minimum_token_opacity/100, 1.0 )
  ```

  i.e. `1 + ln(p)/10` for the probability part (p=1 → 1.0; p≈e⁻¹⁰ ≈ 4.5e-5 → 0); floor =
  `minimum_token_opacity` default **65%**; confidence (if present) can only lower it. Disabled via
  `show_token_probabilities` (default true).
- Font: monospace text style for node text; the "No text" placeholder uses the proportional Body
  font at monospace size, colored like the (first token of the) node (shared/mod.rs:1320–1336).
- Empty content (`Snippet` empty or `Tokens` with empty text) renders as literal **"No text"**.
  Invalid UTF-8 chunks render as `\u{1A}` (SUB) replacement (shared/mod.rs:1366–1400).

### 7.2 Row/button chrome (dark defaults)

| Element | Value |
|---|---|
| hover row fill | `widgets.hovered.weak_bg_fill` = gray(70) |
| hovered text | `widgets.hovered.fg_stroke` color = gray(240) |
| cursor stroke on label button | `widgets.hovered.bg_stroke` = 1px gray(150) |
| active-node button | egui `selected` fill (`selection.bg_fill` = rgb(0,92,128)); if model color exists: that color at alpha **0.5** (lists.rs:1053–1057) |
| inactive label button | transparent fill, no stroke |
| `change_color_opacity(c, a)` | take RGB of `c` opaque, reapply alpha `a` unmultiplied (shared/mod.rs:1356–1364) |

### 7.3 Layout metrics

| Metric | Value |
|---|---|
| outer margin of every list (`listing_margin`) | `menu_spacing` on all sides = **2px** (src/main.rs:629–631; egui default) |
| `icon_spacing` | 4px |
| indent compensation (tree leaves) | 18px (icon_width 14 + icon_spacing 4) |
| separator | full available width, 0 height allocation, 1px hline at the row gap's vertical center |
| tooltip max width | 500px |
| ScrollArea | vertical, `auto_shrink(false)`, animation off |

### 7.4 Separator color (lists.rs:981–1006)

`widgets.noninteractive.bg_stroke` (1px gray(60)) with opacity `list_separator_opacity/100`
(default **30%** ⇒ alpha 0.3). Skipped entirely when the setting is ~0.

---

## 8. Flat list & bookmarks specifics

| | Flat list (`ListView`) | Bookmarks (`BookmarkListView`) |
|---|---|---|
| items | children of cursor node, in weave child order; **roots** if no cursor (lists.rs:76–84) | `weave.get_bookmarks()` — bookmark insertion order (lists.rs:206; Getting Started.md:93) |
| row prefix | none | `\u{E060}` glyph |
| passive info (`show_node_info`) | yes (bookmark glyph + 1-token probability %) | no |
| hover strip | full RTL strip (§3.1, no Show-parents) | remove-bookmark only |
| context menu | full, `collapsing=false` | full, `collapsing=false` |
| virtualization | one `VirtualList` for the whole list when `auto_scroll` off; plain loop when on (lists.rs:99–126) | same (lists.rs:225–252) |
| list reset | on weave change or cursor change (lists.rs:63–65) | on weave change (lists.rs:193–195, 208–210) |
| empty state | renders nothing (no rows, no message) | renders nothing |
| separators / margins / autoscroll / click-to-activate | identical to §2 | identical to §2 |

Missing node ids (stale children/bookmarks) are silently skipped (`if let Some(node) = get_node`,
lists.rs:140, 266, 586).

---

## 9. Edge cases checklist

- Empty weave: tree shows "No nodes" + hover add-root; flat/bookmarks show nothing.
- Cursor on a root (no grandparent): tree falls back to rendering ALL weave roots, not a window.
- Cursor deleted: centrally reassigned to the active-thread head; tree window recomputed.
- Single-token nodes are special three ways: passive probability %, token tooltip section, and
  per-token coloring trivially applies.
- "Add node"/"Create child" inherit `node.active` — adding under an active node silently extends
  the active thread and jumps the cursor even without a modifier.
- Merge button/menu item appear only under the strict §6 gate (single-child + same model/metadata
  /content-kind).
- Auto-scroll can be triggered by hovering a node in a *different* subview (changed_node includes
  hover changes); suppressed only by the pointer being inside the receiving view.
- Multi-line (taller-than-viewport) rows are not auto-scrolled to unless a modifier is held (§2.4
  rule 3).
- Deleting "all siblings" of a root deletes every other root tree.

---

## 10. Web translation notes (coloom)

Egui-isms and their Svelte equivalents, one line each:

- **Immediate mode / per-frame hover reset** → a single `hoveredNodeId` store updated on
  `pointerenter`/`pointerleave` of the row band; CSS `:hover` is insufficient because hover must be
  *shared across panes* (canvas highlights what a list hovers).
- **`changed_node` + repaint flags** → derive "scroll target" from cursor/hover store changes
  (subscribe and `element.scrollIntoView({behavior:'instant', block:'nearest'})`); implement the
  §2.4 suppression with a `pointer-inside-pane` flag per pane.
- **`clicked_with_open_in_background`** → `event.button === 1 || (event.button === 0 &&
  (event.ctrlKey||event.shiftKey||event.altKey||event.metaKey))`; also handle `auxclick` for
  middle. Live tooltip/label swap while modifier held needs a window `keydown/keyup` listener.
- **VirtualList / optimize_tree** → likely unnecessary at first on the web; if needed, virtualize
  only sibling groups ≥ some threshold, and remember Tapestry disables virtualization whenever
  auto-scroll is on.
- **CollapsingState id per editor** → collapse set is client-local UI state (Tapestry classifies it
  shared+temporary, Getting Started.md:139–143); keep it per coloom client, not server-canonical.
- **Context menu** → custom `contextmenu`-event menu component (browser native menus can't host
  these items).
- **Tooltips** → custom hover-card (model label color, metadata, timestamp); egui's default hover
  delay ≈ 0.5s is fine to approximate.

Where coloom's model **differs** (do not port literally):

- **No single active path / `node.active`.** Tapestry's click-to-activate = `set_active(true)` +
  set cursor; coloom's equivalent is **move my cursor to the node** (`POST` cursor move). The
  "active thread" visual (selected/filled rows) becomes "on some participant's cursor thread",
  potentially several, one tint per participant; the §2.5 `cursor` stroke generalizes to one stroke
  per participant cursor sitting exactly on the node. "Show parents"/"Show more"/"Add active node"
  all reduce to cursor moves. The `active = node.active` inheritance rule for new nodes has no
  coloom equivalent — decide explicitly (suggest: plain create never moves cursors; modifier-create
  moves *my* cursor).
- **Tree window centering**: center on *my* cursor by default; moving someone else's cursor (the
  "look here" gesture) is a separate affordance lists never had in Tapestry.
- **Creator attribution**: Tapestry colors nodes by *model label color* only (human nodes
  uncolored). coloom colors every node by `Creator` (human vs model); map §7.1's "model color" to
  creator color and keep the token-opacity formula on top of it.
- **Server-canonical mutations**: every weave write in §3/§4 (activate→cursor-move, bookmark, add,
  delete, merge, sorts) becomes a REST call; `has_weave_changed`-driven invalidation becomes WS
  event handling. Apply optimistically or on-event, but keep lists' "silently skip missing ids"
  behavior for races.
- **Cursor-fallback on delete** (shared/mod.rs:477–484) must be reimplemented **server-side** per
  participant cursor (e.g. fall back to deleted node's parent).
- **Open-thread-on-cursor-move** (force-open the cursor's whole thread) should apply to the local
  participant's cursor moves; decide whether remote cursor moves also auto-expand (Tapestry has no
  notion of "remote").
- `seriate children` / `sort by confidence` depend on metadata coloom may not store yet; gate
  these menu items on data availability rather than porting unconditionally.
