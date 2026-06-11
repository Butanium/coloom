# coloom

Real-time **human + AI agent collaborative looming** over LLM base models: a shared, branching
tree of completions (with per-token logprobs) that a human and an agent weave *together*, live.
In the tradition of loom (socketteer), loomsidian, exoloom, mikupad, wool, logitloom — and
especially [Tapestry Loom](https://github.com/transkatgirl/Tapestry-Loom), whose format design
we draw on. coloom's angle: human and agent weave *at the same time*.

**State (2026-06-10): fully working app.** Backend (FastAPI + SQLite + WS change feed), web SPA
(Vite + Svelte 5), agent CLI. The UI covers the full Tapestry-Loom-derived feature baseline plus:
multi-cursor presence, counterfactual branching from token logprobs, free-form thread editing
(splits/hybrid nodes, nothing destroyed), templates + per-profile generators with inheritance,
node soft-delete with Ctrl+Z undo, per-profile roaming settings + rebindable keybindings,
activity feed (per-weave + global). Build chronology: [`docs/HISTORY.md`](docs/HISTORY.md).

## Running instances (two stacks — don't mix them up)

- **LIVE** (durable, Clément's real weaves): systemd user service `coloom-live` on **:5555**,
  db `~/coloom-data/coloom.sqlite`, config `~/coloom-data/coloom.yaml` (2 builtin templates:
  `gpt4-base` + `gpt-fake`), profile `clement` (no accent). Serves built `web/dist/` — after a
  UI change lands: `cd web && npm run build && systemctl --user restart coloom-live`.
  Daily backups: cron 04:17 → `scripts/backup_coloom_live.sh` → `~/coloom-backups/` (keep 14).
  Logs: `journalctl --user -u coloom-live`. **NEVER point tests at :5555.**
- **DEV playground** (debris db, interactive dev/smokes): `coloom-server` on **:4444**
  (`--db /tmp/coloom-ui-smoke.sqlite`, repo `coloom.yaml`), vite on :5174 proxying to it
  (:5173 belongs to another project — never touch). Tests don't need this stack (see below).

## Run / test

- `uv run pytest` — fast suite (server, store, generators, CLI; UI tests auto-skip).
- `uv run pytest tests/ui` — playwright interaction suite, **hermetic**: conftest builds
  `web/` and spawns an ephemeral fake-openai + coloom-server per session (free ports, temp db).
  `COLOOM_UI_BASE`/`COLOOM_API` re-target the dev stack for fast iteration;
  `COLOOM_TEST_NO_BUILD=1` reuses the existing dist. ~16 min full.
- `npx svelte-check --threshold error` (from `web/`) must end 0 errors.
- Automated tests use the **gpt-fake** mock; real gpt4-base (key in repo-local `.env`,
  loaded by the server via `--env-file`) is for manual smokes.
- Seed a dev weave: `uv run scripts/seed_dev_weave.py`.

## Layout

- `src/coloom/models.py` — pydantic weave model (Node, Snippet/Tokens, Creator, Cursor)
- `src/coloom/store.py` — SQLite store, directly canonical (every mutation a transaction);
  edges in their own table (DAG-ready); append-only `events` table (the change feed);
  node soft-delete via deletion-op markers
- `src/coloom/generators.py` — templates + per-profile generators (inheritance chains,
  `resolve_chain()`); contract: [`docs/generators-api.md`](docs/generators-api.md)
- `src/coloom/inference.py` + `config.py` — httpx `/v1/completions` client + YAML config
- `src/coloom/server/` — FastAPI REST + `/ws` EventHub; `uv run coloom-server`
  (`--db`, `--config`, `--env-file`, `--static-dir`)
- `src/coloom/cli.py` — agent-facing CLI (`uv run coloom …`), JSON stdout / logs stderr
- `src/coloom/fake_openai.py` — gpt-fake mock (`uv run coloom-fake-openai`, `--delay`)
- `web/` — the SPA. `src/lib/state.svelte.ts` = shared-state contract everything builds on;
  `profile.svelte.ts` = roaming-settings hub; `keybindings.svelte.ts` = rebindable shortcuts;
  `editbuffer.ts` = free-form edit diffing (edits are LOCAL-until-boundary: they flush on
  blur/generate/nav/node-ops, never on a timer); `authority.ts` = leg-3 interaction-authority
  guard for optimistic widgets
- `tests/ui/` — hermetic playwright suite (see Run / test)
- `scripts/small-smokes/` — live smokes, re-run when the pipeline changes
- `src/coloom/deprecated/` — retired code with per-file deprecation notes

## Key docs

| Doc | What |
|---|---|
| [`docs/PLAN.md`](docs/PLAN.md) | original design rationale + full weave schema |
| [`docs/generators-api.md`](docs/generators-api.md) | templates/generators contract (binding) |
| [`docs/events-api.md`](docs/events-api.md) | change feed, global scope, soft-delete/restore |
| [`docs/optimistic-state.md`](docs/optimistic-state.md) | **read before building any mutating UI surface** — the three-leg optimistic-state contract |
| [`docs/ui-specs/`](docs/ui-specs/) | exact behaviors extracted from Tapestry-Loom source |
| [`docs/tapestry-viewer-audit.md`](docs/tapestry-viewer-audit.md) | the conscious feature baseline |
| [`docs/HISTORY.md`](docs/HISTORY.md) | round-by-round build chronology |

## Weave model (sketch)

Nodes with stable ids, parent/child links (DAG-capable), roots, bookmarks, and **named cursors**
instead of a single active path: each participant keeps a cursor (thread = root→cursor), and
anyone may move anyone's cursor — the "look here" gesture (`moved_by` records who). Content is
a text `Snippet` or typed `Tokens` (`{text, logprob, token_id?, entropy?, top_logprobs[]}`).
Every node carries a `Creator` (Human | Model with raw request/response) for attribution.
Deletion is soft (restorable); nothing is ever destroyed by editing.

## Conventions

- Global conventions in `~/.claude/CLAUDE.md` apply (don't hide failures, `uv`, etc.).
- UI: no emoji glyphs (no emoji font on this box — tofu); inline SVG or text. No native
  `confirm()`/`alert()` — `askConfirm` dialog or `ArmedButton` two-step. The UI never asks the
  user for raw JSON (`ParamsEditor` rows). Act first, then close popovers. No
  `setPointerCapture` before a 4px drag on canvas-like surfaces; never auto-pan while the
  pointer is inside.
- Mutating UI surfaces follow [`docs/optimistic-state.md`](docs/optimistic-state.md).
- Tests: identities `uitest-*`; verify effects via REST, not DOM-only; `page_as` resets its
  profile server-side; NEVER run `playwright install`.
- Mutations carry `X-Coloom-Client` (echo absorption) and percent-encoded `X-Coloom-Profile`
  (attribution) headers.
- Tapestry-Loom reference checkout: `~/projects2/weird-personas/Tapestry-Loom` (branch
  `new-format`).
