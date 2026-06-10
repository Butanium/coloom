# coloom

Real-time **human + AI agent collaborative looming** over LLM base models: a shared, branching
tree of completions (with per-token logprobs) that a human and an agent weave *together*, live.

**Status: full web UI shipped + interaction-tested** (2026-06-10): the audit's core-27 (plus
several "later" items: tree-list windowing, flat list, node search, graph minimap, keyboard
suite, activity feed) is implemented in `web/` — picker → editor with resizable
tree-sidebar / canvas|graph / thread-text panes, rich token tooltips with **counterfactual
branching** (click a top-logprob alternative → split + sibling branch), right-click context
menu incl. the "summon <cursor> here" gesture, per-node gen-config inspection, presence
("X is weaving…") from `gen_started/finished` events, live multi-client WS sync. Built by a
spec-extraction workflow (`docs/ui-specs/*.md`, exact behaviors from Tapestry-Loom source) +
6 parallel component agents + 7 adversarial playwright testers. **Tests: 81 fast
(`uv run pytest`) + 140 browser-interaction (`uv run pytest tests/ui`, opt-in, needs the dev
stack: `uv run coloom-fake-openai` + `uv run coloom-server` + `cd web && npm run dev`).**
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

## Layout
- `src/coloom/models.py` — pydantic weave model (Node, Snippet/Tokens, Creator attribution, Cursor)
- `src/coloom/store.py` — SQLite weave store, **directly canonical** (every mutation a transaction;
  deliberate deviation from PLAN's in-memory+debounce idea). Edges in their own table → DAG-ready.
  Also the append-only `events` table (the WS/polling change feed).
- `src/coloom/inference.py` + `config.py` — httpx `/v1/completions` client with the
  polyparser-derived edge-case handling; YAML endpoints/presets (`coloom.example.yaml`)
- `src/coloom/setups.py` — two-layer inference setups (ModelSetup/SamplerSetup, param merge,
  `resolve_sampler()`); stored server-globally in SQLite, REST CRUD under `/setups`
- `src/coloom/server/` — FastAPI REST + `/ws` EventHub; `uv run coloom-server`
- `src/coloom/cli.py` — agent-facing CLI (`uv run coloom …`), JSON stdout / logs stderr
- `src/coloom/fake_openai.py` — **gpt-fake** mock completions server (`uv run coloom-fake-openai`,
  :9999): random text + logprobs, text-derived finish_reason, `--delay` for in-flight UI states
- `scripts/small-smokes/` — live smokes (`smoke_generate.py`, `smoke_coweave_e2e.py`,
  `screenshot_ui.py`, `smoke_click_hover_buttons.py`, `smoke_ws_live_ui.py`); re-run when the
  pipeline changes
- `scripts/seed_dev_weave.py` — seed a realistic dev weave (multi-depth, bookmarks, 2 cursors)
- `web/` — the full SPA (Vite + Svelte 5 runes + TS); `src/lib/state.svelte.ts` is the shared-state
  contract every component builds on; `src/lib/profile.svelte.ts` is the roaming-settings hub
  (stores register `onProfileLogin` appliers + persist via `setSetting`); `keybindings.svelte.ts`
  the rebindable-shortcut table; dev proxy to :4444 (`COLOOM_SERVER` overridable);
  `npm run dev|build|check` from `web/`
- `tests/ui/` — 90 playwright interaction tests (opt-in; per-test seeded weaves; fixtures in its
  `conftest.py`)
- `docs/tapestry-viewer-audit.md` — full Tapestry-Loom viewer feature audit (core/later/skip),
  the conscious feature baseline for the web UI
- `docs/ui-specs/` — exact behavioral specs extracted from Tapestry-Loom source (canvas, textedit,
  lists, shell/menus/graph, shared-state); the reference for UI behavior questions
- `tests/fixtures/gpt4base_completion.json` — captured real response driving parser + server tests
  (fun fact: gpt4-base self-reports as `gpt-4-0314`)

## Stack
- **Backend** (`core` + `server`): Python, `uv`, FastAPI (REST) + WebSocket for change events.
  The server owns the canonical weave and pushes change events so every client stays in sync.
- **Persistence**: SQLite (nodes/edges/metadata; partial updates). JSON for the API + export.
- **Inference**: `httpx` against OpenAI-compatible `/v1/completions` (+ chat) with logprobs.
  Dev/test endpoint: OpenAI API with `gpt4-base` (key in repo-local `.env`); llama.cpp
  `llama-server` and vLLM also first-class targets.
- **CLI** (`coloom`): Python, agent-facing — JSON in/out, non-interactive, an HTTP client to the server.
- **Web frontend**: TypeScript SPA — **Vite + Svelte, in-repo under `web/`** (no SvelteKit) — on
  the server's REST + WS; FastAPI serves the built `web/dist/`.
- No Rust, no `.tapestry` interop. Our own weave format, *inspired by* Tapestry Loom's v1 design.

## Weave model (sketch)
Nodes with stable ids, parent/child links (DAG-capable for merges), roots, bookmarks, and **named
cursors** instead of a single active path: each participant keeps a cursor (its thread derived
root→node), and anyone may move anyone's cursor — the "look here" gesture (`moved_by` records who).
Node content is either a text `Snippet` or typed `Tokens` (`{text, logprob, token_id?, entropy?,
top_logprobs[]}`). Each node carries a `Creator` — `Human` or `Model` (with seed + raw request/response)
— for human-vs-agent attribution. See `docs/PLAN.md` for the full schema.

## Conventions
- Global research-code conventions in `~/.claude/CLAUDE.md` apply (don't hide failures, real runs over
  mocks, `uv` for Python, argparse/CLI over globals, etc.).
- `docs/PLAN.md` also lists **places in Tapestry-Loom worth a look** for design inspiration —
  framed loosely; explore freely rather than treating them as a spec. The reference checkout is at
  `~/projects2/weird-personas/Tapestry-Loom` (on the `new-format` branch, which has the richer v1 model).

## Lineage
In the tradition of loom (socketteer), loomsidian, exoloom, mikupad, wool, logitloom — and especially
[Tapestry Loom](https://github.com/transkatgirl/Tapestry-Loom), whose format design we draw on.
coloom's angle: make the loom a place where a human and an agent weave *at the same time*.
