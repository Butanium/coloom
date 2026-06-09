# coloom

Real-time **human + AI agent collaborative looming** over LLM base models: a shared, branching
tree of completions (with per-token logprobs) that a human and an agent weave *together*, live.

**Status: backend milestones 1–5 done** (2026-06-09): weave model + SQLite store, inference with
logprobs (smoked against gpt4-base), FastAPI server, agent CLI, live WebSocket events. 40 tests in
`tests/` (`uv run pytest`). The web UI (in-repo `web/`, Vite + Svelte) is next. Design rationale in
[`docs/PLAN.md`](docs/PLAN.md).

## Layout
- `src/coloom/models.py` — pydantic weave model (Node, Snippet/Tokens, Creator attribution)
- `src/coloom/store.py` — SQLite weave store, **directly canonical** (every mutation a transaction;
  deliberate deviation from PLAN's in-memory+debounce idea). Edges in their own table → DAG-ready.
  Also the append-only `events` table (the WS/polling change feed).
- `src/coloom/inference.py` + `config.py` — httpx `/v1/completions` client with the
  polyparser-derived edge-case handling; YAML endpoints/presets (`coloom.example.yaml`)
- `src/coloom/server/` — FastAPI REST + `/ws` EventHub; `uv run coloom-server`
- `src/coloom/cli.py` — agent-facing CLI (`uv run coloom …`), JSON stdout / logs stderr
- `scripts/small-smokes/` — live smokes against gpt4-base (`smoke_generate.py`,
  `smoke_coweave_e2e.py`); re-run when the pipeline changes
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
Nodes with stable ids, parent/child links (DAG-capable for merges), an active path, roots, bookmarks.
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
