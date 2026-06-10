# Canvas view — behavioral spec (from Tapestry-Loom source)

Source of truth:
- `src/editor/canvas.rs` (pannable/zoomable node-card tree; all line refs `canvas.rs:N`)
- `src/editor/shared/layout.rs` (Sugiyama layout engine + wire beziers; `layout.rs:N`)
- Supporting refs: `src/editor/shared/mod.rs` (`shared.rs:N` below), `src/editor/shared/weave.rs`
  (`weave.rs:N`), `src/editor/lists.rs` (`lists.rs:N`), egui 0.33.3 `containers/scene.rs`
  (`scene.rs:N`) and `style.rs` for theme defaults.

The Canvas is the "content graph of all expanded nodes within the weave" (Getting Started.md).
It renders every node as a wrapped-text card in a left→right layered tree, connected by bezier
wires, inside an egui `Scene` (free pan + zoom, no scrollbars).

---

## 0. Units and theme constants

Everything is sized off one unit:

| Symbol | Definition | Default value |
|---|---|---|
| `M` | row height of the Monospace text style (`ui.text_style_height(Monospace)`) | egui default Monospace is 12 pt; `M` ≈ 12 px logical (then everything is multiplied by `ui_scale`, default **1.25**) |
| `pad` | `= M` (canvas.rs:95) | ≈ 12 px |
| `CARD_W` | `spacing.text_edit_width * 1.2` (canvas.rs:733, 752) | `280 * 1.2 = 336` px |
| `CARD_MIN_H` | `M * 3` (canvas.rs:753) | ≈ 36 px |
| `WIRE_FRAME` | `spacing.interact_size.y * 2` (canvas.rs:217) | `18 * 2 = 36` px |
| `BTN` | `M * 1.75` square (canvas.rs:839-842, 879-882) | ≈ 21 px |

Theme slots used (values = egui dark theme defaults; Light/Solarized themes swap the whole
`Visuals` struct, `settings/mod.rs:126-133` — colors must stay theme-derived, never hardcoded):

| Slot | Dark default | Used for |
|---|---|---|
| `widgets.noninteractive.bg_fill` | gray(27) | card fill, `+`/`...` button fill |
| `widgets.inactive.bg_fill` | gray(60) | inactive card border, inactive wires |
| `widgets.inactive.fg_stroke` | 1.0 px, gray(180) | base stroke width; default node text color |
| `widgets.hovered.weak_bg_fill` | gray(70) | hovered card fill |
| `widgets.active.fg_stroke.color` | white | active-thread border + wire color |
| `selection.stroke.color` | rgb(192,222,255) | bookmarked + active border |
| `selection.bg_fill` | rgb(0,92,128) | bookmarked + inactive border |
| `interaction.tooltip_delay` | 0.5 s | hover-popup suppression window |

UI settings that gate behavior (`settings/mod.rs:34-104`, defaults):
`show_model_colors=true`, `override_model_colors=false`, `show_token_probabilities=true`,
`show_token_confidence=true`, `minimum_token_opacity=65.0` (%), `opened_by_default=false`,
`auto_scroll=true`, `node_sorting=Model`.

---

## 1. Layout pipeline

### 1.1 Cache + invalidation (full relayout, never incremental)

`CanvasView` caches (canvas.rs:36-54): the scene viewport rect, a `WeaveLayout`, a
`node id → CanvasNode {card rect, child ids, wire beziers, max_x, button_rect, button_line}`
map, the root list, the **active-thread id set**, a `last_changed: Instant`, and a `new` flag.

- `update()` (runs every frame before render): if `state.has_weave_changed || state.has_theme_changed`,
  clear `roots` (canvas.rs:84-86).
- `render()`: if `roots` is empty → run the **whole** layout pipeline (canvas.rs:256-263).

So: any weave mutation (add/delete/merge/bookmark/active-status/sort) or theme change triggers a
complete synchronous relayout of *all* nodes. Collapse/expand does **not** relayout — collapsed
subtrees keep their allocated space and simply aren't painted (see §4). Capacity hints assume up
to 16 384 nodes / 32 768 edges (canvas.rs:60-63).

### 1.2 Measuring card sizes (sizing pass)

