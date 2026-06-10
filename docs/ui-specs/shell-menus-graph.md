# Tapestry-Loom UI spec — workspace shell, menus/info/status, graph minimap

Extracted from `src/editor/mod.rs`, `src/editor/menus.rs`, `src/editor/graph.rs` (+ the
shared/settings code they delegate to) of the Tapestry-Loom checkout at
`~/projects2/weird-personas/Tapestry-Loom` (branch `new-format`). All `file:line` references
are to that repo. Written for reimplementation in coloom's web UI without reading the Rust.

Companion specs (other views) live in sibling files under `docs/ui-specs/`.

---

## 1. Workspace shell (`src/editor/mod.rs`)

### 1.1 Panes and tiling

One **editor** = one weave document = one tiling workspace (`egui_tiles::Tree`). Eight pane
types (`mod.rs:575-584`), each existing exactly once, none closable (`mod.rs:775-777`):

| Pane | Tab title (icon glyph + text, `mod.rs:763-774`) | Content (Getting Started.md:156-174) |
|---|---|---|
| Canvas | `\u{E125} Canvas` | content graph of all *expanded* nodes |
| Graph | `\u{E52E} Graph` | minimap of *all* nodes (this spec, §3) |
| TreeList | `\u{E408} Tree` | treelist of expanded nodes near cursor |
| List | `\u{E106} List` | children of the cursor node |
| BookmarkList | `\u{E060} Bookmarks` | all bookmarked nodes |
| TextEdit | `\u{E265} Editor` | editable active text |
| Menu | `\u{E1B1} Menu` | inference parameter editor (§2.1) |
| Info | `\u{E0F9} Info` | weave notes + metadata editor (§2.3) |

Tiling behavior: panes are draggable between tab groups and splittable (egui_tiles default
drag behavior); simplification keeps every pane inside a tab bar and prunes single-child
containers (`mod.rs:778-784`). Tabs cannot be closed, only rearranged.

### 1.2 Default layout (`mod.rs:97-141`)

Root = horizontal split `[left | right]`:

- **Left tab group**: Canvas, Graph, Tree, List, Bookmarks — **Tree active by default**
  (`active_left_tab = left_tabs[2]`, `mod.rs:106`).
- **Right side** depends on whether the editor was opened from a file:
  - opened from file (`path.is_some()`): one tab group `[Editor, Menu, Info]`
    (`mod.rs:115-122`), Editor active (first).
  - new/temporary weave: vertical binary split, **upper 60%** tab group `[Editor, Info]`,
    **lower 40%** tab group `[Menu]` (`Linear::new_binary(Vertical, …, 0.6)`,
    `mod.rs:124-139`) — i.e. a fresh weave shows the inference params alongside the editor.

Split ratio of root horizontal split is the egui_tiles default (50/50). Per-editor layout is
**local + temporary** state (Getting Started.md:144-152): lost when the editor closes.

### 1.3 Editor title (`mod.rs:536-547`)

| Condition | Title |
|---|---|
| has path | file stem (filename minus extension) |
| has path but no extractable stem | `Editor` |
| no path | `New Weave` |

Title regenerates whenever the path changes (`mod.rs:329-341`).

### 1.4 Load / error surface (`mod.rs:232-323`)

While the weave is loading (background thread), the workspace renders only a bottom panel
with a spinner + `Loading weave...` (`mod.rs:307-318`). Load failures surface as **toast
errors** and fall back to a blank in-memory weave with no path (so the user keeps a working
editor): `"Weave deserialization failed"`, `"Invalid weave header"`, `"Filesystem error:
{e}"` (`mod.rs:262-297`). All later async errors funnel through the same toast channel
(`mod.rs:240-243`, `320-323`).

*coloom*: load = initial REST fetch; same pattern applies (spinner panel → toast on error),
no fallback-to-blank needed since the server is canonical.

### 1.5 Bottom status bar (`mod.rs:343-379`)

A single bottom panel per editor, two halves:

