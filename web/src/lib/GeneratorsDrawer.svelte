<script lang="ts" module>
  /** Jump target when opening the drawer from the chips row / menu. */
  export interface GeneratorsPrefill {
    editGeneratorId?: string
    editTemplateId?: string
    newFromTemplateId?: string
    newFromGeneratorId?: string
  }
</script>

<script lang="ts">
  // Templates + per-profile generators manager (docs/generators-api.md),
  // docked as a NON-MODAL bottom drawer: chips row and canvas stay live while
  // editing. ONE edit form (right column) serves generators and (non-builtin)
  // templates; generator fields are nullable = inherited, with the resolved
  // inherited value as placeholder and a clear-to-inherit affordance.
  // Open/closed persists per profile (GenControls owns that setting).
  import { api } from './api'
  import ParamsEditor, {
    paramsFromRows,
    paramsSummary,
    rowsFromParams,
    type ParamRow,
  } from './ParamsEditor.svelte'
  import { askConfirm } from './confirm.svelte'
  import { profile } from './profile.svelte'
  import {
    confirmDeleteGenerator,
    descendantIdsOf,
    directChildrenOf,
    generatorById,
    isGeneratorActive,
    refreshGenerators,
    resolveParentChain,
    session,
    templateById,
    toggleActiveGenerator,
    withToast,
  } from './state.svelte'
  import type { Generator, ParentRef, Template } from './types'

  let {
    onclose,
    prefill = null,
  }: { onclose: () => void; prefill?: GeneratorsPrefill | null } = $props()

  const generators = $derived(session.generators ?? [])
  const templates = $derived(session.templates ?? [])

  function parentLabel(parent: ParentRef | null): string {
    if (parent === null) return 'standalone'
    const name =
      parent.kind === 'template'
        ? (templateById(parent.id)?.name ?? '(missing template)')
        : (generatorById(parent.id)?.name ?? '(missing generator)')
    return `← ${name}`
  }

  // ---- the single edit form --------------------------------------------
  // editing.kind selects PATCH target semantics: generator fields are
  // overrides (empty = inherit), template fields are required + complete.
  let editing = $state<{ kind: 'generator' | 'template'; id: string } | null>(null)
  let fName = $state('')
  let fParentSel = $state('') // '' | 'template:<id>' | 'generator:<id>'
  let fBaseUrl = $state('')
  let fModel = $state('')
  let fApiKeyMode = $state<'keep' | 'set' | 'clear'>('keep')
  let fApiKey = $state('')
  let fApiKeyEnv = $state('')
  let fParams = $state<ParamRow[]>([])
  let fError = $state<string | null>(null)
  // param keys present when the form loaded — removed rows PATCH to null
  let loadedParamKeys: string[] = []

  function parentRefOf(sel: string): ParentRef | null {
    if (sel === '') return null
    const idx = sel.indexOf(':')
    return { kind: sel.slice(0, idx) as ParentRef['kind'], id: sel.slice(idx + 1) }
  }

  // resolved inherited values for the parent CURRENTLY selected in the form —
  // placeholders track the picker live, before saving
  const inherited = $derived(
    editing?.kind === 'generator' ? resolveParentChain(parentRefOf(fParentSel)) : null,
  )

  // ---- endpoint probe (docs/generators-api.md §Endpoint probe) ------------
  // Debounce-probe the EFFECTIVE base_url (typed override or inherited) when
  // endpoint-shaped form fields change: a reachable/unreachable indicator
  // (words + color, no glyphs) and the listed model ids feed the model
  // field's <datalist>. The probe goes through the server (CORS + secrets).
  const probe = $state<{
    status: 'idle' | 'probing' | 'ok' | 'fail'
    error: string | null
    models: string[]
  }>({ status: 'idle', error: null, models: [] })

  let probeTimer: ReturnType<typeof setTimeout> | null = null
  let probeSeq = 0 // stale-response guard: only the latest probe lands

  const effectiveBaseUrl = $derived(
    editing === null
      ? ''
      : fBaseUrl.trim() ||
        (editing.kind === 'generator' ? (inherited?.base_url ?? '') : ''),
  )

  $effect(() => {
    const url = effectiveBaseUrl
    const target = editing
    // 'keep' mode can't replay the stored key (server redacts it to "***") —
    // probe BY ID so the server resolves the stored/inherited credentials;
    // the current URL field still wins via the explicit base_url override.
    // 'set'/'clear' probe literally with what the form can honestly send.
    const byId = fApiKeyMode === 'keep' && target !== null
    const key = fApiKeyMode === 'set' ? fApiKey.trim() : ''
    const env = fApiKeyEnv.trim()
    if (probeTimer !== null) clearTimeout(probeTimer)
    const seq = ++probeSeq
    if (!url || target === null) {
      probe.status = 'idle'
      probe.error = null
      probe.models = []
      return
    }
    probe.status = 'probing'
    probeTimer = setTimeout(async () => {
      probeTimer = null
      try {
        const res = await api.probeEndpoint(
          byId
            ? target.kind === 'template'
              ? { template_id: target.id, base_url: url }
              : { generator_id: target.id, base_url: url }
            : {
                base_url: url,
                ...(key ? { api_key: key } : {}),
                ...(!key && env ? { api_key_env: env } : {}),
              },
        )
        if (seq !== probeSeq) return
        probe.status = res.ok ? 'ok' : 'fail'
        probe.error = res.error
        probe.models = res.models
      } catch (e) {
        if (seq !== probeSeq) return
        probe.status = 'fail'
        probe.error = e instanceof Error ? e.message : `${e}`
        probe.models = []
      }
    }, 500)
  })

  function resetForm() {
    editing = null
    fName = ''
    fParentSel = ''
    fBaseUrl = ''
    fModel = ''
    fApiKeyMode = 'keep'
    fApiKey = ''
    fApiKeyEnv = ''
    fParams = []
    fError = null
    loadedParamKeys = []
  }

  function editGenerator(g: Generator) {
    editing = { kind: 'generator', id: g.id }
    fName = g.name
    fParentSel = g.parent === null ? '' : `${g.parent.kind}:${g.parent.id}`
    fBaseUrl = g.base_url ?? ''
    fModel = g.model ?? ''
    // server redacts api_key to "***" — can't show it; keep = omit on PATCH
    fApiKeyMode = 'keep'
    fApiKey = ''
    fApiKeyEnv = g.api_key_env ?? ''
    fParams = rowsFromParams(g.params)
    loadedParamKeys = Object.keys(g.params)
    fError = null
  }

  function editTemplate(t: Template) {
    if (t.builtin) return // read-only: imported from coloom.yaml
    editing = { kind: 'template', id: t.id }
    fName = t.name
    fParentSel = ''
    fBaseUrl = t.base_url
    fModel = t.model
    fApiKeyMode = 'keep'
    fApiKey = ''
    fApiKeyEnv = t.api_key_env ?? ''
    fParams = rowsFromParams(t.params)
    loadedParamKeys = Object.keys(t.params)
    fError = null
  }

  /** Generator parent picker options: all templates + my generators, minus
   * the edited generator and its descendants (cycle prevention). */
  const parentOptions = $derived.by(() => {
    const excluded =
      editing?.kind === 'generator' ? descendantIdsOf(editing.id) : new Set<string>()
    return {
      templates,
      generators: generators.filter((g) => !excluded.has(g.id)),
    }
  })

  async function saveForm() {
    fError = null
    let params: Record<string, unknown>
    try {
      params = paramsFromRows(fParams)
    } catch (e) {
      fError = e instanceof Error ? e.message : `${e}`
      return
    }
    if (!fName.trim()) {
      fError = 'name is required'
      return
    }
    if (editing === null) return
    const env = fApiKeyEnv.trim()
    try {
      if (editing.kind === 'generator') {
        // params merge per-key server-side: removed keys clear via null
        const patchParams: Record<string, unknown | null> = { ...params }
        for (const k of loadedParamKeys) {
          if (!(k in params)) patchParams[k] = null
        }
        const fields: Parameters<typeof api.updateGenerator>[1] = {
          name: fName.trim(),
          parent: parentRefOf(fParentSel),
          base_url: fBaseUrl.trim() || null, // empty = inherit
          model: fModel.trim() || null,
          api_key_env: env || null,
          params: patchParams,
        }
        if (fApiKeyMode === 'set') fields.api_key = fApiKey
        else if (fApiKeyMode === 'clear') fields.api_key = null
        await api.updateGenerator(editing.id, fields)
      } else {
        // templates are complete definitions: required fields; params PATCH
        // merges per-key on templates too — removed rows clear via null
        if (!fBaseUrl.trim()) {
          fError = 'base_url is required on a template'
          return
        }
        if (!fModel.trim()) {
          fError = 'model is required on a template'
          return
        }
        const patchParams: Record<string, unknown | null> = { ...params }
        for (const k of loadedParamKeys) {
          if (!(k in params)) patchParams[k] = null
        }
        const fields: Parameters<typeof api.updateTemplate>[1] = {
          name: fName.trim(),
          base_url: fBaseUrl.trim(),
          model: fModel.trim(),
          api_key_env: env || null,
          params: patchParams,
        }
        if (fApiKeyMode === 'set') fields.api_key = fApiKey
        else if (fApiKeyMode === 'clear') fields.api_key = null
        await api.updateTemplate(editing.id, fields)
      }
      await refreshGenerators()
      resetForm()
    } catch (e) {
      fError = e instanceof Error ? e.message : `${e}`
    }
  }

  // ---- create flow -------------------------------------------------------
  // from scratch / from template / from existing generator × inherit/duplicate
  let createSource = $state('') // '' = scratch | 'template:<id>' | 'generator:<id>'
  let createMode = $state<'inherit' | 'duplicate'>('inherit')
  let createName = $state('')
  let createError = $state<string | null>(null)

  async function createGenerator() {
    createError = null
    const name = createName.trim()
    const profileName = profile.name
    if (profileName === null) return
    try {
      let created: Generator
      const from = parentRefOf(createSource)
      if (from === null) {
        if (!name) {
          createError = 'name is required'
          return
        }
        created = await api.createGenerator({ profile: profileName, name })
      } else {
        created = await api.createGeneratorFrom(
          from,
          createMode,
          profileName,
          name || undefined,
        )
      }
      await refreshGenerators()
      createName = ''
      // jump straight into editing the new generator (scratch ones need
      // base_url + model before they can weave)
      const g = generatorById(created.id)
      if (g) editGenerator(g)
    } catch (e) {
      createError = e instanceof Error ? e.message : `${e}`
    }
  }

  function newGeneratorFrom(kind: ParentRef['kind'], id: string) {
    createSource = `${kind}:${id}`
    createMode = 'inherit'
    createError = null
    document
      .querySelector('[data-testid="create-panel"]')
      ?.scrollIntoView({ block: 'nearest' })
  }

  // ---- row actions --------------------------------------------------------

  async function promoteGenerator(g: Generator) {
    await withToast(async () => {
      await api.promoteGenerator(g.id)
      await refreshGenerators()
    })
  }

  async function deleteGenerator(g: Generator, e?: MouseEvent) {
    // shift+click = power-user skip of the in-app confirm
    const deleted = await confirmDeleteGenerator(g, { skipConfirm: e?.shiftKey })
    if (deleted && editing?.kind === 'generator' && editing.id === g.id) resetForm()
  }

  async function deleteTemplate(t: Template, e?: MouseEvent) {
    if (!e?.shiftKey) {
      const kids = directChildrenOf('template', t.id)
      const warn =
        kids.length > 0
          ? `${kids.length} generator${kids.length > 1 ? 's' : ''} inherit from it (${kids.map((k) => k.name).join(', ')}) — they will be FLATTENED: the template's values get materialized into them.`
          : ''
      const ok = await askConfirm({
        title: `Delete template "${t.name}"?`,
        body: warn,
        confirmLabel: 'delete',
        danger: true,
      })
      if (!ok) return
    }
    await withToast(async () => {
      await api.deleteTemplate(t.id)
      await refreshGenerators()
      if (editing?.kind === 'template' && editing.id === t.id) resetForm()
    })
  }

  async function duplicateGenerator(g: Generator) {
    const profileName = profile.name
    if (profileName === null) return
    await withToast(async () => {
      const created = await api.createGeneratorFrom(
        { kind: 'generator', id: g.id },
        'duplicate',
        profileName,
        `${g.name} copy`,
      )
      await refreshGenerators()
      const fresh = generatorById(created.id)
      if (fresh) editGenerator(fresh)
    })
  }

  // refetch on open so we never edit stale state
  $effect(() => {
    void refreshGenerators()
  })

  // apply the prefill once data is loaded (edit jump / new-from-template)
  let prefillApplied = false
  $effect(() => {
    if (prefillApplied || session.generators === null || !prefill) return
    prefillApplied = true
    if (prefill.editGeneratorId) {
      const g = generatorById(prefill.editGeneratorId)
      if (g) editGenerator(g)
    }
    if (prefill.editTemplateId) {
      const t = templateById(prefill.editTemplateId)
      if (t) editTemplate(t)
    }
    if (prefill.newFromTemplateId) newGeneratorFrom('template', prefill.newFromTemplateId)
    if (prefill.newFromGeneratorId) newGeneratorFrom('generator', prefill.newFromGeneratorId)
  })

  function onkeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') onclose()
  }
