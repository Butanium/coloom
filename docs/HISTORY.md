# coloom build history

Round-by-round timelapse of how coloom got built (2026-06-09 → …). Moved out of
CLAUDE.md 2026-06-10 — CLAUDE.md is orientation; this file is the archaeology
("why is X built this way" usually has its answer in the round that built X).

**Status: full web UI shipped + interaction-tested** (2026-06-10): the audit's core-27 (plus
several "later" items: tree-list windowing, flat list, node search, graph minimap, keyboard
suite, activity feed) is implemented in `web/` — picker → editor with resizable
tree-sidebar / canvas|graph / thread-text panes, rich token tooltips with **counterfactual
branching** (click a top-logprob alternative → split + sibling branch), right-click context
menu incl. the "summon <cursor> here" gesture, per-node gen-config inspection, presence
("X is weaving…") from `gen_started/finished` events, live multi-client WS sync. Built by a
spec-extraction workflow (`docs/ui-specs/*.md`, exact behaviors from Tapestry-Loom source) +
6 parallel component agents + 7 adversarial playwright testers. **Tests: 124 fast
(`uv run pytest`) + the browser-interaction suite (`uv run pytest tests/ui`, opt-in,
self-contained: the conftest builds web/ and launches its own ephemeral
fake-openai + coloom-server on free ports — no dev stack or restart coordination needed;
escape hatch: export COLOOM_UI_BASE/COLOOM_API to target the dev stack instead).**
Automated tests use the **gpt-fake** mock (`src/coloom/fake_openai.py` — random tokens/logprobs,
free); real gpt4-base stays for fun manual smokes. Seed a dev weave:
`uv run scripts/seed_dev_weave.py`. Design rationale in [`docs/PLAN.md`](docs/PLAN.md);
feature baseline in [`docs/tapestry-viewer-audit.md`](docs/tapestry-viewer-audit.md).