- **Left, LTR** (`mod.rs:348-371`):
  - if saved: file path **relative to the documents root** (falls back to absolute); hover →
    tooltip with full path; right-click → context menu with one item, **Copy path** (copies
    full path to clipboard).
  - if unsaved: a **`Save as...` button** → opens the save modal (§1.6).
- **Right, RTL** (`mod.rs:373-376` → `menus.rs::render_rtl_panel`, spec §2.2): live request
  indicator or node statistics.

The RTL half is also where each view's per-frame `update()` is driven (`mod.rs:615-682`) —
an egui implementation detail, see §6.

### 1.6 Save-as modal (`mod.rs:385-430`)

| Property | Value |
|---|---|
| width | 280 px (`mod.rs:388`) |
| heading | `Save Weave` |
| input | label `Path:` + single-line text field, prefilled `Untitled.tapestry` (`mod.rs:161`) |
| buttons | `Cancel`, `Save` (right-aligned) |
| Enter key | same as Save (`mod.rs:400-401`) |
| Save validation | rejected (silently, modal stays open) if: input empty; extension-less path after appending `.tapestry` is already open in another editor; file already exists on disk (`mod.rs:402-411`) |
| extension | `.tapestry` appended if input has no extension (`mod.rs:406-408`) |
| on success | path set, modal closes, immediate save (`mod.rs:420-427`) |

### 1.7 Autosave + close

- **Autosave** every `settings.documents.save_interval`, **suppressed while any inference
  request is in flight** (`get_request_count() != 0`) (`mod.rs:432-439`).
- **Close** (tab close, or `Cmd+W` = `CloseFocusedTab` shortcut, only when the pointer is
  over this editor, `mod.rs:196-203`, default binding `shortcuts.rs:136-139`):
  - saved weave → save + unload silently (`mod.rs:513-533`);
  - unsaved **non-empty** weave → confirmation modal: heading *"Do you want to close this
    weave without saving?"*, body *"All changes made will be lost."*, buttons **Yes**
    (close, discard) / **No** (`mod.rs:205-230`); width 210 px.
  - unsaved **empty** weave → closes silently (`unsaved()`/emptiness check
    `mod.rs:501-512`).

*coloom*: no save/close lifecycle (server-canonical, every mutation persisted) — drop §1.6,
autosave, and the close-confirmation entirely. Keep the status bar (path → weave name) and
the statistics/request indicator.

---

## 2. Menu pane, Info pane, status-bar statistics (`src/editor/menus.rs`)

### 2.1 Inference parameter pane (Menu)

Container: vertical scroll area, auto-shrink off, no scroll animation, standard menu margin
(`menus.rs:30-39`). Content is the shared `InferenceParameters` editor
(`settings/inference/mod.rs:500-627`). **This state is per-editor and temporary**: it is
cloned from settings defaults when the editor opens (`shared/mod.rs:114`,
Getting Started.md:39) and never persisted into the weave.

Top-to-bottom:

1. **Presets row** — only rendered if presets exist in settings (`inference/mod.rs:501-523`).
   A boxed group with wrapping horizontal buttons:
   - first button: reset icon (`\u{E148}`) → replace the whole parameter state with the
     settings default (`inference/mod.rs:504-506`);
   - then one button per preset, in settings order; button label text colored with the
     preset's optional color; click → replace the whole parameter state with the preset
     (`inference/mod.rs:508-519`).
2. **Recursion slider** (`inference/mod.rs:528-533`): range 0–3, clamping *off* (typed
   values may exceed 3), label `Recursion`, suffix ` layers`, hover tooltip: *"The recursion
   depth used for generating nodes. If this is > 0, nodes will be recursively generated up
   to the set number of layers."* Request count grows exponentially with depth
   (Getting Started.md:182).
