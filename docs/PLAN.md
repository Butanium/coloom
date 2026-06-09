# Plan: `coloom` — an agent-human co-weaving tool (Python + TS)

> **Status 2026-06-09: milestones 1–5 implemented** (model+store, inference, server, CLI, live
> events; 40 tests + live gpt4-base smokes). Deliberate implementation deviations: SQLite is
> directly canonical (no in-memory + debounced layer — fewer crash/sync failure modes, partial
> updates are what SQLite is for); `Tokens` nodes split at **token boundaries** (index), Snippets
> at char offsets — not byte offsets (sidesteps Tapestry's mid-token logprob-duplication
> machinery); text stored as `str` not bytes (JSON/web-native). Tapestry's sibling-dedup-on-add
> and `set_active_content` (diff-style editing of the flattened active text) are noted as
> post-milestone features. Milestone 6 (web frontend) is next.
>
> **Decision 2026-06-09 (supersedes "separate repo" below): the web frontend lives in THIS repo,
> under `web/`** — a Vite + Svelte SPA (no SvelteKit; no SSR/routing needs). Rationale: the
> frontend is tightly coupled to the API and the team is one human + one agent, so cross-repo
> coordination is pure friction; colocating lets e2e tests drive server+UI together and lets
> FastAPI serve the built `web/dist/`, making `coloom-server` a single self-contained deployable.

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
  parse `logprobs`/`top_logprobs`/token ids. **Dev/test endpoint: the OpenAI API with `gpt4-base`**
  (special access; `OPENAI_API_KEY` in the repo-local `.env`). llama.cpp `llama-server` and vLLM are
  the other first-class targets.
- **CLI**: Python (`click`/argparse), an HTTP client to the server (primary), JSON in/out, non-interactive.
- **Web frontend**: TypeScript SPA — **Vite + Svelte, in-repo under `web/`** (decision 2026-06-09;
  originally planned as a separate repo) — consuming the server's REST + WS.

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
- `events --since <cursor>` — poll change events newer than the last look, so a non-resident agent can
  catch up on what the human (or another agent) did without holding a WebSocket open.
- (Later) `bookmark`, `rm`, `list-leaves`, logprob export; an MCP wrapper so agents get native tool access.

## Web frontend (`web/`, Vite + Svelte)

TS SPA on the server's REST + WS: tree/graph view, active-path editing, per-token logprob/confidence
display, human-vs-agent node coloring (from `creator`), live updates. FastAPI serves the built
`web/dist/` so `coloom-server` ships the UI.

**When viewer work starts**: have a teammate/subagent audit the full Tapestry-Loom viewer feature set
(`src/editor/`, `Getting Started.md`, README roadmap) so we consciously pick features rather than
rediscover them — Tapestry Loom is the feature baseline for the viewer (Clément's call, 2026-06-09).

## Build sequence / milestones

1. **Scaffold + model**: `uv` project; `core` weave store over SQLite; round-trip create/add/read/active;
   pytest with small in-memory weaves.
2. **Inference**: `core.generate` against an OpenAI-completions endpoint with logprobs; parse into `Tokens`.
3. **Server**: FastAPI REST over `core`; SQLite persistence.
4. **CLI**: client commands against the server; the agent co-weave loop (`read` → `gen`/`add` → observe).
5. **Live events**: WebSocket broadcast; two clients observe each other's edits.
6. **Web frontend** (`web/`, Vite + Svelte) on the API.
7. (Later) chat-completions inference, merges/multi-parent, MCP wrapper, JSON import of others' looms.

## Verification

- **Model**: pytest round-trips (create → add branches → set active → reload) asserting tree shape,
  active thread, attribution.
- **Inference**: smoke `core.generate` against the OpenAI API with `gpt4-base` (key in `.env`); assert the
  node has per-token logprobs + top_logprobs populated. Capture a sample response and add a parser unit
  test (add llama.cpp / vLLM sample fixtures when we first point at those). Smoke prompts: anything fun.
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
- The README **roadmap** (on `new-format`) — feature backlog (`TL#` request post-processing, sorting,
  stats) + "take inspiration from multiverse/mikupad/loom". Cherry-pick. (FIM, blind comparison, and
  adaptive looming were reviewed and ruled out — see "Out of scope" below.)

You'll likely form sharper opinions by poking around yourself — these notes are to skip the cold-start, not to fence you in.

## Out of scope (decided 2026-06-09)

From the Tapestry-Loom roadmap, explicitly **not** wanted for coloom: **FIM** (fill-in-the-middle
insertions), **blind comparison modes**, and **adaptive looming** (uncertainty-based node-length
cutting). Listed here so future sessions don't re-propose them.

## Notes / open items (non-blocking)
- Repo: `coloom`, public, on Butanium (github.com/Butanium/coloom); web frontend in-repo under
  `web/` (2026-06-09, superseding the earlier sibling-repo plan).
- Maintainer reply may open future collaboration, but does not change this plan.
- SQLite vs flat-JSON persistence: SQLite chosen for partial updates at scale (weaves reach tens of MB,
  mostly token logprobs); revisit if it adds friction early.
