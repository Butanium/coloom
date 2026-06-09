# Tapestry-Loom viewer feature audit — for coloom's web UI

**Purpose:** consciously pick features for coloom's web frontend (`web/`, Vite + Svelte) rather than rediscover them. Tapestry-Loom is the feature baseline for the viewer (Clément's call, 2026-06-09; see `docs/PLAN.md` "Web frontend").
**Source:** full read of `~/projects2/weird-personas/Tapestry-Loom` (branch `new-format`, audited 2026-06-09) — all of `src/editor/` (canvas, graph, lists, textedit, menus, shared), `Getting Started.md`, and the README roadmap. File:line refs below are relative to that checkout.
**Legend:** each feature is tagged **core** (v1 of coloom's web UI), **later** (worth it, not first), or **skip** (with reason). Judgments are calibrated for a *collaborative web* loom — coloom's differentiator is live human + agent co-weaving over the existing REST + WS backend, not desktop-app parity.

---

## 1. Workspace shell

- **[later] Drag-rearrangeable tiling workspace with tabbed panes** — egui_tiles layout hosting 8 subviews (Canvas, Graph, Tree, List, Bookmarks, Editor, Menu, Info); new-weave layout pre-splits to surface inference settings. v1 should ship a *fixed* two/three-pane layout (tree + text + generation controls); user-rearrangeable tiling is real work on the web and not where coloom's value is. (`src/editor/mod.rs:97-186, 686-785`; `Getting Started.md:156-174`)
- **[skip] Files / Settings / Editors top-level view triad** — desktop-app shell with multiple open documents. coloom is server-backed: a weave picker route + one editor route replaces it. (`Getting Started.md:3`)

## 2. Tree & graph navigation

- **[core] Canvas view: pannable/zoomable node-card tree** — infinite 2D scene, left-to-right tree of content-sized node cards, multiple roots laid out independently. This is the primary weave visualization; coloom's PLAN names "tree/graph view" as v1. (`canvas.rs:88-336`)
- **[core] Layered DAG auto-layout (Sugiyama)** — shared layout engine, variable node sizes in / positions out, disconnected roots side by side. Web equivalent: dagre/elk (or a small bespoke tree layout while coloom is tree-only). (`shared/layout.rs:12-146`)
- **[core] Bezier parent→child wires with active-path coloring** — edges on the active thread drawn in the active color, painted under the cards; edge color decided by the child's thread membership. Cheap and high-signal. (`canvas.rs:211-237, 507-537`; routing details `shared/layout.rs:148-277`)
- **[core] Node card visual states** — stroke/fill encodes: on-active-thread, bookmarked, cursor node (double stroke), hovered (fill swap). Four orthogonal states, one glance. coloom adds creator color as a fifth channel (see §3). (`canvas.rs:735-761, 272-279, 563-567`)
- **[core] Click-to-select = activate + focus** — single click both re-routes the active thread through the node and moves the cursor there; same gesture everywhere (canvas, graph, lists). (`canvas.rs:775-778`; `lists.rs:1008-1177`; `graph.rs:305-309`) ⚠️ **Open design question for coloom:** in Tapestry, navigation *mutates* the active path ("browsing IS weaving") — fine single-user, contentious when a human and an agent share one weave. Decide consciously whether the active path is shared or per-client before copying this. Genuinely unsure of the right answer; flagging rather than guessing.
- **[core] Hover-revealed '+' generate button at node tails** — appears over the node or the empty strip right of it; click = generate children; middle-click variant also focuses there. The single most-used loom gesture. (`canvas.rs:573-714, 864-911`)
- **[core] Per-node subtree collapse/expand with '…' stub** — collapsed nodes with children show an always-visible '…' button; stub line drawn in the active color when the active path continues inside; hovering pre-highlights the first hidden child. Collapse state is shared across views, not persisted in the weave. (`canvas.rs:629-664, 814-862`; `shared/mod.rs:66-68, 436-462`; `Getting Started.md:142`)
- **[core] Cross-view hover sync** — hovered node tracked globally by id; hovering in any pane highlights the node in all panes, bidirectionally. The app's nicest affordance, nearly free with a Svelte store; in coloom it extends naturally to broadcasting the *other participant's* hover (see §11). (`graph.rs:324`; `shared/mod.rs:57-60`; `Getting Started.md:132-154`)
- **[core] Auto-scroll / focus-follow on changed node, with pointer suppression** — when a generation lands, views pan/scroll to it — *unless the pointer is inside that view*. The pointer-suppression rule is the key insight, and it's exactly the contention coloom hits with two live participants: never steal the viewport from a user who's interacting. (`canvas.rs:250-291, 337-363`; `lists.rs:1090-1097`; `textedit.rs:197-224`)
- **[later] Fit-to-cursor / fit-to-weave shortcuts** — Cmd/Ctrl+9 / Cmd/Ctrl+0, identical in canvas and graph. (`canvas.rs:290-331`; `graph.rs:208-290`; `settings/shortcuts.rs:128-135`)
- **[later] Graph view: zoomed-out minimap** — whole weave as unit squares + straight edges, no text; model-colored squares, bookmark-ribbon cutout glyphs, hover/cursor outlines, auto-recenter, no collapse state. Great structural overview once weaves get big; not needed before the canvas works. Includes its two-level cache (re-layout vs recolor) — worth mirroring when built. (`graph.rs:46-366`)
- **[later] Tree list view with sliding window around cursor** — renders parent/grandparent of the cursor as display root, depth-capped with "Show more" re-centering and "Show parents". The key trick for keeping huge trees navigable and cheap; adopt when the canvas alone stops scaling. (`lists.rs:301-681, 863-978`)
- **[later] Flat list view (children of cursor)** — a siblings/continuations browser for the focus point. (`lists.rs:37-165`)
- **[later] List virtualization** — virtualized scrolling, per-branch virtual lists for ≥10 children, pre-allocation for 16k nodes. Required at scale; premature for v1. (`lists.rs:99-126, 301-563`)
- **[later] Viewport culling + cached canvas layout** — layout computed once, invalidated on weave/theme change; offscreen subtrees culled per-axis. Tapestry's own implementation is acknowledged-hairy; do the web-native version (cache layout keyed on weave version, cull by bounding box) when perf demands it. (`canvas.rs:84-87, 364-506`)

## 3. Node display & token/logprob rendering

- **[core] Per-node color by creator/model** — every node tinted by its creator's color in every view; model-less (human) nodes default. Includes the override mode painting all model nodes one uniform color — i.e. a binary human-vs-AI view. This is literally coloom's `Creator` attribution; for coloom the human-vs-agent palette is the *default*, not an option. (`shared/mod.rs:1049-1070`; `graph.rs:102-126`; `Getting Started.md:17, 65`)
- **[core] Token probability as text opacity** — confident tokens opaque, unlikely ones fade (`1 - ln(1/p)/10`, clamped by a minimum-opacity setting). The at-a-glance "where was the model unsure" heat view; coloom's `Tokens.logprob` feeds it directly. (`shared/mod.rs:1017-1047`)
- **[core] Rich hover tooltips: token + node metadata** — token tooltip: debug-escaped text, probability %, token_id (only when boundaries unmodified); node tooltip: model label in its color, metadata key/values, creation timestamp. Tooltip-as-inspector, zero clicks. (`textedit.rs:258-280, 943-1003`; `shared/mod.rs:859-915, 979-1015`; `Getting Started.md:206-217`)
- **[core] Counterfactual branching from the tooltip** — top_logprobs rendered as a button row (token + probability %); clicking an alternative splits the node around that token and creates a sibling branch starting with the chosen alternative, preserving attribution and the counterfactual list. The killer loom affordance; coloom already stores `top_logprobs` and has `split_node`, so the backend cost is one split-out-token op. (`textedit.rs:950-996`; `shared/mod.rs:935-977`; `shared/weave.rs:230-307`)
- **[core] Node boundary markers in flowing text** — thin theme-derived ticks at node boundaries in the text view, so structure is visible without relying on color alone. Cheap; matters more in coloom where boundaries are *attribution* boundaries. (`textedit.rs:824-927`)
- **[core] "No text" placeholder for empty nodes** — distinct font, inherits node color so attribution stays visible. Trivial. (`shared/mod.rs:1240-1354`)
- **[later] Confidence-based attenuation** — second opacity factor from a distribution-peakedness "confidence" metric (arXiv 2508.15260). coloom stores `entropy` per token instead — same UI slot, different metric; do it once the probability view exists. (`shared/mod.rs:1017-1047`; `Getting Started.md:219-229`)
- **[later] Passive per-row info display** — non-hovered rows show bookmark glyph + token probability % for single-token nodes; makes token-loom trees scannable. Comes with the list views. (`lists.rs:1142-1158`)
- **[later] Interactive hover tooltip with embedded action buttons** — canvas tooltips containing a live action toolbar + collapse toggle + metadata, debounced on pan/zoom idle. Web analog: a small floating toolbar on hover. v1's right-click menu covers the need. (`canvas.rs:780-810, 295-296`)
- **[skip] Invalid-UTF-8 byte preservation through the editable view** — substitution-character round-tripping of raw token bytes. coloom's `Tokens` content is JSON strings end-to-end; there is no raw-byte content type to protect. (`textedit.rs:49-50, 407-413, 445-459`)

## 4. Text editing (active thread)

- **[core] Linear text view of the active thread** — root→leaf concatenation rendered as one continuous monospace document, per-node colored, per-token opacity, hover-synced with the tree. v1 scope: read + append (type at the end → new human node); see next item for full editing. (`textedit.rs:104-353`)
- **[core] Caret/click → node mapping** — clicking or moving the caret in the text publishes which node (and byte offset within it) is selected, driving the tree views; selecting in the tree moves the text view. Keep Tapestry's node-vs-within-node granularity distinction. (`textedit.rs:313-346, 415-444, 929-941`)
- **[core] Auto-scroll text view to changed node (pointer-guarded)** — same live-update behavior as §2, applied to the document view; essential when the agent is generating while the human reads. (`textedit.rs:197-224`)
- **[later] Full whole-buffer edit diffing** — Tapestry lets you edit *anywhere* and diffs the buffer back into the weave (`set_active_content` decides which nodes change/split). Powerful but the hardest feature in the app; PLAN already earmarks v0's algorithm (~L360–450 of `v0.rs`) as the reference. Phase it after read+append. (`textedit.rs:354-414`; `shared/weave.rs:318-328`)
- **[later] Generate-at-caret with mid-node split** — caret mid-node → split then generate from the prefix; caret at node start → sibling alternatives from the parent. Depends on split-at-offset plumbing through the API. (`shared/mod.rs:174-190, 771-809`)
- **[later] Token-exact regeneration** — unmodified tokens with token_id + model_id sent back as token ids rather than re-encoded text. Backend/inference concern more than viewer; coloom already parses token ids. (`shared/mod.rs:821-857`; `Getting Started.md:219-229`)

## 5. Generation controls

- **[core] Inference parameter pane** — model/endpoint selection + sampling params (temperature, max_tokens, n, logprobs) for the next generation; non-persistent, resets to defaults per session. coloom's server-side YAML presets define the defaults. (`menus.rs:21-40`; `Getting Started.md:35-39, 176-180`)
- **[core] One-click parameter presets** — named presets as buttons atop the menu pane (Tapestry adds number-key shortcuts 1–10 + reset; keys can come later). coloom already has presets server-side — this is just the surfacing pattern. (`menus.rs:95-137`; `Getting Started.md:184`)
- **[core] Live request indicator with cancel** — spinner + "N requests" while generations are in flight; hover reveals cancel-all. In coloom this should show *whose* requests (human vs agent) — see §11. (`menus.rs:52-69`)
- **[later] Multi-model simultaneous generation** — several models per request, distinguished by node color in one tree. Natural fit for coloom's endpoint config, not v1. (`Getting Started.md:35-39, 176-180`)
- **[later] Recursive generation (depth parameter)** — grow a subtree in one action; exponential request count. Arguably more useful for the *agent* (which can loop the CLI) than the UI. (`Getting Started.md:182`)
- **[later] Single-token looming mode** — max_tokens=1 + logprobs>1 turns each alternative into its own child node; an emergent param combo, not a mode. Cheap once generation controls exist. (`Getting Started.md:186`)
- **[later] Node deduplication** — identical sibling completions merged. Matters when generating many samples; verify semantics in code first (only obliquely documented). (`Getting Started.md:186`)
- **[later] Sibling auto-sorting on arrival (None / Model / Confidence / Seriation)** — including seriation, an embedding-powered semantic ordering of children (async, needs an embedding endpoint). (`shared/mod.rs:527-614, 658-770`; `README.md:105-131`)
- **[skip] Counterfactual storage opt-out** — per-document toggle stripping top_logprobs to save file size. In coloom this is server-side storage policy (SQLite, partial updates), not a viewer feature; revisit only if weave size becomes a problem. (`shared/mod.rs:514-520`)

## 6. Organization: bookmarks, sorting, search

- **[core] Bookmark toggle + bookmark display** — hover/menu toggle, glyph in tree rows, selection-colored stroke on canvas cards. Backend already has bookmarks. (`Getting Started.md:89-98`; `canvas.rs:563-567`)
- **[core] Bookmarks list pane** — every bookmarked node, click to jump/activate, single remove button. Cheap, and doubles as a human↔agent handoff surface ("look at these"). (`lists.rs:167-299`)
- **[later] Manual child sorting via context menu** — sort by confidence (token logprobs), by timestamp (ULID), seriate. (`lists.rs:1179-1351`; `shared/mod.rs:712-770`)
- **[later] In-weave node search** — notable *absence* in Tapestry (explicit TODO, `mod.rs:23`; roadmap Milestone 4, `README.md:230`). coloom can leapfrog here; trivial against a server API.
- **[later] Info pane: weave notes + metadata editor** — free-form notes field + key/value metadata table, persisted in the weave. coloom's weave metadata supports it. (`menus.rs:141-206`)
- **[later] Status-bar weave statistics** — "{N} nodes, {M} active, {K} bookmarked". Nice, cheap, do it when there's a status bar. (`menus.rs:54-93`)

## 7. Structure editing, context menus, keyboard

- **[core] Node context menu (right-click)** — v1 set: generate, bookmark, create child/sibling, delete, set active. Tapestry's full menu adds collapse/expand-all, seriate/sort, delete-all-children/siblings, merge-with-parent — fold those in as the backing ops land. (`lists.rs:1179-1351`; `canvas.rs:767-769`)
- **[later] Hover-revealed action button strips on rows** — delete / bookmark / add / generate / merge appearing in-place on row hover; the dominant interaction grammar of the app. Comes with the list views; note hover-to-reveal has no touch story (Tapestry admits this, `README.md:342, 354`) — pick a deliberate touch/keyboard fallback. (`lists.rs:683-861`; `Getting Started.md:57-87`)
- **[later] Keyboard suite for tree surgery + navigation** — bookmark, add child/sibling, delete variants, merge, split-at-caret, parent/child/sibling navigation (which also re-routes the active path), activate-hovered as a hold modifier; all handled centrally so they work from any pane. A minimal subset (sibling/parent arrows, generate) is worth pulling forward early. (`shared/mod.rs:174-462`)
- **[later] Rebindable shortcuts with priority rules** — settings UI, Escape clears, macOS Ctrl/Cmd equivalence. (`Getting Started.md:188-196`)
- **[later] Shift-/middle-click "do it and focus it" modifiers** — every generate/create action has a move-my-position and a stay-put variant; tooltips live-switch while a modifier is held. Small but consistent power-user affordance; web analog ctrl/cmd-click. (`lists.rs:683-861`; `Getting Started.md:198-204`)
- **[later] Merge-with-parent / split-node UI** — with an `is_mergeable` check gating menu enablement; split-generated ids preserve creation-time ordering. Backend has `split_node`; merge needs API support. (`shared/weave.rs:19-343`)

## 8. Session, file & app management

- **[core] Async load states + graceful failure** — loading spinner while the weave fetches; distinct error surfaces per failure class; never block the UI. Web translation of `mod.rs:232-324`.
- **[core] Error toasts** — all async failures (inference, server) surface as toasts with the error text, never swallowed. Matches the "don't hide failures" house rule. (`mod.rs:62, 240-244`; `shared/mod.rs:583-587`)
- **[later] Example weaves for inference-free onboarding** — ship a sample weave so the UI can be explored without an endpoint configured. (`Getting Started.md:7, 77`)
- **[later] Model/endpoint management UI with templates** — settings pane with endpoint-type templates, per-model label + color. coloom's endpoints live in server YAML; an in-UI editor is not v1. (`Getting Started.md:9-19`)
- **[later] Import from other loom formats** — Tapestry's migration-assistant converts loom/loomsidian/exoloom/etc. Maps to PLAN milestone 7 "JSON import of others' looms". (`README.md:77-79, 263-270`)
- **[later] In-app breaking-change dialogs** — surface schema/format changes in the client, not just release notes; the only shipped roadmap item. Good pattern for coloom schema migrations. (`README.md:171`)
- **[skip] Autosave machinery (background-thread saves, barriers, file locking, save-suppression during generation)** — obsoleted by coloom's server-canonical, transaction-per-mutation SQLite store. Only the UX-visible parts (spinner, toasts) carry over. (`mod.rs:432-533`)
- **[skip] Save As modal / temporary-vs-file-backed editors** — no client-side files in coloom; weaves are created on the server. (`mod.rs:385-430`; `Getting Started.md:100-108`)
- **[skip] Close-without-saving confirmation** — nothing is ever unsaved in coloom. (`mod.rs:196-230`)
- **[skip] Files view (in-app file manager)** — browse/create/move/delete .tapestry files. Replace with a server-backed weave picker (list/create/open). (`Getting Started.md:110-126`)
- **[skip] File path status bar + window-title-from-filename** — pathless in coloom; show weave title (and set `document.title`) instead. (`mod.rs:343-379, 325-341`)

## 9. Cross-cutting design artifacts

- **[core] The shared-state taxonomy** — Tapestry's explicit three-tier model: shared+persistent (in the weave: nodes, active path, bookmarks, metadata), shared+temporary (editor session: hovered node, cursor node, collapse set, current params, last-changed node), local+temporary (per-pane: scroll, zoom). This quartet of shared signals (cursor / active thread / hovered / changed) is what makes six different views feel like one editor, and it maps directly onto a Svelte store fed by coloom's WS feed. Adopt as the frontend's architecture, with one twist: in coloom "shared+temporary" splits again into *per-client* vs *broadcast-to-other-participants*. (`Getting Started.md:132-154`; `shared/mod.rs:51-76`)
- **[skip] egui change-flag / minimal-repaint mechanics** — `has_*_changed` dirty flags, `request_discard`, frame-level repaint gating. Svelte reactivity + derived stores give the equivalent; the portable lesson (key expensive geometry/layout caches on weave-version + theme + container size, separate re-layout from recolor) is folded into §2's layout/culling items. (`shared/mod.rs:464-505, 616-628`)

---

## Planned but unbuilt in Tapestry-Loom (leapfrog candidates)

Everything here is roadmap-only in Tapestry — coloom can ship these first:

- **Active-counterfactual highlighting** — at each position of chosen text, show the alternatives with the actually-chosen token highlighted among them (`README.md:175-176`). Pairs directly with coloom's stored `top_logprobs`.
- **Exoloom-style hovered-child preview** — hover a child to preview its text inline in the document view without activating it (`README.md:189`). High value: branch-peeking without committing — and without yanking the *shared* active path.
- **Autoloom click-to-generate** — activating a node immediately generates children (`README.md:190`).
- **Node search** (`README.md:230`) and **weave statistics** (per-model contributions, probability/branching distributions, `README.md:236`) — stats by creator is especially natural for coloom's attribution model.
- **Client-side post-processing of generations** — `TL#keep_top_p` / `keep_top_k` / `prune_empty` single-token pruning, confidence/avg-probability node pruning (`README.md:208-223`, *excluding* the adaptive-looming portion, which is ruled out — see below).
- **Path bookmarks, prefix-based dedup, child-select-by-number shortcuts, copy-node-to-clipboard, undo/redo via stored edit history** (`README.md:197-207, 253, 339, 358`).
- **v2 DAG weaves** — split/merge-instead-of-mutate editing, arbitrary node connections (`README.md:241-257`). coloom's edges-in-their-own-table store is already DAG-ready; this milestone is largely coloom's head start.
- **And the big one:** Tapestry's most speculative wishlist — *collaborative weave editing, AI-agent interfaces, multi-user server-client WebUI, event-based sync* (`README.md:360-399`) — is precisely coloom's founding premise. The commented-out block at `README.md:375-399` (auth/permissions/rate limiting, plugin/custom-subview API, autolooms) reads as a free requirements brainstorm for coloom's future.

## Out of scope (decided 2026-06-09 — do not re-propose)

Reviewed and **ruled out** per `docs/PLAN.md` "Out of scope": **FIM** (fill-in-the-middle insertions), **blind comparison modes**, and **adaptive looming** (uncertainty-based node-length cutting). They appear in Tapestry's roadmap (`README.md:233-235, 218-223, 241-257`) and are listed here only so future sessions don't rediscover them.

## coloom-specific features with no Tapestry-Loom equivalent

The audit above is a parity baseline; coloom's reason to exist is none of it. Don't let the inventory anchor the UI:

- **Live multi-client sync** — every mutation arrives over WS; remote changes animate in (the "changed node" signal of §2 fired by *someone else*). Tapestry has no second client at all.
- **Presence & awareness** — whose cursor/hover/in-flight generation is whose. Tapestry's shared hover state becomes a *broadcast* presence channel: show the agent's focus to the human and vice versa. The pointer-suppression rule (§2) is the seed of the contention policy.
- **Human-vs-agent attribution as the default lens** — Tapestry's per-model coloring becomes first-person: *you* vs *the agent(s)*, always on, in every view (cards, text, minimap).
- **Agent activity feed** — the backend's append-only `events` table rendered as a timeline ("agent generated 4 branches under X", "human re-routed the active path"), giving the human a narrative of what the agent did while they weren't looking.
- **Shared-active-path policy** — Tapestry never had to decide whether navigation re-routes a path someone *else* is reading. coloom does (flagged in §2); per-client paths vs a negotiated shared path is a genuine open design question, not something this audit can settle.

## Summary

| Category | core | later | skip |
|---|---|---|---|
| 1. Workspace shell | 0 | 1 | 1 |
| 2. Tree & graph navigation | 9 | 6 | 0 |
| 3. Node display & token/logprob rendering | 6 | 3 | 1 |
| 4. Text editing (active thread) | 3 | 3 | 0 |
| 5. Generation controls | 3 | 5 | 1 |
| 6. Organization (bookmarks/sorting/search) | 2 | 4 | 0 |
| 7. Structure editing & keyboard | 1 | 5 | 0 |
| 8. Session, file & app management | 2 | 4 | 5 |
| 9. Cross-cutting design artifacts | 1 | 0 | 1 |
| **Total** | **27** | **31** | **9** |