For every node id (in reverse-DFS order, see §1.3) the card is rendered once into an invisible
sizing-pass Ui (`calculate_size`, canvas.rs:913-921) using the normal card renderer (§3) with the
inactive stroke. Resulting size obeys: width ≤ `CARD_W` (text wraps), width ≥ `CARD_W` via
`min_size` — i.e. **every card is exactly `CARD_W` wide**; height ≥ `CARD_MIN_H`, grows with
wrapped monospace text. The size handed to layout is `(h, w + 2*pad)` — node height, and width
padded by `pad` on each side (canvas.rs:121-125).

Web equivalent: measure each card's rendered height at fixed width 336 px (offscreen measurement
or canvas text metrics); cache per node content + theme.

### 1.3 Node ordering fed to layout

`dump_identifiers_ordered_u128_rev()` = DFS from each root visiting children **in reverse order**
(weave.rs:126-134, 145-152). This is the vertex order given to the Sugiyama solver; final sibling
vertical order is whatever Barycenter crossing-minimization produces from that seed order (not a
guaranteed mirror of weave child order).

### 1.4 Sugiyama arrangement (layout.rs)

`WeaveLayout.load_weave` maps ulids to dense u32 ids and collects `(parent→child)` edges
(layout.rs:31-58). `layout_weave(spacing)` with `spacing = pad * 3` (canvas.rs:129) calls
`rust_sugiyama::from_vertices_and_edges` with config (layout.rs:73-86):

```
minimum_length: 1, vertex_spacing: pad*3, dummy_vertices: false,
ranking_type: Up, crossing_minimization: Barycenter, transpose: false
```

The solver returns one subgraph per root (a forest). Subgraphs are placed side by side: running
`x_offset` starts at `spacing` and advances by `subgraph_width + spacing` per subgraph;
`y_offset = spacing` (layout.rs:88-129). Positions are node **centers**; each rect is
`center ± size/2`.

### 1.5 Transpose to left→right

The solver lays ranks along one axis; canvas swaps x↔y of every rect (canvas.rs:132-143) so the
final picture is: **roots at the left, depth increases rightward, siblings stack vertically**;
separate roots' trees are stacked vertically as independent blocks (the pre-transpose horizontal
subgraph offset becomes vertical). Nodes of equal depth share a column (per-rank coordinate from
the solver).

### 1.6 Per-node derived geometry (canvas.rs:156-239)

Given the transposed arranged rect `R` for a node:

| Field | Value |
|---|---|
| card `rect` | `R` inset horizontally by `pad` each side (`min.x+pad`, `max.x-pad`); full height (canvas.rs:160-169) |
| `button_rect` | x ∈ `[rect.max.x + pad, rect.max.x + 4*pad]`, same y-range as card (canvas.rs:170-179). Inner button is `BTN`² left-center-aligned in it. |
| `max_x` | `rect.max.x + 5*pad` (canvas.rs:180) — right edge of the **hover strip** |
| `button_line` | straight segment from `(rect.max.x, card center-y)` to `(button_rect.min.x, button-rect center-y)` (same y; canvas.rs:190-207); width = `inactive.fg_stroke.width` (1.0) |
| wire to parent | cubic bezier `wire_bezier_3(WIRE_FRAME, P, C)` stored **on the parent**, where `P = (parent card right edge, parent center-y)` and `C = (child card left edge, child center-y)` (canvas.rs:211-226) |

Wire color is decided at layout time: white (`active.fg_stroke.color`) if the **child** is in the
active thread, else gray (`inactive.bg_fill`) (canvas.rs:229-233). Width 1.0.

### 1.7 Wire bezier math (layout.rs:149-277, copied from egui-snarl)

`wire_bezier_3(f, from, to)` returns 4 cubic control points `[a,b,c,d]` taken from a 5-segment
construction `wire_bezier_5`:

- `from_2 = from + (f, 0)`, `to_2 = to − (f, 0)` (control handles extend horizontally by
  `f = WIRE_FRAME = 36 px`).
- Normal case (`from_2.x ≤ to_2.x` and `|from_2−to_2| ≥ 2f`): controls are `from_2`/`to_2` —
  a smooth horizontal S-curve.
