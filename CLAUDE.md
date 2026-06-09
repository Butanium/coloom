# coloom

Real-time **human + AI agent collaborative looming** over LLM base models: a shared, branching
tree of completions (with per-token logprobs) that a human and an agent weave *together*, live.

**Status: day zero.** This repo currently holds the README, license, and the design plan. No
application code yet. The full design + build sequence lives in [`docs/PLAN.md`](docs/PLAN.md) —
read it first.

## Stack (planned)
- **Backend** (`core` + `server`): Python, `uv`, FastAPI (REST) + WebSocket for change events.
  The server owns the canonical weave and pushes change events so every client stays in sync.
- **Persistence**: SQLite (nodes/edges/metadata; partial updates). JSON for the API + export.
- **Inference**: `httpx` against OpenAI-compatible `/v1/completions` (+ chat) with logprobs
  (llama.cpp `llama-server`, vLLM first).
- **CLI** (`coloom`): Python, agent-facing — JSON in/out, non-interactive, an HTTP client to the server.
- **Web frontend**: TypeScript SPA, **separate repo**, on the server's REST + WS.
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
