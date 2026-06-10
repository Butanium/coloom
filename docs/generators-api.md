# Generators API contract (templates + per-profile generators)

Supersedes `setups-api.md` (model setups + sampler setups + read-only presets —
four nouns for what the user thinks of as one thing). Design converged with
Clément 2026-06-10.

## The two nouns

- **Template** — a complete generator definition on the shelf, **server-global**:
  `{id, name, builtin, base_url, model, api_key | api_key_env, params}`.
  `params` is an arbitrary JSON object of API flags merged into the completion
  request body (do not whitelist keys). `builtin=true` templates are imported
  from `coloom.yaml` presets at boot (upsert by name) and are **read-only via
  the API** (PATCH/DELETE → 403). Templates never generate directly.
- **Generator** — the concrete, activatable thing, **per-profile** (nobody can
  edit your sampling strategy or your `n`):
  `{id, profile, name, parent, base_url?, model?, api_key? | api_key_env?, params}`.
  `parent` is `{kind: 'template' | 'generator', id} | null`. All definition
  fields are nullable = inherited; `params` holds only overridden keys.
  A parent generator must belong to the same profile. Cycles rejected on write.

## Resolution

Walk the parent chain leaf → root (generator → … → template). Scalar fields:
nearest-set wins. `params`: merge root → leaf, leaf wins. Final request body:

```
{model: resolved.model, logprobs: <server default>, ...}
  ← resolved.params
  ← request.params          (per-request overrides; CLI/agents still use this)
```

A parentless generator must resolve to non-empty `base_url` + `model` to be
usable; `/gen` with an unresolvable generator → 400.

## Endpoints

```
GET    /templates                  → Template[]
POST   /templates                  → 201   (accepts {from_generator: id} to
                                            promote: materialize the generator's
                                            RESOLVED fields into a new template)
PATCH  /templates/{id}             → Template      (403 if builtin)
DELETE /templates/{id}             → 204           (403 if builtin; FLATTENS
                                            inheriting generators: materialize
                                            resolved fields into them first)
GET    /generators?profile=NAME    → Generator[]  (each item carries both the
                                            raw overrides AND a `resolved` view)
POST   /generators                 → 201   {profile, name, ...fields} or
                                            {from: {kind,id}, mode: 'inherit'|'duplicate', profile, name?}
PATCH  /generators/{id}            → Generator    (partial; explicit null clears
                                            a field back to inherited; a params
                                            key set to null removes the override)
DELETE /generators/{id}            → 204   (flattens child generators)
```

- `mode: 'inherit'` → `parent = from`, empty overrides.
- `mode: 'duplicate'` → source is a generator: literal row copy (same parent,
  same overrides, new id/name); source is a template: `parent = null`, all
  template fields copied.
- ids server-minted (same helper as nodes). 404 unknown id, 400 bad parent ref /
  cycle / api_key+api_key_env both set / cross-profile parent.
- **Redaction**: GET responses redact `api_key` to `"***"` when set (resolved
  view too, and raw_request stored in Creators). PATCH `api_key: null` clears,
  omitted = unchanged.

## Events (the spooky-action answer)

Template/generator mutations ARE logged + broadcast (unlike old setups):
`template_created/updated/deleted`, `generator_created/updated/deleted` with
payload `{id, name, profile?, by, origin}` — `by` = actor profile name (from the
`X-Coloom-Profile` header clients already authenticate-ish with; CLI uses its
logged-in profile), `origin` = the round-4 `X-Coloom-Client` echo-absorption id.
These are global, not weave-scoped: broadcast to every connected WS client
(clients filter). UI consequences:

- Activity feed shows "clément edited template gpt4-base".
- A chip whose **ancestor** changed with `by ≠ me` gets a tint/badge until
  focused/inspected (tooltip: "template gpt4-base changed by clément").
  Remote edits can only ever move a chip's *inherited* (placeholder) fields —
  overridden fields are untouchable by others.

## Endpoint probe (reachability + model suggestions)

```
POST /probe-endpoint   {base_url, api_key? | api_key_env?}
                       → {ok: bool, error: str | null, models: string[]}
```

Server-side httpx GET of `{base_url}/models` (OpenAI-compatible), short timeout
(~3s); `ok=false` + a human-readable `error` on connect failure / non-2xx /
unparseable body, `models` = the listed model ids (empty if the endpoint is up
but doesn't implement /models — that's `ok=true`). Never echoes the key. The
probe goes through the server because the browser can't (CORS) and must not
(secrets) call third-party endpoints directly.

UI: in the template/generator edit form, probe automatically (debounced) when
base_url/key fields change → a small reachable/unreachable indicator, and the
returned model ids feed a `<datalist>` autocomplete on the `model` field.
gpt-fake gains a `/v1/models` route so this is testable against the mock.

## Seeding

Each profile starts with one inheriting generator per **builtin** template
(name = template name). Runs at: profile creation, and server boot for active
profiles missing a generator derived from a builtin template (so new yaml
entries appear for everyone). Promoted (non-builtin) templates never auto-seed —
you create from them explicitly.

## Generation

`GenRequest`: `generator_id` replaces `sampler_id` (clean break; CLI updated).
Multi-active fan-out stays client-side: one `/gen` per active generator.
`gen_started/finished` events carry `generator` (name). `preset` fallback dies
with the presets API (`GET /presets` removed; yaml presets only exist as
builtin templates now).

