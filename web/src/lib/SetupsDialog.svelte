<script lang="ts" module>
  /** Prefill for "start from an existing setup/preset, then edit". */
  export interface SetupsPrefill {
    model?: {
      name?: string
      base_url?: string
      model?: string
      api_key_env?: string | null
      params?: Record<string, unknown>
    }
    sampler?: {
      name?: string
      model_setup_id?: string
      params?: Record<string, unknown>
    }
    editSamplerId?: string
  }
</script>

<script lang="ts">
  // Two-layer inference setups manager (docs/setups-api.md). Model setups carry
  // an endpoint + arbitrary default API flags; sampler setups reference a model
  // and override params. Server-global state — every mutation refetches /setups.
  import { api } from './api'
  import ParamsEditor, {
    paramsFromRows,
    paramsSummary,
    rowsFromParams,
    type ParamRow,
  } from './ParamsEditor.svelte'
  import {
    type GeneratorRef,
    isGeneratorActive,
    refreshSetups,
    session,
    toast,
    toggleActiveGenerator,
    withToast,
  } from './state.svelte'
  import type { ModelSetup, SamplerSetup } from './types'

  let {
    onclose,
    prefill = null,
  }: { onclose: () => void; prefill?: SetupsPrefill | null } = $props()

  const models = $derived(session.setups?.models ?? [])
  const samplers = $derived(session.setups?.samplers ?? [])

  function modelName(id: string): string {
    return models.find((m) => m.id === id)?.name ?? '(missing model)'
  }

  // ---- model form (create + edit share one panel) ----
  let mEditingId = $state<string | null>(null)
  let mName = $state('')
  let mBaseUrl = $state('')
  let mModel = $state('')
  let mApiKeyMode = $state<'keep' | 'set' | 'clear'>('set')
  let mApiKey = $state('')
  let mApiKeyEnv = $state('')
  let mParams = $state<ParamRow[]>([])
  let mError = $state<string | null>(null)

  function resetModelForm() {
    mEditingId = null
    mName = ''
    mBaseUrl = ''
    mModel = ''
    mApiKeyMode = 'set'
    mApiKey = ''
    mApiKeyEnv = ''
    mParams = []
    mError = null
  }

  function editModel(m: ModelSetup) {
    mEditingId = m.id
    mName = m.name
    mBaseUrl = m.base_url
    mModel = m.model
    // server redacts api_key to "***" — we can't show it, so editing defaults
    // to "keep" (omit on PATCH = unchanged). api_key_env is not secret.
    mApiKeyMode = 'keep'
    mApiKey = ''
    mApiKeyEnv = m.api_key_env ?? ''
    mParams = rowsFromParams(m.params)
    mError = null
  }

  async function saveModel() {
    mError = null
    let params: Record<string, unknown>
    try {
      params = paramsFromRows(mParams)
    } catch (e) {
      mError = e instanceof Error ? e.message : `${e}`
      return
    }
    if (!mName.trim()) {
      mError = 'name is required'
      return
    }
    if (!mBaseUrl.trim()) {
      mError = 'base_url is required'
      return
    }
    if (!mModel.trim()) {
      mError = 'model is required'
      return
    }
    // api_key (literal) / api_key_env (env var name) are mutually exclusive; we
    // send them honestly and let the server 400 when both are set rather than
    // silently dropping the user's input.
    const env = mApiKeyEnv.trim()
    const base = { name: mName.trim(), base_url: mBaseUrl.trim(), model: mModel.trim(), params }
    try {
      if (mEditingId) {
        // mApiKeyMode governs the LITERAL key: keep=omit (unchanged), set=replace,
        // clear=null. api_key_env is sent independently (always reflects the field).
        const fields: Partial<Omit<ModelSetup, 'id'>> = { ...base, api_key_env: env || null }
        if (mApiKeyMode === 'set') fields.api_key = mApiKey
        else if (mApiKeyMode === 'clear') fields.api_key = null
        await api.updateModelSetup(mEditingId, fields)
      } else {
        const fields: Omit<ModelSetup, 'id'> = {
          ...base,
          api_key: mApiKeyMode === 'set' && mApiKey ? mApiKey : null,
          api_key_env: env || null,
        }
        await api.createModelSetup(fields)
      }
      await refreshSetups()
      resetModelForm()
    } catch (e) {
      mError = e instanceof Error ? e.message : `${e}`
    }
  }

  async function deleteModel(m: ModelSetup) {
    // 409 if a sampler references it — surface as a toast, do not swallow.
    await withToast(async () => {
      await api.deleteModelSetup(m.id)
      await refreshSetups()
      if (mEditingId === m.id) resetModelForm()
    })
  }

  // ---- sampler form ----
  let sEditingId = $state<string | null>(null)
  let sName = $state('')
  let sModelId = $state('')
  let sParams = $state<ParamRow[]>([])
  let sError = $state<string | null>(null)

  function resetSamplerForm() {
    sEditingId = null
    sName = ''
    sModelId = models[0]?.id ?? ''
    sParams = []
    sError = null
  }

  function editSampler(s: SamplerSetup) {
    sEditingId = s.id
    sName = s.name
    sModelId = s.model_setup_id
    sParams = rowsFromParams(s.params)
    sError = null
  }

  async function saveSampler() {
    sError = null
    let params: Record<string, unknown>
    try {
      params = paramsFromRows(sParams)
    } catch (e) {
      sError = e instanceof Error ? e.message : `${e}`
      return
    }
    if (!sName.trim()) {
      sError = 'name is required'
      return
    }
    if (!sModelId) {
      sError = 'pick a model setup'
      return
    }
    try {
      if (sEditingId) {
        await api.updateSamplerSetup(sEditingId, {
          name: sName.trim(),
          model_setup_id: sModelId,
          params,
        })
      } else {
        await api.createSamplerSetup({
          name: sName.trim(),
          model_setup_id: sModelId,
          params,
        })
      }
      await refreshSetups()
      resetSamplerForm()
    } catch (e) {
      sError = e instanceof Error ? e.message : `${e}`
    }
  }

  async function deleteSampler(s: SamplerSetup) {
    await withToast(async () => {
      await api.deleteSamplerSetup(s.id)
      // dropping an active sampler: refreshSetups prunes the localStorage set
      await refreshSetups()
      if (sEditingId === s.id) resetSamplerForm()
    })
  }

  function duplicateModel(m: ModelSetup) {
    mEditingId = null
    mName = `${m.name} copy`
    mBaseUrl = m.base_url
    mModel = m.model
    mApiKeyMode = 'set' // the literal key can't be copied (server redacts it)
    mApiKey = ''
    mApiKeyEnv = m.api_key_env ?? ''
    mParams = rowsFromParams(m.params)
    mError = null
  }

  function duplicateSampler(s: SamplerSetup) {
    sEditingId = null
    sName = `${s.name} copy`
    sModelId = s.model_setup_id
    sParams = rowsFromParams(s.params)
    sError = null
  }

  // refetch on open so we never edit stale server-global state
  $effect(() => {
    void refreshSetups()
  })

  // apply the prefill once setups are loaded (clone-from-preset / edit jump)
  let prefillApplied = false
  $effect(() => {
    if (prefillApplied || !session.setups || !prefill) return
    prefillApplied = true
    if (prefill.editSamplerId) {
      const s = samplers.find((x) => x.id === prefill.editSamplerId)
      if (s) editSampler(s)
    }
    if (prefill.model) {
      mEditingId = null
      mName = prefill.model.name ?? ''
      mBaseUrl = prefill.model.base_url ?? ''
      mModel = prefill.model.model ?? ''
      mApiKeyMode = 'set'
      mApiKey = ''
      mApiKeyEnv = prefill.model.api_key_env ?? ''
      mParams = rowsFromParams(prefill.model.params ?? {})
    }
    if (prefill.sampler) {
      sEditingId = null
      sName = prefill.sampler.name ?? ''
      sModelId = prefill.sampler.model_setup_id ?? models[0]?.id ?? ''
      sParams = rowsFromParams(prefill.sampler.params ?? {})
    }
  })

  function onkeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') onclose()
  }
