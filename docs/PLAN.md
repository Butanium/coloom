# Plan: `coloom` — an agent-human co-weaving tool (Python + TS)

## Context

Goal: a tool where a **human and an AI agent collaborate in real time on the same "loom"** — a
branching tree of LLM **base-model** completions (with per-token logprobs), à la
[Tapestry Loom](https://github.com/transkatgirl/Tapestry-Loom) / loom / mikupad. The human explores
branches in a web UI; an agent (e.g. Claude via a CLI) reads the tree, generates its own branches
through the same base-model endpoint, and writes them back — both sides seeing each other's edits live.

Decisions reached during scoping (this supersedes the earlier Rust-based draft of this plan):

- **This is our own tool, separate repo, full control.** Real-time human+agent collaboration is not on
  Tapestry Loom's roadmap and would be a major directional change to their desktop egui app, so we build
  independently rather than fork/PR. (A courtesy heads-up to the maintainer is in flight; it does not gate us.)
- **No `.tapestry` interop.** We do not need to open files in the official GUI. That removes the only thing
  that forced Rust (the `.tapestry` rkyv binary format). So: **pure Python + TypeScript, no Rust.**
- **We keep their format's good *design*, not its encoding.** Build our own JSON/SQLite-native weave model
  inspired by Tapestry Loom's v1 design (below).
- **Server-as-authority for concurrency.** A single server owns the canonical weave and pushes change
  events; every client (web UI, agent CLI) stays in sync. No shared-file clobbering, no merge gymnastics.
- **Stack fits the user** (a Python researcher): Python backend is easy to maintain, hack on, and wire into
  their existing research/vLLM/logprob tooling; TS for the web UI; I can iterate fast in both.

Outcome target: a first milestone where the agent (and a human via UI) can create a weave, generate
base-model branches with logprobs from a configured endpoint, add manual branches, and see each other's
edits live.

## Stack

- **Backend (core + server)**: Python, **FastAPI** (REST) + **WebSocket** for change events. `uv` project.
- **Persistence**: **SQLite** (nodes/edges/metadata; transactional, cheap partial updates, no full-file
  rewrites). JSON for API payloads and import/export.
- **Inference**: Python `httpx` against OpenAI-compatible `/v1/completions` (+ chat) with logprobs;
  parse `logprobs`/`top_logprobs`/token ids. Target **llama.cpp `llama-server`** and **vLLM** first.
- **CLI**: Python (`click`/argparse), an HTTP client to the server (primary), JSON in/out, non-interactive.
- **Web frontend**: TypeScript SPA, **separate repo**, consuming the server's REST + WS.

## Weave model (our own; inspired by Tapestry Loom v1)

A weave = nodes + roots + active path + bookmarks + metadata.

- **Node**: `{ id, parents: [id], children: [id], content, creator, timestamp, modified, bookmarked, metadata }`.
  Allow multiple parents (DAG) to support merges later; tree is the common case.
- **content**: one of
  - `Snippet{ text }` — plain text (manual/agent-written branches), or
  - `Tokens{ tokens: [ { text, logprob, token_id?, entropy?, top_logprobs: [ {text, logprob, token_id?} ] } ] }`
    — base-model generations with full per-token logprob info (the research-valuable shape).
- **creator**: `Human{ label, color?, id? }` | `Model{ label, color?, id?, seed?, raw_request?, raw_response? }`
  | `Unknown`. This is the human-vs-agent attribution; `Model` optionally stores the seed + raw API exchange
  for reproducibility.
- **Weave**: `{ id, nodes, roots: [id], active_path: [id], bookmarks: [id], metadata: {title, description, created} }`.
- **active path** = the currently-selected root→leaf thread; `get_active_content()` = concatenated text.

Stored in SQLite (a `nodes` table keyed by id with parent/child edges + JSON content/creator columns,
a `weave` row for metadata/active/roots/bookmarks). Exposed/exported as JSON.

## Backend: `core`

Pure Python library (no web framework), unit-testable:
- **Weave store**: load/save against SQLite; CRUD on nodes; `add_node`, `set_active`, `get_active_content`,
  `get_active_thread`, `split_node`, `remove_node`, bookmarks, roots. Mirror the useful ops from Tapestry
  Loom's `v0::TapestryWeave` API (as behavioral reference, reimplemented in Python).
- **Inference client**: `generate(endpoint_config, prompt, sampling_params) -> [Node]`. Builds the
  `/v1/completions` request (model, temp/top_p/top_k/min_p, max_tokens, n, logprobs), POSTs via httpx,
  parses response into `Tokens` content with logprobs + top_logprobs (+ token ids when present). Start
  with completions; add chat-completions variant after. (Reference: Tapestry Loom's request shapes &
  logprob parsing in `src/settings/inference/{openai,polyparser}.rs` — reimplement the slice we need.)
- **Config**: our own config (YAML/JSON) of endpoints + named presets (endpoint URL, model, sampling, headers).

## Backend: `server` (the authority)

- FastAPI app holding open weaves in memory (canonical), persisting to SQLite (debounced + on change).
- **REST**: get tree / node / active-thread; `POST gen` (generate from a node); `POST add` (manual branch);
  `PUT active`; bookmarks; create/list weaves.
- **WebSocket**: broadcast change events (`node_added`, `active_changed`, `node_removed`, …) to all clients,
  so the web UI and any agent CLI see edits in real time. Only the server writes persistence.

## CLI (`coloom`)

HTTP client to the server. JSON on stdout, logs on stderr, non-interactive, exit codes.
- `read [--active|--tree|--node ID] [--text]`, `add --parent ID [--text|--stdin] [--set-active]`,
  `set-active ID`, `gen [--node ID] [--preset NAME] [-n N]`, `new [--text]`.
- (Later) `bookmark`, `rm`, `list-leaves`, logprob export; an MCP wrapper so agents get native tool access.

## Web frontend (separate repo, later)

TS SPA on the server's REST + WS: tree/graph view, active-path editing, per-token logprob/confidence
display, human-vs-agent node coloring (from `creator`), live updates. Out of scope beyond ensuring the
server API supports it.

## Build sequence / milestones

1. **Scaffold + model**: `uv` project; `core` weave store over SQLite; round-trip create/add/read/active;
   pytest with small in-memory weaves.
2. **Inference**: `core.generate` against an OpenAI-completions endpoint with logprobs; parse into `Tokens`.
3. **Server**: FastAPI REST over `core`; SQLite persistence.
4. **CLI**: client commands against the server; the agent co-weave loop (`read` → `gen`/`add` → observe).
5. **Live events**: WebSocket broadcast; two clients observe each other's edits.
6. **Web frontend** (separate repo) on the API.
7. (Later) chat-completions inference, merges/multi-parent, MCP wrapper, JSON import of others' looms.

## Verification

- **Model**: pytest round-trips (create → add branches → set active → reload) asserting tree shape,
  active thread, attribution.
- **Inference**: run `llama.cpp llama-server` with a small base GGUF on truthful-1 (CPU; slow but fine for a
  smoke) exposing `/v1/completions` with logprobs; `core.generate` and assert the node has per-token
  logprobs + top_logprobs populated. Capture a sample response and add a parser unit test (llama.cpp + vLLM).
- **Server + WS**: start server; two `coloom --server` clients; A `add`s, assert B receives the WS event
  and SQLite reflects it.
- **End-to-end**: agent generates a branch via CLI while a second client (or the web UI later) sees it live.

## Tapestry-Loom: places worth a look (inspiration, not gospel)

Local checkout: `~/projects2/weird-personas/Tapestry-Loom` (currently on the `new-format` branch, where
`v1.rs` lives; `main` only has v0). These are pointers from this session's reading — trust your own read
over mine, and feel free to ignore the lot and explore fresh. Our model should be whatever best serves the
web UI + agent loop, **not** a port of theirs; v1 is a *donor of ideas*, not a target.

What I read closely (reasonably confident):
- `tapestry-weave/src/v1.rs` — richest source for our schema: `NodeContent`, `InnerNodeToken`
  (logprob/entropy/counterfactual/original), `Creator`/`Model`/`Author`, token split/merge, weave metadata.
- `tapestry-weave/src/v0.rs` — simpler, but its `set_active_content` (~L360–450) is a neat "given new
  active-thread text, decide which nodes to keep/split/add" algorithm; may map to our editing logic.
- `tapestry-weave/src/lib.rs` — the `VersionedWeave` envelope + v0→v1 upgrade; small, shows their versioning.

What I only have a *secondhand map* of (a subagent read it — verify before trusting):
- `src/settings/inference/polyparser.rs` — robustly extracts logprobs/top_logprobs/token-ids across many
  response shapes (OpenAI completions + chat, Gemini, Claude). Best reference for our Python parser; port
  only the slice we need.
- `src/settings/inference/openai.rs` — request building (completions vs chat), token-reuse trick, FIM.
- `src/settings/inference/mod.rs` — `create_request`/`get_responses`, `recursion_depth` (auto/recursive
  generation — interesting feature idea), presets, caching.

What I never opened (genuinely unexplored — maybe the most interesting):
- `universal-weave/src/{independent,dependent}.rs` — the abstract weave traits + the DAG/multi-parent
  ("independent") model v1 builds on. Start here if we want merges/multi-parent.
- `src/editor/{graph,canvas,lists,textedit}.rs` — their tree/graph/canvas views; egui-specific but rich
  UX inspiration for the web frontend.
- `Getting Started.md` — intended mental model / interaction patterns; probably worth reading before UI design.
- `migration-assistant/src/` — converters from loom/loomsidian/exoloom/pyloom; reference for a future
  "import other looms" feature.
- The README **roadmap** (on `new-format`) — feature backlog (FIM, `TL#` request post-processing, adaptive
  looming, sorting, blind-comparison, stats) + "take inspiration from multiverse/mikupad/loom". Cherry-pick.

You'll likely form sharper opinions by poking around yourself — these notes are to skip the cold-start, not to fence you in.

## Notes / open items (non-blocking)
- Repo: `coloom`, public, on Butanium (github.com/Butanium/coloom); web frontend a sibling repo.
- Maintainer reply may open future collaboration, but does not change this plan.
- SQLite vs flat-JSON persistence: SQLite chosen for partial updates at scale (weaves reach tens of MB,
  mostly token logprobs); revisit if it adds friction early.