## Migration (additive, in `_MIGRATIONS` style)

1. `model_setups` rows → templates (`builtin=0`).
2. For each active profile × each `sampler_setups` row → a generator
   `{profile, name: sampler.name, parent: template(model_setup_id), params: sampler.params}`.
3. yaml presets → builtin templates, then seeding (above).
4. Old tables stay in the db (harmless); `/setups/*` endpoints removed.
5. Profile settings `activeGenerators`/`hiddenGenerators` hold old
   `{kind:'sampler'|'preset', id}` refs → the **frontend** migrates by name
   match to `{kind:'generator', id}` on login, best effort, then persists.

## Backend implementation notes (2026-06-10, binding for clients)

- **`X-Coloom-Profile` is percent-encoded UTF-8** (`encodeURIComponent` /
  `urllib.parse.quote`): HTTP headers can't carry `clément` raw. The server
  unquotes; ASCII names are unaffected either way.
- **Resolved view shape**: every generator in GET/POST/PATCH responses carries
  `resolved: {base_url, model, api_key, api_key_env, params}` (api_key
  redacted) plus a top-level `usable: bool` (false until base_url + model
  resolve non-empty). `GET /generators/{id}` also exists (additive).
- **`migrated_from: string|null`** on generators: the legacy `sampler_setups`
  id a migrated generator came from (null otherwise; cleared on duplicate) —
  lets the frontend map old `{kind:'sampler', id}` settings refs exactly
  instead of by name.
- **Global events mechanism**: template/generator events are logged with
  `weave_id: ""` (a sentinel no real weave id can collide with). The WS hub
  delivers them to every subscriber regardless of weave filter, and
  weave-filtered `GET /events?weave_id=X` includes the `""` rows too —
  clients filter by type. `by` is null when the header is absent.
- **Credential resolution is joint**: the nearest chain row that sets *either*
  api_key or api_key_env wins *both* — a child api_key cleanly overrides an
  ancestor api_key_env (a resolved view never has both set).
- **PATCH params semantics apply to templates too** (per-key merge, null
  removes the key); `params: null` wholesale clears all overrides.
- **Builtin import source**: `config.presets`, falling back to
  `config.endpoints` when no presets are defined. Endpoints shadowed by
  presets are *not* imported separately (would duplicate chips).
- **Boot is silent**: builtin upsert + boot-time seeding log no events (no
  clients connected yet; keeps the feed clean). Runtime seeding (profile
  creation via `PUT /profiles/{name}`) emits `generator_created` with
  `seeded: true`; seeding also runs idempotently on every settings save.
- **"Never resurrect" seeding**: a `generator_seeds(profile, template_id)`
  table records every pair ever seeded; deleting a seeded generator never
  brings it back (only a genuinely NEW builtin template seeds again).
- **Flatten-on-delete emits `generator_updated`** for each materialized child
  (payload extra `flattened_from: 'template'|'generator'`), before the
  `*_deleted` event, all in one transaction.
- `gen_started`/`gen_finished` payloads carry `generator` (name) **and**
  `generator_id`.
- **Probe statuses**: connect failure / non-2xx (other than 404/405/501) /
  unparseable body → `ok=false` + error; 404/405/501 → up-but-no-/models →
  `ok=true, models=[]`; unset `api_key_env` → `ok=false` without any network
  call.
- **Probe by id** (re-probing an EXISTING row whose stored key the client only
  sees as `"***"`): `POST /probe-endpoint {template_id | generator_id,
  base_url?}` — the server resolves the stored/inherited credentials (joint
  resolution, same as /gen) and probes the stored/resolved base_url; an
  explicit `base_url` in the request wins (the user is editing the URL field).
  `template_id`/`generator_id` are mutually exclusive with each other and with
  literal `api_key`/`api_key_env` (400). When the user types a NEW key into
  the form, send the literal shape instead (the field is no longer `"***"`).
  An id that resolves to no base_url → `ok=false` (operational, not 400).
- CLI: `coloom gen --generator <id-or-name>`, `coloom templates
  list|create|promote|rm`, `coloom generators list|create|update|rm`
  (`--parent template:<id-or-name>|generator:<id>`; field value `null` /
  `--param k=null` clears an override back to inherited).

## Frontend behavior (summary; details in code review)

- **Chips**: body click = **focus** (distinct outline; the quick row + drag
  edit the focused generator, persisted via debounced PATCH); a small leading
  **dot toggles active** (generation fan-out). Digit keys keep toggling active
  of the k-th visible chip. Focus follows the last body-click; default = first
  active else first visible chip.
- **Quick row** (temp / max_tokens / n + drag-to-adjust): binds to the focused
  generator's params. Placeholders show inherited values; clearing a field
  removes the override (falls back to inherited). The old request-level
  override layer disappears from the UI (API keeps `request.params` for CLI).
- **Drawer**: my generators + templates list; single edit form with per-field
  inherited-placeholder vs typed-override + clear-to-inherit; parent picker;
  create flow (from scratch / from template / from existing generator ×
  inherit / duplicate); "promote to template"; delete warns it flattens
  children; builtin templates read-only with "new generator from this".
- **Hidden** stays a per-profile client setting (hide ≠ delete; deleting a
  seeded generator would resurrect on re-seed only if a NEW builtin template
  appears, but hide is still the right tool for "keep but no chip").
