# Deprecated reusable modules

- `setups.py` — two-layer inference config (ModelSetup + SamplerSetup pydantic
  models, `resolve_sampler()` merge). Deprecated 2026-06-10: replaced by the
  templates + per-profile generators model (`src/coloom/generators.py`,
  contract in `docs/generators-api.md`) — four nouns (endpoints, presets,
  model setups, sampler setups) collapsed into two (templates, generators)
  with parent-chain inheritance. The old `model_setups`/`sampler_setups`
  SQLite tables stay on disk; `WeaveStore._migrate_setups_to_generators()`
  imports their rows once at store init.