- When endpoints are close or the child is left of the parent (backward edge), a battery of
  piecewise cases (layout.rs:157-270) bends the controls vertically (±`f` offsets, lerped by
  closeness factors) so the wire loops around instead of collapsing.

Web translation: port the function verbatim (pure math, ~80 lines); draw with
`<path d="M a C b c d">`.

---

## 2. Scene: pan / zoom / fit

The whole tree lives in an egui `Scene` with default config (scene.rs:76-84):

| Gesture | Effect |
|---|---|
| drag with **any** pointer button (primary/secondary/middle/extra), started on background or any non-interactive spot | pan (`translation += scaling * drag_delta`, scene.rs:230-241) |
| scroll wheel / trackpad two-finger scroll, pointer over canvas | pan by scroll delta (scene.rs:273-275) |
| pinch, or ctrl/cmd+scroll (egui `zoom_delta`) | zoom **about the pointer position**, clamped (scene.rs:256-271) |
| zoom range | `ε ..= 1.0` — arbitrary zoom-out, **never above 1:1** (text would blur; scene.rs:79) |

The viewport state is a single `scene_rect` (the region of scene-space shown), persisted across
frames (canvas.rs:37, 281-283). Rendering fits `scene_rect` into the available screen rect with
**letterboxing** (uniform scale = `min_elem(screen/scene_rect)`, centered; scene.rs:15-36).

- **Fit-to-weave** shortcut: set `scene_rect = Rect::ZERO` (canvas.rs:329-331); a zero/invalid
  rect makes Scene auto-reset to fit the entire content bounds (scene.rs:168-173).
- **Fit-to-cursor** shortcut: treat the cursor node as the changed node this frame
  (canvas.rs:290-292) → focus logic (§5). This bypasses pointer suppression (it's applied *after*
  the suppression check).
- Whenever `scene_rect` changed this frame (pan, zoom, focus, fit), `last_changed = now`
  (canvas.rs:333-335) — which suppresses hover popups for the next `tooltip_delay` (0.5 s, §3.4).

Web equivalent: a single `viewBox`-style transform (translate+scale) on an SVG/HTML layer;
pointer-capture drag for pan, wheel handler distinguishing plain scroll (pan) vs ctrl+wheel/pinch
(zoom-at-cursor); clamp scale to ≤ 1.

---

## 3. The node card

Rendered by `render_node` (canvas.rs:720-811) as a single egui Button with rich text inside.

### 3.1 Geometry & fill

- Width exactly `CARD_W` = 336 px; min height `3*M`; wrapped monospace text (canvas.rs:748-755).
- Fill: `noninteractive.bg_fill` (gray 27). If this node is the **shared hovered node** →
  `hovered.weak_bg_fill` (gray 70) (canvas.rs:757-759). Note: hover highlight is driven by the
  *shared* hovered-node signal, so hovering the node in any other view highlights it here too.

### 3.2 Border (stroke) state machine

Base stroke: width `inactive.fg_stroke.width * 1.5` = **1.5 px** (canvas.rs:114, 273, 277).
Resolution order (canvas.rs:563-567, 736-746):

| State | Color | Width |
|---|---|---|
| plain | `inactive.bg_fill` gray(60) | 1.5 |
| on active thread | `active.fg_stroke.color` white | 1.5 |
| bookmarked, not active | `selection.bg_fill` rgb(0,92,128) | 1.5 |
| bookmarked + active | `selection.stroke.color` rgb(192,222,255) | 1.5 |
| cursor node (any of the above) | same color | **×2 → 3.0** (canvas.rs:744-746) |

(Bookmark color *overrides* active color; cursor only doubles width.)

### 3.3 Text content & token shading

Text via `render_node_text_or_empty` (shared.rs:1240-1353):

- **Node color** (shared.rs:1049-1070): if `show_model_colors`, the node's model label color
  (hex in model metadata `color`), optionally globally overridden by
  `model_color_override`; fallback `inactive.fg_stroke.color` (gray 180). Human-typed nodes have
  no model → fallback color.
