# Setups API contract (model setups + sampler setups)

Two-layer, user-composable inference config (replaces "presets are the only way"):

- **Model setup** — an endpoint + model + default params: `{id, name, base_url,
  api_key | api_key_env, model, params}`. `params` is an **arbitrary** JSON object of
  API flags (temperature, top_p, logit_bias, anything) merged into the completion
  request body — arbitrary-flag support is the point, do not whitelist keys.
- **Sampler setup** — a named reference to a model setup plus overrides:
  `{id, name, model_setup_id, params}`. E.g. model "gpt-4.5-base @ temp 0.8", then
  sampler "wild" overriding `{temperature: 1.2}` and sampler "safe" `{temperature: 0.4}`
  — **both can be active at the same time** (activation is per-client UI state, NOT
  server state; the client fans out one /gen call per active sampler).

## Param merge order (later wins)

```
{model: model_setup.model, logprobs: <server default>, ...}
  ← model_setup.params
  ← sampler_setup.params
  ← request.params           (per-request overrides from GenControls)
```

Unknown keys pass through to the `/v1/completions` JSON body verbatim.

## Storage

Server-global SQLite tables (same db as weaves, not per-weave): `model_setups`,
`sampler_setups`. `api_key` stored as given; `api_key_env` resolves from the server's
environment at request time (exactly one of the two may be set; both null is allowed
for keyless endpoints like llama.cpp). **GET responses must redact `api_key` to
`"***"` when set** (never echo secrets); PATCH with `api_key: null` clears it,
omitted = unchanged.

## Endpoints

```
GET    /setups                       → {models: ModelSetup[], samplers: SamplerSetup[]}
POST   /setups/models                → ModelSetup        (201)
PATCH  /setups/models/{id}           → ModelSetup        (partial update)
DELETE /setups/models/{id}           → 204               (409 if a sampler references it)
POST   /setups/samplers              → SamplerSetup      (201)
PATCH  /setups/samplers/{id}         → SamplerSetup
DELETE /setups/samplers/{id}         → 204
```

ids: server-minted (same id helper as nodes). Names need not be unique (ids rule).
404 on unknown id; 400 on bad references (sampler → missing model_setup_id) or
api_key+api_key_env both set.

## Generation

`GenRequest` gains `sampler_id: str | None`. Resolution order in `/gen`:
`sampler_id` (→ its model setup → merge chain above) beats `preset` beats server
default preset. The raw request/response stored in the node's Creator must reflect
the *merged* request actually sent (with api_key redacted in raw_request).
`gen_started`/`gen_finished` events: include `sampler` (sampler name) when used,
keep `preset` field as-is otherwise.

YAML presets stay untouched and read-only (GET /presets unchanged) — they coexist;
the UI shows them as non-editable entries.

## Out of scope

No WS events for setups CRUD (global, not weave-scoped); clients refetch
`GET /setups` after their own mutations and on setups-dialog open.