3. **One boxed group per configured model** (`inference/mod.rs:455-487`, order = request
   order; multiple models may be active simultaneously):
   - row 1 (wrapping): **request-count drag-value** with suffix `x`, min 1, hover tooltip
     `Request count`; **model dropdown** (width 0.6 × standard text-edit width; entries are
     all models from settings, each rendered in its label color; unknown id shows `Invalid
     model`); then per-row buttons: move up `\u{E44E}` (hidden on first), move down
     `\u{E44D}` (hidden on last), copy `\u{E09E}` (duplicates the entry in place), delete
     `\u{E18E}` (`inference/mod.rs:543-573`).
   - row 2: label `Request parameters:` + **editable key/value map**
     (`render_config_map`, `inference/mod.rs:1170-1204`): one wrapping row per pair with two
     single-line text fields (hint texts `key` / `value`; widths 0.55 / 0.45 × text-edit
     width) and a remove button `\u{E28F}` (hover `Remove item`); below the rows an add
     button `\u{E13D}` (hover `Add item`) appends an empty pair. Free-form strings —
     anything (temperature, max_tokens, logprobs, …) is passed through to the backend.
4. **Add-model dropdown** at the bottom (`inference/mod.rs:599-626`): placeholder `Choose
   model...`; selecting a model **immediately** appends an entry with `requests = 5` and the
   endpoint's default parameters, then resets the dropdown to placeholder.

**Keyboard shortcuts** acting on this state (handled in the status-bar code path,
`menus.rs:95-137`, applied regardless of pane visibility):

| Shortcut | Default binding (`shortcuts.rs:90-115`) | Effect |
|---|---|---|
| ParameterPreset1–5 | `Cmd/Ctrl+1` … `Cmd/Ctrl+5` | load preset N (1-indexed; no-op if absent, `inference/mod.rs:495-499`) |
| ParameterPreset6–10 | unbound by default | same |
| ResetParameters | unbound by default | reload settings defaults (`inference/mod.rs:492-494`) |

### 2.2 Status-bar right half: request indicator / statistics (`menus.rs:42-138`)

Two mutually exclusive modes, switched on the in-flight request count
(`get_request_count()` = generation + seriation requests, `shared/mod.rs:810-812`):

- **Requests in flight** (`menus.rs:58-69`): spinner + label `1 request` / `{n} requests`.
  **Hovering the label reveals a hover-popup containing a `Cancel requests` button**; click
  → cancel **all** in-flight requests and drop already-buffered unprocessed responses
  (`shared/mod.rs:813-818`). The popup lives as long as pointer is over label/popup (egui
  `on_hover_ui` semantics; no explicit debounce in source).
- **Idle** (`menus.rs:70-92`): text statistics, comma-separated:
  - `{nodes} nodes, {active} active` and, only when ≥1 bookmark exists,
    `, {bookmarked} bookmarked`;
  - numbers compacted by `format_large_number` (`main.rs:641-655`): ≥100M → `{x:.0}M`;
    ≥1M → `{x:.1}M`; ≥100k → `{x:.0}k`; ≥1k → `{x:.1}k`; `1` → singular suffix
    (`1 node`); else plain;
  - **hover** on the statistics label (only when a file size is known, i.e. saved at least
    once) → tooltip with the on-disk size, `format_file_size` (`main.rs:677-693`):
    ≥100 GB `{x:.0} GB`, ≥1 GB `{x:.1} GB`, ≥100 MB `{x:.0} MB`, ≥1 MB `{x:.1} MB`,
    ≥100 kB `{x:.0} kB`, ≥1 kB `{x:.1} kB`, else `{n} bytes`.
  - perf note: the active-thread length is cached and recomputed only when the weave
    changed or the cache is 0 (`menus.rs:54-56`).

### 2.3 Info pane: notes + weave metadata (`menus.rs:155-205`)

Vertical scroll area, menu margin. Two boxed groups:

1. **Notes** (`menus.rs:171-181`) — rendered only when the metadata map has a `notes` key:
   label `Notes:` + multiline text editor bound directly to `metadata["notes"]`, width 2 ×
   standard text-edit width, Tab inserts a tab character (focus locked). Edits hit the weave
   immediately (every frame).