</script>

<svelte:window {onkeydown} />

<div
  class="overlay"
  role="presentation"
  onclick={(e) => {
    if (e.target === e.currentTarget) onclose()
  }}
>
  <div class="dialog" role="dialog" aria-modal="true" aria-label="inference setups">
    <header>
      <h2>inference setups</h2>
      <button class="x" onclick={onclose} aria-label="close">×</button>
    </header>

    <div class="cols">
      <!-- models -->
      <section class="col">
        <h3>model setups</h3>
        <ul class="list" data-testid="model-list">
          {#each models as m (m.id)}
            <li class:editing={mEditingId === m.id}>
              <div class="row-main">
                <span class="nm">{m.name}</span>
                <span class="meta">{m.model}</span>
              </div>
              <div class="row-sub">
                <span class="url">{m.base_url}</span>
                {#if m.api_key === '***'}<span class="key" title="api_key set">🔑</span>{/if}
                {#if m.api_key_env}<span class="key" title="from env">${m.api_key_env}</span>{/if}
              </div>
              <div class="actions">
                <button onclick={() => editModel(m)}>edit</button>
                <button onclick={() => duplicateModel(m)}>duplicate</button>
                <button class="danger" onclick={() => deleteModel(m)}>delete</button>
              </div>
            </li>
          {:else}
            <li class="empty">no model setups yet</li>
          {/each}
        </ul>

        <div class="form">
          <h4>{mEditingId ? 'edit model' : 'new model'}</h4>
          <input placeholder="name" bind:value={mName} data-testid="m-name" />
          <input placeholder="base_url (e.g. http://localhost:9999/v1)" bind:value={mBaseUrl} data-testid="m-base-url" />
          <input placeholder="model (e.g. gpt-fake)" bind:value={mModel} data-testid="m-model" />
          <div class="keyrow">
            {#if mEditingId}
              <label><input type="radio" value="keep" bind:group={mApiKeyMode} /> keep key</label>
            {/if}
            <label><input type="radio" value="set" bind:group={mApiKeyMode} /> set key</label>
            <label><input type="radio" value="clear" bind:group={mApiKeyMode} /> clear</label>
          </div>
          {#if mApiKeyMode === 'set'}
            <input placeholder="api_key (stored literal, never echoed)" bind:value={mApiKey} data-testid="m-api-key" />
          {/if}
          <input placeholder="api_key_env (env var name, mutually exclusive)" bind:value={mApiKeyEnv} data-testid="m-api-key-env" />
          <ParamsEditor bind:rows={mParams} testid="m-params" />
          {#if mError}<p class="err" data-testid="m-error">{mError}</p>{/if}
          <div class="form-actions">
            <button class="primary" onclick={saveModel} data-testid="m-save">
              {mEditingId ? 'save' : 'create'}
            </button>
            {#if mEditingId}<button onclick={resetModelForm}>cancel</button>{/if}
          </div>
        </div>
      </section>

      <!-- samplers -->
      <section class="col">
        <h3>sampler setups</h3>
        <ul class="list" data-testid="sampler-list">
          {#each samplers as s (s.id)}
            {@const ref = { kind: 'sampler', id: s.id } as GeneratorRef}
            <li class:editing={sEditingId === s.id} class:active={isGeneratorActive(ref)}>
              <div class="row-main">
                <label class="activate" title="activate for fan-out generation">
                  <input
                    type="checkbox"
                    checked={isGeneratorActive(ref)}
                    onchange={() => toggleActiveGenerator(ref)}
                    data-testid={`s-active-${s.id}`}
                  />
                  <span class="nm">{s.name}</span>
                </label>
                <span class="meta">→ {modelName(s.model_setup_id)}</span>
              </div>
              <div class="row-sub">
                <span class="params">{paramsSummary(s.params)}</span>
              </div>
              <div class="actions">
                <button onclick={() => editSampler(s)}>edit</button>
                <button onclick={() => duplicateSampler(s)}>duplicate</button>
                <button class="danger" onclick={() => deleteSampler(s)}>delete</button>
              </div>
            </li>
          {:else}
            <li class="empty">no sampler setups yet</li>
          {/each}
        </ul>

        <div class="form">
          <h4>{sEditingId ? 'edit sampler' : 'new sampler'}</h4>
          <input placeholder="name (e.g. wild)" bind:value={sName} data-testid="s-name" />
          <select bind:value={sModelId} data-testid="s-model">
            <option value="" disabled>pick a model setup…</option>
            {#each models as m (m.id)}
              <option value={m.id}>{m.name}</option>
            {/each}
          </select>
          <ParamsEditor bind:rows={sParams} testid="s-params" />
          {#if sError}<p class="err" data-testid="s-error">{sError}</p>{/if}
          <div class="form-actions">
            <button class="primary" onclick={saveSampler} disabled={models.length === 0} data-testid="s-save">
              {sEditingId ? 'save' : 'create'}
            </button>
            {#if sEditingId}<button onclick={resetSamplerForm}>cancel</button>{/if}
          </div>
          {#if models.length === 0}
            <p class="hint">create a model setup first</p>
          {/if}
        </div>
      </section>
    </div>
  </div>
</div>

<style>
  .overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.55);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 100;
  }
  .dialog {
    background: var(--bg-raised);
    border: 1px solid var(--border);
    border-radius: 8px;
    width: min(900px, 94vw);
    max-height: 88vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  header {
    display: flex;
    align-items: center;
    padding: 0.6rem 0.9rem;
    border-bottom: 1px solid var(--border);
  }
  header h2 {
    margin: 0;
    font-size: var(--fs-ui);
    font-weight: 600;
  }
  .x {
    margin-left: auto;
    font-size: 1.1rem;
    line-height: 1;
    padding: 0 0.4rem;
    background: none;
    border: none;
    color: var(--text-dim);
  }
  .x:hover {
    color: var(--text);
  }
  .cols {
    display: flex;
    gap: 1rem;
    padding: 0.9rem;
    overflow: auto;
  }
  .col {
    flex: 1;
    min-width: 0;
  }
  h3 {
    font-size: var(--fs-small);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-dim);
    margin: 0 0 0.5rem;
  }
  h4 {
    font-size: var(--fs-small);
    color: var(--text-dim);
    margin: 0 0 0.4rem;
  }
  .list {
    list-style: none;
    margin: 0 0 0.8rem;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }
  .list li {
    border: 1px solid var(--border);
    border-radius: 5px;
    padding: 0.4rem 0.55rem;
    background: var(--bg-card);
    font-size: var(--fs-ui);
  }
  .list li.editing {
    border-color: var(--accent);
  }
  .list li.active {
    border-color: var(--accent);
    box-shadow: inset 3px 0 0 var(--accent);
  }
  .list li.empty {
    color: var(--text-dim);
    font-size: var(--fs-small);
    text-align: center;
    background: none;
    border-style: dashed;
  }
  .row-main {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .nm {
    font-weight: 600;
  }
  .meta {
    color: var(--text-dim);
    font-size: var(--fs-small);
    margin-left: auto;
  }
  .row-sub {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    margin-top: 0.2rem;
    font-size: var(--fs-tiny);
    color: var(--text-dim);
  }
  .row-sub .url,
  .row-sub .params {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .activate {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    cursor: pointer;
  }
  .actions {
    display: flex;
    gap: 0.35rem;
    margin-top: 0.4rem;
  }
  .actions button {
    font-size: var(--fs-tiny);
    padding: 0.15rem 0.5rem;
  }
  .form {
    border-top: 1px dashed var(--border);
    padding-top: 0.7rem;
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }
  .form input,
  .form select {
    width: 100%;
    font-size: var(--fs-ui);
    padding: 0.3rem 0.45rem;
    box-sizing: border-box;
  }
  .keyrow {
    display: flex;
    gap: 0.8rem;
    font-size: var(--fs-small);
    color: var(--text-dim);
  }
  .keyrow label {
    display: flex;
    align-items: center;
    gap: 0.25rem;
  }
  .form-actions {
    display: flex;
    gap: 0.4rem;
  }
  .err {
    color: var(--danger);
    font-size: var(--fs-small);
    margin: 0;
    white-space: pre-wrap;
  }
  .hint {
    color: var(--text-dim);
    font-size: var(--fs-small);
    margin: 0;
  }
</style>