**Adjustments round (2026-06-10, post-feedback):** (1) navigation made instant — cursor moves
are optimistic + `cursor_moved`/bookmark events patch the local weave instead of a full refetch
(`state.svelte.ts`); (2) type scale: `--fs-doc/ui/small/tiny` vars in `app.css`, 12px hard floor;
(3) readable colors: text fills use `creatorTextColor()` (hue mixed into light text), saturated
`creatorColor()` is accent-only; (4) **free-form thread editing** — the text pane is a
contenteditable buffer diffed against the cursor thread (`web/src/lib/editbuffer.ts`, spec
`docs/ui-specs/textedit.md` §6 + suffix preservation): appends coalesce into one growing human
node (via `PATCH /nodes/{id}`), mid-thread edits split + branch into **hybrid nodes** (creator
preserved, `metadata.edited_by`) whose preserved tail tokens keep logprobs flagged
`Token.inexact` (dotted underline, tooltip "logprob from pre-edit context"); nothing is ever
destroyed — original branches survive as siblings; (5) **two-layer inference setups**
(`docs/setups-api.md`, `src/coloom/setups.py`): model setups (endpoint + arbitrary API flags)
+ sampler setups (model ref + overrides), CRUD UI in `SetupsDrawer.svelte` (née SetupsDialog,
now a bottom drawer — round 4), **several samplers
active at once** → generate fans out one completion per active sampler (merge: model.params ←
sampler.params ← request.params; api keys redacted everywhere).
**Round 3 (2026-06-10, team coloom-ui: lead + teammates keys/textpane):** (1) fonts round 2
(thread ~19px, ui 16px, NodeCard 15px — calibrated against Tapestry side-by-side); (2) unified
**generators** model: sampler setups AND presets are toggleable multi-active chips (digit keys
toggle k-th chip), "generators ▾" menu (activate/hide/edit/dup/delete, "→ setup" clones a
preset into an editable model setup), `ParamsEditor.svelte` key/value rows replace all JSON
textareas; (3) **profiles**: app opens on a login gate (`Login.svelte`), all per-person client
state (ui prefs, generators, keybindings, picker collapse) lives in server-side profile
settings (`profile.svelte.ts`, PUT /profiles/{name}, debounced) and roams across browsers;
identity == profile name; `coloom profile login` gives the CLI the same attribution; (4)
**keybindings**: every shortcut is a rebindable action (`keybindings.svelte.ts`, capture-UX
dialog à la Tapestry, Escape-while-capturing unbinds, conflicts highlighted), stored per
profile; (5) free-form editor polish: legacy append box removed — typing in a blank weave
creates the root directly; fixed a Chromium text-canonicalization double-render; (6) picker
**folders** (weave.metadata.folder path strings → collapsible groups, move/filter/create-into).
Known deferred: in-editor undo (ctrl+z doesn't revert applied edits), merge-with-parent,
sibling sorting, virtualization, occasional emoji-edit test flake (silent no-op ~1/5),
typing again within the 600ms debounce of an unapplied edit can double-append (rare).
**Round 4 (2026-06-10 night, QoL batch, team coloom-ui):** (1) **sticky child navigation** —
arrow-right returns to the child you last visited, not children[0] (per-window map in
`keyboard.svelte.ts`); (2) **fast-nav flicker fixed** — late `cursor_moved` echoes of my own
moves are absorbed while moves are in flight (`myPendingCursorEchoes` in `state.svelte.ts`;
smoke: `scripts/small-smokes/smoke_no_cursor_flicker.py`); (3) **Alt+Arrow** = hardwired nav
aliases that work even while typing in the doc (plain arrows stay caret moves); (4) setups
editing moved from modal to a collapsible **bottom drawer** (`SetupsDrawer.svelte`, open state
roams per profile) — top generators chips row + quick temp/max_tokens/n unchanged; (5) **canvas
multi-select** — shift+drag rubber-band (never pans) + shift+click toggle (`selection.svelte.ts`,
per-client), selection ring + floating bulk-action bar (`SelectionBar.svelte`: bookmark / collapse
/ delete-with-cascade-confirm / clear), Escape clears; (6) activity tab hides plain cursor-move
chatter — a move shows only when a real event follows it (summons always show); (7) test
identities renamed `uitest-*`, dev/test weaves live in the picker's `testing` folder (seed
script targets it); "keys" button renamed "keybindings"; (8) generate defaults are
**Ctrl+Enter / Ctrl+Shift+Enter** (gen / gen+follow), and bound combos with a non-shift
modifier dispatch even from editable targets; (9) **profile deletion is soft** — DELETE marks
`active=0` (hidden from the gate), logging in with the same name resurrects all settings
(motivated by a real same-night loss: gate-deleting a duplicate profile ate its config);
(10) **event origin mechanism** — every mutating request carries a per-tab `X-Coloom-Client`
id, stamped into event `payload.origin`, so clients absorb their *own* echoes generically
(replaces the ad-hoc pending-echo counter; origin-skipping applies only to optimistically
applied features, currently cursors); (11) **free-form edit serialization** — the
double-append bug was a stale diff baseline (edit 2 diffed against a thread not yet containing
edit 1): `applyEdit` now waits for the live thread to provably contain the previous edit
before re-diffing, doc render stays frozen across the whole sync window, with a prefix-match +
4s timeout escape for concurrent remote edits.
**Round 5 (2026-06-10 evening, team coloom-gen: lead + backend/frontend/frontend2):** the
**templates + per-profile generators redesign** (`docs/generators-api.md`, supersedes
setups-api): one noun — a **template** (server-global shelf definition; builtin = imported
from yaml, read-only) and a **generator** (per-profile, chip-able, optionally inheriting
template→generator chains with per-field overrides; nobody can edit YOUR sampling strategy).
Chips: body-click = **focus** (quick temp/max_tokens/n row + **drag-to-adjust**, à la egui
DragValue, edit the focused generator, persisted via debounced PATCH; placeholders = inherited
values, emptying clears to inherit), leading **dot = active** toggle (fan-out unchanged);
stale-ancestor badge when someone else edits a template you inherit (origin-absorbed, cleared
on focus). Drawer: single edit form, create from scratch/template/generator × inherit/duplicate,
promote-to-template, **endpoint probe** (`POST /probe-endpoint`, by-id mode for stored "***"
keys; reachability indicator + `/models` datalist autocomplete). Server: mutation events with
`by` (percent-encoded `X-Coloom-Profile`) ride a global scope (`weave_id:""`,
`docs/events-api.md`); per-profile seeding from builtin templates (never-resurrect). Also:
(1) **node soft-delete + Ctrl+Z undo** — DELETE marks the subtree (`nodes.deleted` = op-root
id), `POST .../restore` un-deletes by op with ancestor-chaining, undo stack
(`web/src/lib/undo.svelte.ts`, rebindable, LIFO, re-parks cursors via `moved_cursors.from`),
toast with undo button; **node deletes have NO confirmation**; (2) **zero native popups** —
in-app `askConfirm` dialog + `ArmedButton` inline two-step for weave/profile deletes;
(3) **activity feed**: click-to-expand entries, no horizontal scroll, **this-weave/all-weaves
toggle** (refcounted unfiltered `/ws` in `globalevents.svelte.ts`), neutral info-toast variant;
(4) **refetch race fixed** — a weave snapshot can't clobber events patched while it was in
flight (latest-fetch-wins + in-flight patch replay in `state.svelte.ts`; third member of the
optimistic-update bug family); (5) **two-stack split**: Clément's **live instance :5555**
(systemd user service `coloom-live`, durable `~/coloom-data/coloom.sqlite`, profile `clement`,
2 templates, daily 04:17 backup cron → `~/coloom-backups/`, serves `web/dist` via
`--static-dir`) vs the throwaway dev playground :4444/:5174; (6) **hermetic UI tests** —
conftest builds web/ + launches an ephemeral fake-openai + server on free ports per session
(proof: 166/166), dev stack no longer involved; (7) `.env` now actually loaded by the server
(`--env-file`); activating a hidden generator un-hides it. Known deferred: in-editor TEXT undo
(structural undo shipped), neutral-toast adoption beyond undo notices, `removed` legacy alias
drop, the round-3/4 deferreds.
**Round 5.1 (2026-06-11 night, teammate fixer):** the blank-pane bug + boundary-flush editing.
(1) **blank-doc-pane bug root-caused & fixed** — paste multiline, let it apply, type mid-text →
the text pane went permanently blank while server + local state stayed correct. Cause: the
post-edit recovery used to surgically "sweep" untracked stray DOM out of the contenteditable,
but Svelte 5 uses empty TEXT nodes (not just comments) as block anchors and Chromium freely
types into/around them — the sweep removed the each-block's `#text ""` anchor, after which
every Svelte insertion targeted a detached node and silently vanished. Fix: `{#key docEpoch}`
now wraps the WHOLE `.doc` element — recovery REPLACES the element (fresh anchors, strays die
with the old one); never mutate inside framework-managed DOM. NOT a leg of the optimistic-state
contract — a fourth rule (view-vs-framework, not client-vs-server). Evidence:
`scratch/repro_paste_then_type.py` (+ `debug_vanish_*.py`); regression test
`tests/ui/test_paste_then_edit.py`, verified failing on the pre-fix code.
(2) **boundary-flush editing** (Clément's call, option "pure boundary"): free-form doc edits
stay LOCAL until a boundary — doc blur, generate, any cursor move/node op/undo, weave switch,
tab hide (best-effort beforeunload) — no auto-apply timer at all. Every weave-mutating action
in `state.svelte.ts` awaits `flushPendingEdits()` first (TextPane registers the flusher; the
plan executor uses `moveMyCursorNoFlush` to avoid re-entrancy), so "send the node update, then
run the next one" is literal. Why: the 600ms debounce turned one mid-thread edit session into
a CASCADE of splits + permanent intermediate sibling branches (nothing-destroyed = debounce
trails pollute the tree forever); pure boundary = one deliberate edit session → one split +
one node. Keyboard nav/generate recompute their target AFTER the flush (a flush can move or
even create your cursor). Tradeoffs (accepted, documented): co-weavers no longer see
in-progress text until a boundary (a lightweight "X is editing…" presence ping is DEFERRED),
and a browser/OS crash loses an un-flushed session (acceptable; the fix if it bites is
localStorage draft persistence, NOT a timer). The editing-bar now says
"local edits — sync on generate / nav / blur".
(3) **leg-3 interaction authority** (`web/src/lib/authority.ts`, per
`docs/optimistic-state.md`): reusable per-value guard — gesture depth counting,
deferred server writes applied only when a gesture ends WITHOUT a local commit, echo
confirmation, failure relinquish. Applied to the quick row (incident 4: values jumped
backwards under the pointer); `dragnum` gained dragStart/dragEnd and **commit-on-release**
(local visual updates per frame, ONE PATCH on release). Test: `tests/ui/test_quick_row.py`
(route-delayed PATCH landing mid-drag, monotonicity asserted).
(4) **canvas QoL**: center-on-demand — selection/navigation pans only when the target is
(nearly) outside the viewport (`nodeVisible` gate; fit/zoom commands still always act);
shift+scroll pans horizontally; **gen placeholder skeletons** — `gen_started` now carries `n`,
`weaveWithPlaceholders()` injects one phantom child per expected completion (dashed pulsing
cards/rows, not click targets), resolved by the real nodes on `gen_finished` (errors already
toast); gpt-fake honors a per-request `delay` param for deterministic in-flight windows.
(5) **Delete key deletes the whole multi-selection** via the SelectionBar bulk path
(`deleteSelection()` in `selection.svelte.ts` — one undo batch, no confirm, selection cleared;
editable targets keep text deletion). (6) **incident 5 fixed** (Clément hit it live as "cursor
lands on the non-deleted branch after an end-of-thread edit-delete + pane stuck until reload"):
a weave snapshot fetched BEFORE an optimistic cursor POST but assigned AFTER it clawed the
local cursor back to the pre-edit leaf — leg 2's patch replay can't restore it because the
own-origin echo is absorbed by leg 1 even on replay. Fix: `pendingCursorMoves` ledger in
`state.svelte.ts` — optimistic cursor moves are re-asserted over every assigned snapshot until
a snapshot whose fetch STARTED after the POST settled retires them (authoritative either way:
it carries the move, or something legitimately newer like a summon). Repro evidence:
`scratch/repro_end_delete_nav.py`; regression:
`test_editing.py::test_end_truncation_cursor_lands_on_head_and_nav_keeps_pane_live`.
(7) **whitespace-only tokens are hoverable** — `\n`/space tokens render ~zero width and had no
hover target for the logprob tooltip; an absolutely-positioned halo (`.token.ws::after`,
inset -2/-3px) extends the hit area without shifting layout, tinted on hover as feedback.
(8) **merge-with-parent UI** (retrier built the op): context-menu entry (disabled on
roots) + rebindable Ctrl+M; undo follows the binding rules — restore the absorbed originals +
re-park cursors, delete the merged copy ONLY when it took no children (else it must stay or
its deletion would cascade onto the migrated children); driven entirely off the response
(`deleted_node_ids` / `merged_node.children`), never recomputed client-side. Tests:
`tests/ui/test_merge.py` (sibling + in-place-leaf + root-disabled).
(9) **edit-at-ancestor consolidation** (task #6): free-form-editing the span of a NON-LEAF
thread node (cursor at a descendant) now produces ONE new sibling of the edited node holding
the full consolidated edited A..leaf text — the original branch is completely untouched, not
even split (the old behavior split the ancestor and copied the downstream chain node-by-node).
Leaf-only edits keep the split/hybrid path (token granularity preserved there); consolidation
deliberately degrades to a snippet. Cursor moves to the new node. `planMidEdit` in
`editbuffer.ts`; tests re-targeted accordingly (the emoji code-point test is leaf-only now).
Known deferred: "X is editing…" presence ping for held-local edits; localStorage draft
persistence; round-5 deferreds.