2. **Metadata** (`menus.rs:184-202`) — label `Metadata:` + the same editable key/value map
   widget as §2.1 (`render_config_map`, key/value widths 0.9 / 1.1 × text-edit width, with
   per-row remove + trailing add button), over **all metadata entries except `notes`**.
   After rendering, the (possibly edited) entries are written back with the preserved
   `notes` value re-appended at the end — so `notes` cannot be deleted via this editor, and
   a user-typed row with key `notes` is overwritten by the preserved value on the same
   frame.

**Edge case (source behavior, possibly unintended):** the write-back appends
`("notes", get("notes").unwrap_or(""))` unconditionally (`menus.rs:196-201`), so merely
rendering the Info pane once **creates an empty `notes` key**, which makes the Notes editor
appear from the second frame onward. Net effect for reimplementation: the Notes editor is
effectively always present once the Info pane has been opened.

---

## 3. Graph pane — zoomed-out minimap (`src/editor/graph.rs`)

A plot-style 2D viewport (egui_plot) showing **every node in the weave** as a unit square,
intended as an overview/teleport map.

### 3.1 Viewport & navigation (`graph.rs:212-220`)

| Property | Value |
|---|---|
| axes / grid / background / coordinate labels | all hidden |
| axis direction | **both X and Y inverted** (`invert_x(true)`, `invert_y(true)`) — layout coordinates grow toward the top-left of the screen |
| aspect | `data_aspect(1.0)` — squares stay square at any zoom |
| pan | pointer drag (egui_plot default) |
| zoom | scroll wheel / pinch, cursor-anchored (egui_plot default) |
| reset | double-click → fit-all (egui_plot default auto-bounds) |
| fit-all shortcut | `FitToWeave`, default `Cmd/Ctrl+0` → auto-bounds (`graph.rs:288-290`, `shortcuts.rs:132-135`) |
| fit-cursor shortcut | `FitToCursor`, default `Cmd/Ctrl+9` → recenter on cursor node (`graph.rs:208-210`, `shortcuts.rs:128-131`) |

(Pan/zoom/double-click are egui_plot built-ins, not configured in this file; exact gesture
nuances beyond drag-pan/scroll-zoom/double-click-reset are not specified by this source.)

### 3.2 Layout (`shared/layout.rs:21-146`)

- Algorithm: **Sugiyama layered DAG layout** (`rust_sugiyama`), config: `minimum_length 1`,
  `vertex_spacing 1.5`, no dummy vertices, `RankingType::Up`, barycenter crossing
  minimization, no transpose (`layout.rs:73-86`).
- Every node has size **(1.0, 1.0)** in plot units (`graph.rs:176-181`); spacing 1.5 ⇒
  0.5-unit gutters between adjacent unit squares.
- Edges fed to layout = `parent → child` for every node with a parent (`layout.rs:44-55`).
- **Disconnected subgraphs (multiple roots/trees) are laid side by side** along x, each
  offset by the previous subgraph's width + 1.5 (`layout.rs:88-129`). Total bounds get a
  1.5/3.0-unit margin (`layout.rs:131-136`).
- Output: `positions: node → (x, y)` center points (+ per-node rects, unused by this view).

### 3.3 Visual encoding (`graph.rs:61-157`, paint order = list order)

Painted back-to-front in four classes (z-order matters):

| # | Item | Geometry | Color | Stroke/width |
|---|---|---|---|---|
| 1 | edges, child **not** on active thread | straight segment parent-center → child-center | `widgets.inactive.bg_fill` (egui dark default ≈ `#3c3c3c`/gray 60) | line width **2.0** |
| 2 | edges, child **on** active thread | same | `widgets.noninteractive.fg_stroke.color` (dark default ≈ gray 140) | line width **2.0** |
| 3 | node | axis-aligned unit square, corners (x±0.5, y±0.5) | fill = **model label color** (`get_node_color`) else `widgets.inactive.text_color()` (dark default ≈ gray 180) | hover/cursor outline, see §3.4 |
| 4 | bookmark ribbon (only if node bookmarked) | 6-gon: (x±0.25, y−0.35) → (x±0.25, y+0.35) with an inner notch vertex at (x, y+0.2) — a "bookmark" tag with a V-cut, drawn inside the square | fill = `panel_fill` (panel background; dark default ≈ `#1b1b1b`) — i.e. a *cut-out* glyph | none |