- **Token nodes**: one text section per token, color = node color with per-token opacity
  (shared.rs:1016-1047):

  ```
  prob_term  = 1 − ln(1/clamp(p, ε, 1)) / 10          # p=1→1.0, p=e⁻¹→0.9, p≈4.5e−5→0
  conf_term  = confidence / (ln(k) + 2)               # only if show_token_confidence and
                                                      # confidence & confidence_k present, else 1.0
  opacity    = clamp(min(conf_term, prob_term), minimum_token_opacity/100, 1.0)   # floor 0.65
  ```

  Applied as alpha on the node color (`change_color_opacity`, shared.rs:1355+).
- **Snippet nodes**: single section, node color.
- **Empty content** (zero-length snippet or no token bytes): literal text `"No text"` in the
  proportional Body font at monospace size, same color rules (shared.rs:1285-1311, 1335-1351).
  This is the *only* visual marker of blank nodes — no special border.
- Non-UTF-8 bytes are rendered lossy (U+FFFD) on the card.

### 3.4 Gestures on the card

| Gesture | Effect | Ref |
|---|---|---|
| hover (pointer inside card) | `set_hovered_node(node)` (shared signal); fill→hovered; arms the hover strip (§4); opens the hover popup (below) | canvas.rs:757-759, 771-773 |
| **left-click** | `weave.set_node_active_status(node, true)` (activate the whole root→node path) **and** `set_cursor_node(node)` | canvas.rs:775-778 |
| **right-click** | context menu (§3.6) | canvas.rs:767-769 |
| middle-click | nothing special on the card itself |
| drag started on card | nothing (button consumes click, not drag → drag pans the scene) |

Cursor-change side effect (shared.rs:497-505): whenever the shared cursor node changes, every
node on the root→cursor thread is force-`set_open(true)` — collapsed ancestors auto-expand.

### 3.5 Hover popup ("tooltip" that is actually the hover toolbar)

A `Tooltip` anchored to the card, **forced open while the pointer is inside the card** (or normal
egui tooltip rules apply), but globally gated by `show_tooltip = (now − last_changed) ≥ 0.5 s`
(canvas.rs:295-296, 780-785). Since `last_changed` resets on every relayout *and* every viewport
change, the popup never appears while panning/zooming/auto-scrolling, and reappears 0.5 s after
motion stops. egui keeps a tooltip alive while the pointer is over the tooltip itself
(`tooltip_grace_time` 0.2 s to travel to it); the popup body re-asserts
`set_hovered_node(node)` every frame it is shown (canvas.rs:788), keeping hover state alive
while the user is inside the popup.

Popup contents, top to bottom (canvas.rs:787-810):