</script>

<svelte:window {onkeydown} />

<div class="drawer" role="dialog" aria-label="generators" data-testid="generators-drawer">
  <header>
    <h2>generators</h2>
    <span class="drawer-hint">non-modal — the weave stays live behind</span>
    <button class="collapse" onclick={onclose} data-testid="generators-collapse"
      >collapse ▾</button
    >
  </header>

  <div class="cols">
    <!-- lists: my generators, then templates -->
    <section class="col">
      <h3>my generators</h3>
      <ul class="list" data-testid="generator-list">
        {#each generators as g (g.id)}
          <li class:editing={editing?.kind === 'generator' && editing.id === g.id} class:active={isGeneratorActive(g.id)}>
            <div class="row-main">
              <label class="activate" title="activate for fan-out generation">
                <input
                  type="checkbox"
                  checked={isGeneratorActive(g.id)}
                  onchange={() => toggleActiveGenerator(g.id)}
                  data-testid={`g-active-${g.id}`}
                />
                <span class="nm">{g.name}</span>
              </label>
              <span class="meta">{parentLabel(g.parent)}</span>
            </div>
            <div class="row-sub">
              <span class="params" title={paramsSummary(g.resolved.params)}>
                {paramsSummary(g.params) || 'no overrides'}
              </span>
              {#if g.api_key === '***'}<span class="key" title="api_key set">[key]</span>{/if}
              {#if g.api_key_env}<span class="key" title="from env">${g.api_key_env}</span>{/if}
            </div>
            <div class="actions">
              <button onclick={() => editGenerator(g)} data-testid={`g-edit-${g.id}`}>edit</button>
              <button title="literal copy: same parent, same overrides" onclick={() => duplicateGenerator(g)}>duplicate</button>
              <button
                title="materialize the resolved fields into a new server-global template"
                onclick={() => promoteGenerator(g)}
                data-testid={`g-promote-${g.id}`}>promote to template</button
              >
              <button
                class="danger"
                title="delete (shift+click skips the confirm)"
                onclick={(e) => deleteGenerator(g, e)}
                data-testid={`g-delete-${g.id}`}>delete</button
              >
            </div>
          </li>
        {:else}
          <li class="empty">no generators yet — create one below</li>
        {/each}
      </ul>

      <div class="create" data-testid="create-panel">
        <h4>new generator</h4>
        <div class="create-row">
          <select bind:value={createSource} data-testid="create-source">
            <option value="">from scratch</option>
            <optgroup label="from template">
              {#each templates as t (t.id)}
                <option value={`template:${t.id}`}>{t.name}</option>
              {/each}
            </optgroup>
            <optgroup label="from generator">
              {#each generators as g (g.id)}
                <option value={`generator:${g.id}`}>{g.name}</option>
              {/each}
            </optgroup>
          </select>
          {#if createSource !== ''}
            <label title="inherit: follow the source's future changes (empty overrides)">
              <input type="radio" value="inherit" bind:group={createMode} data-testid="create-mode-inherit" /> inherit
            </label>
            <label title="duplicate: copy the values, no link to the source">
              <input type="radio" value="duplicate" bind:group={createMode} data-testid="create-mode-duplicate" /> duplicate
            </label>
          {/if}
        </div>
        <div class="create-row">
          <input
            placeholder={createSource === '' ? 'name' : 'name (default: source name)'}
            bind:value={createName}
            data-testid="create-name"
          />
          <button class="primary" onclick={createGenerator} data-testid="create-generator">create</button>
        </div>
        {#if createError}<p class="err" data-testid="create-error">{createError}</p>{/if}
      </div>

      <h3 class="tpl-head">templates <span class="hint-inline">(server-global)</span></h3>
      <ul class="list" data-testid="template-list">
        {#each templates as t (t.id)}
          <li class:editing={editing?.kind === 'template' && editing.id === t.id}>
            <div class="row-main">
              <span class="nm">{t.name}</span>
              {#if t.builtin}<span class="builtin" title="imported from coloom.yaml — read-only">builtin</span>{/if}
              <span class="meta">{t.model}</span>
            </div>
            <div class="row-sub">
              <span class="url">{t.base_url}</span>
              {#if t.api_key === '***'}<span class="key" title="api_key set">[key]</span>{/if}
              {#if t.api_key_env}<span class="key" title="from env">${t.api_key_env}</span>{/if}
            </div>
            <div class="actions">
              <button
                title="create a generator inheriting from this template"
                onclick={() => newGeneratorFrom('template', t.id)}
                data-testid={`t-new-gen-${t.id}`}>new generator from this</button
              >
              {#if !t.builtin}
                <button onclick={() => editTemplate(t)} data-testid={`t-edit-${t.id}`}>edit</button>
                <button
                  class="danger"
                  title="delete (shift+click skips the confirm)"
                  onclick={(e) => deleteTemplate(t, e)}
                  data-testid={`t-delete-${t.id}`}>delete</button
                >
              {/if}
            </div>
          </li>
        {:else}
          <li class="empty">no templates (none in coloom.yaml?)</li>
        {/each}
      </ul>
    </section>

    <!-- the single edit form -->
    <section class="col">
      {#if editing === null}
        <h3>edit</h3>
        <p class="hint">pick a generator or template on the left — or create a new generator.</p>
      {:else}
        <h3>
          {editing.kind === 'generator' ? 'edit generator' : 'edit template'}
        </h3>
        <div class="form" data-testid="edit-form">
          <label class="field">
            <span class="fl">name</span>
            <input bind:value={fName} data-testid="f-name" />
          </label>

          {#if editing.kind === 'generator'}
            <label class="field">
              <span class="fl">parent <span class="fl-hint">(inherits everything not set below)</span></span>
              <select bind:value={fParentSel} data-testid="f-parent">
                <option value="">none — standalone</option>
                <optgroup label="templates">
                  {#each parentOptions.templates as t (t.id)}
                    <option value={`template:${t.id}`}>{t.name}</option>
                  {/each}
                </optgroup>
                <optgroup label="my generators">
                  {#each parentOptions.generators as g (g.id)}
                    <option value={`generator:${g.id}`}>{g.name}</option>
                  {/each}
                </optgroup>
              </select>
            </label>
          {/if}

          <label class="field">
            <span class="fl">
              base_url
              {#if editing.kind === 'generator' && fBaseUrl !== ''}
                <button class="clear" onclick={() => (fBaseUrl = '')} data-testid="f-clear-base-url"
                  title="clear the override — fall back to the inherited value">clear to inherit</button>
              {/if}
            </span>
            <input
              bind:value={fBaseUrl}
              placeholder={editing.kind === 'generator'
                ? (inherited?.base_url ?? 'no inherited value — set one')
                : 'base_url (e.g. http://localhost:9999/v1)'}
              data-testid="f-base-url"
            />
          </label>
          {#if probe.status !== 'idle'}
            <p
              class="probe"
              class:ok={probe.status === 'ok'}
              class:fail={probe.status === 'fail'}
              data-testid="f-probe"
            >
              {#if probe.status === 'probing'}
                probing endpoint…
              {:else if probe.status === 'ok'}
                endpoint reachable{probe.models.length > 0
                  ? ` — ${probe.models.length} model${probe.models.length > 1 ? 's' : ''} listed`
                  : ''}
              {:else}
                unreachable: {probe.error ?? 'unknown error'}
              {/if}
            </p>
          {/if}

          <label class="field">
            <span class="fl">
              model
              {#if editing.kind === 'generator' && fModel !== ''}
                <button class="clear" onclick={() => (fModel = '')} data-testid="f-clear-model"
                  title="clear the override — fall back to the inherited value">clear to inherit</button>
              {/if}
            </span>
            <datalist id="f-model-suggestions">
              {#each probe.models as m (m)}
                <option value={m}></option>
              {/each}
            </datalist>
            <input
              bind:value={fModel}
              list="f-model-suggestions"
              placeholder={editing.kind === 'generator'
                ? (inherited?.model ?? 'no inherited value — set one')
                : 'model (e.g. gpt-fake)'}
              data-testid="f-model"
            />
          </label>

          <div class="field">
            <span class="fl">api_key</span>
            <div class="keyrow">
              <label><input type="radio" value="keep" bind:group={fApiKeyMode} /> keep</label>
              <label><input type="radio" value="set" bind:group={fApiKeyMode} /> set</label>
              <label
                title={editing.kind === 'generator'
                  ? 'remove the override — fall back to the inherited key'
                  : 'remove the stored key'}
                ><input type="radio" value="clear" bind:group={fApiKeyMode} />
                {editing.kind === 'generator' ? 'clear to inherit' : 'clear'}</label
              >
            </div>
            {#if fApiKeyMode === 'set'}
              <input
                placeholder="api_key (stored literal, never echoed)"
                bind:value={fApiKey}
                data-testid="f-api-key"
              />
            {/if}
          </div>

          <label class="field">
            <span class="fl">api_key_env</span>
            <input
              bind:value={fApiKeyEnv}
              placeholder={editing.kind === 'generator' && inherited?.api_key_env
                ? inherited.api_key_env
                : 'env var name (mutually exclusive with api_key)'}
              data-testid="f-api-key-env"
            />
          </label>

          <div class="field">
            <span class="fl">
              params
              {#if editing.kind === 'generator'}
                <span class="fl-hint">(own overrides — a key set here wins over the inherited one)</span>
              {/if}
            </span>
            {#if editing.kind === 'generator' && inherited && Object.keys(inherited.params).length > 0}
              <p class="inherited-params" data-testid="f-inherited-params">
                inherited: {paramsSummary(inherited.params)}
              </p>
            {/if}
            <ParamsEditor bind:rows={fParams} testid="f-params" />
          </div>

          {#if fError}<p class="err" data-testid="f-error">{fError}</p>{/if}
          <div class="form-actions">
            <button class="primary" onclick={saveForm} data-testid="f-save">save</button>
            <button onclick={resetForm} data-testid="f-cancel">cancel</button>
          </div>
        </div>
      {/if}
    </section>
  </div>
</div>

<style>
  .drawer {
    /* docked, non-modal: no backdrop — the editor stays interactive above */
    position: fixed;
    left: 0;
    right: 0;
    bottom: 0;
    height: min(44vh, 30rem);
    background: var(--bg-raised);
    border-top: 1px solid var(--border);
    box-shadow: 0 -8px 24px rgba(0, 0, 0, 0.45);
    display: flex;
    flex-direction: column;
    overflow: hidden;
    z-index: 40; /* above pane content, below dropdown menus (50) and dialogs (100) */
  }
  header {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.45rem 0.9rem;
    border-bottom: 1px solid var(--border);
  }
  header h2 {
    margin: 0;
    font-size: var(--fs-ui);
    font-weight: 600;
  }
  .drawer-hint {
    color: var(--text-dim);
    font-size: var(--fs-tiny);
    margin-right: auto;
  }
  .collapse {
    font-size: var(--fs-small);
    padding: 0.15rem 0.55rem;
    color: var(--text-dim);
  }
  .cols {
    display: flex;
    gap: 1.2rem;
    padding: 0.9rem;
    overflow: auto;
    flex: 1;
    min-height: 0;
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
  h3.tpl-head {
    margin-top: 1rem;
  }
  .hint-inline {
    text-transform: none;
    letter-spacing: 0;
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
  .builtin {
    font-size: var(--fs-tiny);
    color: var(--text-dim);
    border: 1px solid var(--border);
    border-radius: 3px;
    padding: 0 0.3rem;
  }
  .meta {
    color: var(--text-dim);
    font-size: var(--fs-small);
    margin-left: auto;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
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
    min-width: 0;
  }
  .actions {
    display: flex;
    gap: 0.35rem;
    margin-top: 0.4rem;
    flex-wrap: wrap;
  }
  .actions button {
    font-size: var(--fs-tiny);
    padding: 0.15rem 0.5rem;
  }
  .actions button.danger:hover {
    color: var(--danger);
  }
  .create {
    border: 1px dashed var(--border);
    border-radius: 5px;
    padding: 0.5rem 0.55rem;
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
  }
  .create-row {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    font-size: var(--fs-small);
  }
  .create-row select {
    min-width: 0;
    flex: 1;
  }
  .create-row input {
    flex: 1;
    min-width: 0;
  }
  .create-row label {
    display: flex;
    align-items: center;
    gap: 0.25rem;
    color: var(--text-dim);
    white-space: nowrap;
  }
  .create-row label input {
    flex: none;
    width: auto;
  }
  .form {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }
  .field {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }
  .fl {
    font-size: var(--fs-tiny);
    color: var(--text-dim);
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }
  .fl-hint {
    opacity: 0.8;
  }
  .clear {
    font-size: var(--fs-tiny);
    padding: 0 0.35rem;
    background: none;
    border: 1px solid var(--border);
    color: var(--text-dim);
  }
  .clear:hover {
    color: var(--text);
  }
  .form input,
  .form select,
  .create input,
  .create select {
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
  .keyrow input {
    width: auto;
  }
  .inherited-params {
    margin: 0;
    font-size: var(--fs-tiny);
    color: var(--text-dim);
    font-family: var(--mono, monospace);
  }
  .probe {
    margin: 0;
    font-size: var(--fs-tiny);
    color: var(--text-dim);
  }
  .probe.ok {
    color: var(--ok, #4a4);
  }
  .probe.fail {
    color: var(--danger);
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
  .key {
    flex-shrink: 0;
  }
</style>