"Active thread" = the set of node ids on the current active path (`graph.rs:62`). Active
edges are pushed after all inactive edges so the active path always paints on top.

Node fill (`shared/mod.rs:1049-1070`): only if `interface.show_model_colors` (default on);
if `override_model_colors` + an override color is set, every *model-generated* node gets the
override and human nodes get the default; otherwise the color comes from the node's model
`color` metadata (hex). Nodes without a model (human-written) always use the theme default
fill.

### 3.4 Hover / cursor outlines and gestures (`graph.rs:194-326`)

Hit test: pointer plot-coordinate must be inside the current plot bounds **and the view must
not be being dragged** (`graph.rs:223-227`); a node is hit when the pointer falls inside its
unit square (`graph.rs:252-255`). The hit node is `pointer_node`.

Outline rules (else-if priority chain, `graph.rs:252-262`):

| Priority | Condition | Stroke |
|---|---|---|
| 1 | node under pointer (this view) | width **2.75**, color `widgets.noninteractive.fg_stroke.color` (same as active-edge color) |
| 2 | node == shared hovered node (hovered in *any* view) | same hover stroke |
| 3 | node == shared cursor node | width **2.75**, color `widgets.inactive.bg_fill` (same as inactive-edge color — a dimmer outline) |

Gestures on the hit node:

| Gesture | Effect | Ref |
|---|---|---|
| left click | mark node active (activates its path) **and** set the shared cursor node to it | `graph.rs:305-309` |
| right click | node context menu (the shared node context menu, same as list views — generate/add/bookmark/delete/etc.; specified in the lists/canvas spec) | `graph.rs:311-316`, `337-347` |
| hover (no context menu open) | (a) tooltip at pointer, (b) publish node as the shared hovered node | `graph.rs:318-325` |

Context-menu persistence: the node that opened the menu is cached so the menu keeps
rendering for that node while open, even if the pointer leaves it; cache cleared when the
menu closes (`graph.rs:28`, `295-303`, `311-316`). The hover tooltip is suppressed while a
context menu is open (`graph.rs:318`).

Tooltip content (`graph.rs:349-366`):

1. node text, colored per node/token colors (tokens get probability-opacity shading, §5);
   non-UTF8 single-token nodes render as a byte-array debug string in the noninteractive
   text color (`shared/mod.rs:1141-1198`);
2. if the node is a **single-token** node: token metadata block — `probability: {p:.2}%`,
   `confidence: {c:.2} (k = {k})`, `token_id` (only when boundaries unmodified), a
   warn-colored `modified_boundaries: true` when the stored length differs, plus any other
   keys verbatim (`shared/mod.rs:979-1015`);
3. separator;
4. node metadata block — model label in its label color, node metadata key/values
   (confidence pretty-printed with k/n), node creation timestamp derived from the ULID; node
   id shown in debug builds only (`shared/mod.rs:859-898`).

### 3.5 Auto-recenter & pointer suppression (`graph.rs:168-292`)

`fitting_node` (an explicit "center on this node" request) is set when:

- first layout of the weave (initial open / structure change): center on **cursor node**
  (`graph.rs:174-184`);
- recolor-only refresh **and the pointer is not over the graph pane**: center on cursor node
  (`graph.rs:185-190`);
- `FitToCursor` shortcut (`graph.rs:208-210`).

While drawing each node (`graph.rs:264-274`), the view recenters the plot bounds on the node
when **either**:

- `node == fitting_node`, or
- *auto-follow*: `interface.auto_scroll` is on (default on, toggle shortcut `Cmd/Ctrl+D`,
  `shortcuts.rs:119-122`) **and** the pointer is outside the plot bounds **and** there is no
  explicit fitting node **and** `node == last-changed node` (§4).