1. Horizontal button row (`render_horizontal_node_label_buttons_ltr`, lists.rs:683-771 — icon
   glyphs are from the app's private icon font):
   | Button | Shown when | Click effect |
   |---|---|---|
   | merge (E43F) "Merge node with parent" | `weave.is_mergeable_with_parent(node)` | merge node into parent |
   | generate (E5CE) "Generate completions" / with modifier held: "…& focus node" | always | `generate_children(node)`; if **middle-click or any-modifier+click** also activate node + set cursor; always `set_open(node, true)` |
   | add (E40C) "Add node" / "Add active node" | always | create blank snippet child; child `active` = parent's active, **forced true** on middle/modifier-click; if active → cursor = new child, else `set_open(parent, true)` |
   | bookmark (E23C filled / E23D outline) "Bookmark node"/"Remove bookmark" | always | toggle `bookmarked` |
   | delete (E28F) "Delete node" | always | `weave.remove_node(node)` |
   | collapse toggle (E43C open / E43E closed) "Collapse/Expand node" | node has children (canvas.rs:792-794) | `toggle_open(node)` (canvas.rs:923-935) |
2. If the node is a **single-token** node: token detail block (canvas.rs:797-803) — the token's
   debug-quoted text in monospace, then `probability: NN.NN%`, `confidence: X.XX (k = K)`,
   `modified_boundaries: true` warning (in `warn_fg_color`) when `original_length ≠ byte len`,
   `token_id` (shared.rs:900-915, 978-1014).
3. Separator, then a collapsed-by-default `CollapsingHeader` **"Node Information"**
   (canvas.rs:805-809) → model label (in its label color), node metadata key/values
   (confidence formatted specially), node timestamp from the ulid, and (debug builds) the node
   id (shared.rs:859-898).

The "any modifier" rule: egui's `clicked_with_open_in_background()` = middle-click OR
left-click with any of ctrl/shift/alt/cmd held (egui response.rs:226-228). Getting Started.md
documents it as shift-click: "Shift clicking a node generation button will mark the parent of
the generated nodes as active."

### 3.6 Context menu (right-click; `render_node_context_menu` with `collapsing=true`, lists.rs:1179-1350)

Entries in order: **Generate completions** (same semantics incl. middle/modifier variant) ·
**Bookmark/Remove bookmark** · ─ · **Create child** / with modifier "Create active child" ·
**Create sibling** / "Create active sibling" (same blank-node + active/cursor rules; sibling of a
root = new root) · ─ · then if the node has children: **Collapse all children** / **Expand all
children** (canvas passes `collapsing=true`) · ─ · **Seriate children** · **Sort children by
confidence** · **Sort children by timestamp** · ─ · **Delete all children** · **Delete all
siblings** · **Merge with parent** (only if `is_mergeable_with_parent`) · ─ · **Delete**.

---

## 4. The strip right of the card: `+` generate, `...` expand stub

Each node owns an invisible **hover strip**: `x ∈ [card.max.x, max_x]` (width `5*pad` ≈ 60 px),
card's y-range. Hit-testing uses the raw pointer position transformed into scene coordinates
(canvas.rs:298-308) — it works over empty canvas, no widget needed.

Reveal condition `S(node)` (canvas.rs:576-594, 665-682):

```
S = mouse ∈ strip-rect
    OR (mouse ∈ card-rect AND shared hovered_node == node)
```

No timers/debounce — pure geometric containment, evaluated every frame. Moving the pointer from
the card into the strip keeps the control alive because the strip itself satisfies `S`.

Cases (canvas.rs:573-715), all only when the card's right edge is inside the viewport:

| Node state | What shows | When |
|---|---|---|
| open (expanded or leaf) | connector line + **`+` button** | only while `S` |
| closed, has children | connector line + **`...` expand stub** | **always** (no hover needed) |
| closed, no children | connector line + `+` button | only while `S` (degenerate: a leaf marked closed) |

Connector line color: white if the node is on the active thread, else gray(60); for the `...`
stub case the line is white if **any child** is on the active thread (canvas.rs:598-611,
633-649, 684-697).

**`+` generate button** (canvas.rs:864-911): `BTN`² square, label `+` at monospace size, fill
`noninteractive.bg_fill`, stroke = inactive node stroke; tooltip "Generate children". Click:
`state.generate_children(node)` (queues inference for the root→node thread; async, results land
as new child nodes in a later frame, see §6); **middle-click or modifier+click additionally**
activates the node and moves the cursor to it; always `set_open(node, true)`. There is no hover
fill change (dead code, canvas.rs:872-897).

**`...` expand stub** (canvas.rs:824-862): `BTN`² square, label `...`, tooltip "Expand node".
Click → `set_open(node, true)`. Hover sets the shared hovered node to the node's **first child**
(canvas.rs:832-834, 852-854) — a peek at what's hidden — and the stub uses the hovered fill when
that first child is the shared hovered node.

Collapse semantics: `is_open` defaults to `opened_by_default` (false) for never-touched nodes
(shared.rs:629-634); the open/closed map is shared editor state (all views) and is *temporary*
(not persisted in the weave). A closed node's subtree is skipped during painting only — layout
space remains allocated (blank gap).

---

## 5. Auto-focus / auto-scroll on changed node

Every frame (canvas.rs:250-262, 284-292, 325-335):

1. `changed_node = state.get_changed_node()` if `settings.interface.auto_scroll` (default on),
   else `None`. The shared "changed node" is, per frame, with this priority (shared.rs:464-489,
   521-525): the new **hovered** node if hover changed → overridden by the new **cursor** node if
   cursor changed → else the first inference-response node added this frame. (So hovering a node
   in *another* view makes the canvas glide to it — the cross-view "look here".)
2. First frame on a newly opened weave (`new` flag): `changed_node = cursor node`
   (canvas.rs:259-262).
3. **Pointer suppression**: if the pointer is anywhere inside the canvas's clip rect (and the
   scene rect is initialized), `changed_node = None` (canvas.rs:284-288) — the view never
   auto-scrolls under the user's own mouse. The Fit-to-cursor shortcut is applied *after* this
   check and therefore bypasses it (canvas.rs:290-292).