Recenter sets the visible window to **screen-width/15 × screen-height/15 plot units**
centered on the node (`graph.rs:229-233`, `270-273`) — i.e. it also *zooms* to a fixed
"about 15 columns visible" scale, regardless of previous zoom.

**Pointer-suppression summary** (the view never yanks the viewport out from under an
interacting user):

| Suppressed thing | Condition |
|---|---|
| recenter after recolor | pointer anywhere over the graph pane's clip rect (`graph.rs:170-172`, `185-189`) |
| auto-follow of changed node | pointer inside plot bounds (`pointer.is_none()` check, `graph.rs:264-266`) |
| hover hit-testing | view currently being dragged (`graph.rs:226`) |

`FitToCursor`/first-layout recentering is *not* pointer-suppressed.

### 3.6 Two-level cache & invalidation (`graph.rs:23-60`, `159-190`)

| Level | Contents | Invalidated when | Rebuild cost |
|---|---|---|---|
| **Layout** (`arranged`) | node → (x,y) positions | `has_weave_layout_changed` — node/edge *structure* changed (add/delete/reparent/merge/split) (`graph.rs:54-56`) | full Sugiyama re-layout, then recolor; recenters on cursor unconditionally |
| **Items** (`items`) | precomputed colored edges/squares/ribbons | `has_weave_changed` (any weave mutation incl. active-path/bookmark changes) **or** `has_theme_changed` (`graph.rs:57-59`) | recolor only (positions reused); recenters on cursor only if pointer not over pane |

Both flags are one-frame pulses computed centrally per frame (`shared/mod.rs:616-617`).
Note active-path and bookmark changes invalidate only the color cache because
active/bookmark state is baked into edge colors and ribbon glyphs.