4. `traverse_and_focus` (canvas.rs:337-363) searches for the changed node among roots and **open**
   subtrees only (children of a closed node are not visited — a hidden changed node does not
   trigger focus). On match:

   ```
   scale = min_elem(viewport_size / node_rect_size)
   focus_rect = scale > 0.9 ? node_rect.scale_from_center(scale / 0.9) : node_rect
   ```

   i.e. zoom so the node renders at **90 % of native scale**, centered; if the node can't fit at
   0.9× it is fitted exactly to the viewport. `scene_rect = focus_rect` (canvas.rs:325-327) —
   an **instant jump** (`style.animation_time = 0` globally, settings/mod.rs:176-180; no easing).
5. Any scene-rect change resets `last_changed`, re-suppressing hover popups for 0.5 s (§3.4).

---

## 6. Painting order, culling, repaint

### 6.1 Two-pass z-order (canvas.rs:364-506)

Recursive descent from each root: for a node — `first_pass` (draw its **outgoing wires**), then
recurse into children (each child gets first_pass + its own recursion, then in a second loop its
`second_pass`), then the node's own `second_pass` (draw the **card + strip controls**). Net
effect: **all wires paint under all cards**; controls paint with their card.

### 6.2 Viewport culling (horizontal-only + per-rect)

| Check | Condition | Effect | Ref |
|---|---|---|---|
| left cull | `clip.min.x > max_x + (max_x − rect.min.x)` (node fully left of viewport with ~1 strip-width margin) | skip this node's paint, still recurse into children if open | canvas.rs:381-396, 446-462 |
| right cull | children/wires/strip painted only if `clip.max.x ≥ rect.max.x` (node's right edge visible) | subtree beyond the right edge not painted | canvas.rs:465, 523, 573 |
| rect visibility | `ui.is_rect_visible(rect)` per card / button rect | vertical + fine culling | canvas.rs:555, 575, 629 |
| `disable_culling` | first frame after a scene-rect reset (`last_rect == ZERO`) | paint *everything* so content bounds are measurable for fit-to-content | canvas.rs:319, 377 |

Closed nodes stop the paint recursion entirely (canvas.rs:384, 448, 464).

### 6.3 Repaint triggers (immediate mode)

egui only repaints on input; the shared state explicitly requests a repaint when any of
weave/layout/cursor/hover/theme/open-set changed (shared.rs:617-627). Hover-strip reveal and
popup forcing work because egui repaints on pointer movement anyway.

---

## 7. Shared state — reads and writes

| Signal | Canvas reads | Canvas writes |
|---|---|---|
| hovered node (one-frame-delayed: writes go to `hovered_node`, reads see `last_hovered_node`; cleared every frame, shared.rs:464-470) | card fill, strip reveal, stub fill | card hover, popup body, stub hover (→ first child) |
| cursor node | border ×2; initial/fit-to-cursor focus target | card click, modifier/middle `+` & generate, "create active child/sibling" |
| active thread (recomputed from weave at relayout, canvas.rs:148) | border & wire color, connector color | card click / modifier-click set `active=true` on the path |
| changed node | auto-focus (§5) | indirectly (hover/cursor writes) |
| open set (`is_open`, default `opened_by_default=false`) | paint recursion, focus recursion, stub vs `+` | stub click, `+` click, collapse toggle, context menu collapse/expand-all |
| `has_weave_changed` / `has_theme_changed` | relayout trigger | — |
| in-flight inference | — (no canvas spinner; request count shown elsewhere) | `generate_children` queues requests; responses are appended to the weave asynchronously by the shared update loop (shared.rs:519-585), then child sorting per `node_sorting` (Model by default) |

---

## 8. Edge cases

- **Empty weave**: nothing painted; `roots` stays empty so the layout pipeline reruns every frame
  (cheap no-op); scene auto-fit keeps the viewport sane.
- **Single child / leaf**: no special casing in canvas (lists/tree views have those); leaf nodes
  simply never show the `...` stub (`should_render_expand_button` requires non-empty `to`,
  canvas.rs:814-822).
- **Merge gating**: the merge button/menu entry only exists when `is_mergeable_with_parent`
  (parent has exactly this one child and content kinds are compatible — gate is in the weave
  lib; treat as a server-provided boolean per node).
- **Bookmarked vs active conflict**: bookmark color wins over active color (§3.2).
- **Hidden changed node**: a changed node inside a collapsed subtree triggers no focus (§5 step 4)
  — except cursor changes, which auto-expand the thread first (shared.rs:497-505).
- **Stale geometry panics**: canvas `unwrap()`s its node map (canvas.rs:345, 379); correctness
  depends on relayout-before-paint on every weave change. Web: rebuild layout atomically with the
  store snapshot — never paint a node set newer than the layout.
- **No-text nodes** render the literal `"No text"` placeholder (§3.3).
- **Zoom ceiling 1:1** also caps focus zoom (fit math clamps through `zoom_range`).

---

## 9. Web / Svelte translation notes

One line each; coloom-model deltas at the end.

- Immediate mode + `request_repaint` → Svelte 5 runes: all of §7 becomes `$state` in a shared
  store; rendering is reactive, no repaint management.
- Sizing pass (invisible Ui per node, every relayout) → measure card heights at fixed 336 px
  width with an offscreen DOM node or `CanvasRenderingContext2D.measureText`; cache by
  `(node content hash, theme, scale)`.
- `rust_sugiyama` → any JS layered-DAG layout (d3-dag sugiyama / elkjs) with: rank separation =
  `3*pad`, node sizes = measured `(w + 2*pad, h)`, barycenter ordering, no dummy-vertex routing;
  then transpose to left→right. Full relayout per mutation is fine at loom scale; debounce only
  if profiling demands.
- egui `Scene` → one transformed layer (`translate(...) scale(...)`); SVG paths for wires under
  an HTML layer for cards, or one SVG for both. Clamp scale ≤ 1. Letterboxed fit = uniform
  min-scale + centering.
- Pointer-in-scene math (`layer_transform.mul_pos`) → invert your view transform on
  `pointerimove`; the hover strip is a geometric hit-test, not a DOM element (or make it an
  invisible absolutely-positioned div per visible node).
- Tooltip-forced-open-while-hovering-card → a positioned popover that opens on card hover, stays
  while pointer is in card ∪ popover (CSS `:hover` on a common wrapper or a tiny
  pointer-tracking store), and is globally suppressed until 0.5 s after the last viewport change.
- `clicked_with_open_in_background` → `event.button === 1 || (event.button === 0 &&
  (event.ctrlKey||event.shiftKey||event.altKey||event.metaKey))`; mind browser middle-click
  autoscroll (`preventDefault` on `auxclick`/`pointerdown`).
- Culling → at loom scale start with none (browser handles a few hundred nodes); if needed, skip
  rendering cards whose rect ∩ viewport = ∅ using the same horizontal-first logic.
- Instant focus jump → coloom may animate (Tapestry sets `animation_time = 0`, so faithful =
  instant); keep the **pointer-suppression rule** either way.

**coloom model deltas:**

- **No single active path.** Tapestry's `active` set / white borders+wires become per-participant
  **cursor threads**: each named cursor induces a root→cursor-node thread; color wires/borders
  per cursor (participant color), with overlaps showing multiple indicators. "Click activates the
  path + moves cursor" becomes "click moves *my* cursor there" (server `PUT` cursor); moving
  someone else's cursor is an explicit separate gesture. The shared hovered node and changed-node
  auto-focus ("look here") map naturally onto cursor-move events arriving over WS.
- **Creator attribution replaces model-label colors**: `get_node_color` → color from the node's
  `Creator` (human vs model, per participant), same token-opacity math on top.
- **Server-canonical**: every write in §3.4/§3.6/§4 (activate, cursor, bookmark, add, delete,
  merge, generate) is a REST call; the weave-changed → relayout trigger becomes "WS event
  appended to the store". `generate_children`'s async response-landing is already the shape of
  coloom's flow; `last_changed_node` ≈ the node id in the WS event.
- Collapse set is client-local in Tapestry (shared across its subviews, lost on close); coloom
  can keep it per-client (matches Tapestry) — it is *not* weave state.