Empty weave: layout yields no items; the pane renders an empty viewport (the
`items.is_empty()` branch then re-runs the cheap recolor every frame — harmless no-op;
don't replicate).

---

## 4. Shared state read/written by these views

Shared signal semantics (`shared/mod.rs:51-101`, `464-504`, `643-657`): all are
**editor-scoped, temporary** (not persisted in the weave).

| Signal | Type | Graph reads | Graph writes | Menu/status reads |
|---|---|---|---|---|
| cursor node | node id (or position-within-node) | recenter target, dim outline | set on node click | — |
| hovered node | node id | hover outline (priority 2) | set on node hover; **cleared globally every frame** — a view must re-assert hover each frame (`shared/mod.rs:472`) | — |
| last-changed node | node id, one-frame pulse | auto-follow target | — | — |
| has_weave_changed / has_weave_layout_changed | one-frame pulses | cache invalidation | — | stats cache refresh (`menus.rs:54`) |
| has_theme_changed | pulse (UI settings diff, `shared/mod.rs:488-493`) | recolor | — | — |
| in-flight request map | id → handle | — | — | count, cancel-all |
| inference parameters | full param struct | — | — | read+write (the whole §2.1 editor) |

**last-changed node** is set, in priority order each frame (`shared/mod.rs:466-487`,
`522-525`): (1) hovered node when hover changed, (2) cursor node when cursor changed, (3)
the first freshly inserted generated node (only if nothing else changed). Consequence: with
auto-scroll on, the graph (pointer away) re-centers when you hover/click nodes in *other*
panes and when generations land — this is the cross-pane "follow" behavior.

Cursor fallbacks (`shared/mod.rs:473-482`): a cursor pointing at a deleted node resets to
none; a none cursor snaps to the first node of the active thread.

---

## 5. Token probability → opacity (used by tooltip text here; global rule)

`get_token_color` (`shared/mod.rs:1017-1047`), applied to a token's text color over the
node's base color, when `show_token_probabilities` (default on):

```
p_term    = 1 + ln(clamp(p, ε, 1)) / 10            # p = token probability
conf_term = confidence / (ln(k) + 2)               # only if show_token_confidence (default on)
            (else 1.0)                             # k = confidence_k metadata
opacity   = clamp(min(conf_term, p_term), minimum_token_opacity/100, 1.0)
```

`minimum_token_opacity` default **65** (`settings/mod.rs:93`) ⇒ opacity ∈ [0.65, 1.0];
p = 1 ⇒ 1.0; p ≈ 3% ⇒ ~0.65 floor. Tokens without a probability keep the node color
unmodified.

---

## 6. Web translation notes (one line each unless coloom diverges)

- **Immediate mode / one-frame pulse flags** (`has_*_changed`, hover cleared every frame):
  replace with Svelte 5 runes/stores + WS events; hover is a plain `$state` that
  pointer-leave clears, not a per-frame re-assertion.
- **`request_repaint` / `request_discard`** (`mod.rs:304`, `shared/mod.rs:626`): rendering
  bookkeeping, no web equivalent needed.
- **egui_tiles tiling**: any web dock/split library (or fixed CSS grid for the first slice);
  preserve the default layout of §1.2 and the "Tree active by default" choice.
- **egui theme colors** (`inactive.bg_fill`, `noninteractive.fg_stroke`, `panel_fill`,
  text colors): define as CSS custom properties; the dark-theme values quoted in §3.3 are
  egui defaults (approximate) — pick coloom theme tokens with the same *roles* (dim
  structure / mid-contrast highlight / background cut-out).
- **egui_plot pan/zoom**: SVG/canvas with a zoom-pan transform (e.g. d3-zoom);
  double-click-reset and cursor-anchored wheel zoom must be reproduced manually.
- **Hover popup with button** (`on_hover_ui`, §2.2): CSS hover popover where the popover is
  a child of the hover target so pointer can travel into it without it closing.
- **File lifecycle** (§1.4–1.7): drop entirely; coloom mutations are REST calls, "saving" is
  meaningless, errors come from HTTP/WS and still surface as toasts.
- **Active path → per-participant cursors (coloom model)**: there is no single active path.
  Per view feature:
  - graph "active-thread edges" → one highlighted root→cursor thread **per participant
    cursor**, each in that participant's cursor color (multiple threads may be lit at once);
  - graph cursor outline → one outline per cursor, colored per participant;
  - graph click "activate + set cursor" → `move my cursor here` (REST move-cursor; others
    see it via WS); moving *someone else's* cursor is a separate explicit action (context
    menu), not the default click;
  - status-bar `{active} active` → thread length of *my* cursor (or show per-cursor counts);
  - shared `last_changed_node` auto-follow → follow *my* cursor moves + new nodes; following
    *another participant's* cursor should be opt-in ("follow X" mode), or it becomes a
    fighting-for-the-viewport bug.
- **Model label color → creator attribution**: coloom colors nodes by `Creator`
  (human participant vs model); `get_node_color` maps to "creator color", and
  `override_model_colors` ≈ a "flat color mode" toggle. Human-authored nodes get the
  human participant's color instead of TL's "no color" default.
- **In-flight request indicator**: requests are server-side and multi-participant in coloom;
  the count should aggregate all participants' active generations (WS `generation_started/
  finished` events), and cancel-all is a server endpoint — consider scoping the button to
  "cancel mine".
- **Inference params per editor, non-persistent** (§2.1): in coloom these are client-local
  UI state seeding each generate request; presets can stay client-side (localStorage) until
  shared presets are wanted.
- **Info pane metadata editor**: maps to weave-level metadata over REST; replicate the
  "notes is always present and undeletable" behavior deliberately rather than via the §2.3
  write-back quirk; debounce writes (TL writes every keystroke-frame).
- **Number-key presets / shortcuts**: `Cmd/Ctrl+1..5` presets, `Cmd/Ctrl+9` fit-cursor,
  `Cmd/Ctrl+0` fit-weave, `Cmd/Ctrl+D` toggle auto-scroll, `Cmd/Ctrl+W` close — beware
  browser-reserved combos (`Ctrl+W`, `Ctrl+1..9` switch tabs in most browsers); pick
  alternates rather than preventDefault fights.
