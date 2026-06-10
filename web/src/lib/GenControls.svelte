<script lang="ts">
  // Generation controls: one row of GENERATOR chips (sampler setups + presets,
  // click to toggle, several active at once → weave fans out per generator),
  // a manage menu (activate / hide / edit / duplicate / delete), param
  // overrides, and the weave button.
  import { paramsSummary } from './ParamsEditor.svelte'
  import SetupsDialog, { type SetupsPrefill } from './SetupsDialog.svelte'
  import {
    activeGenerators,
    generateAt,
    type GeneratorRef,
    generatorKey,
    isGeneratorActive,
    isGeneratorHidden,
    myCursorNodeId,
    session,
    toggleActiveGenerator,
    toggleHiddenGenerator,
    validActiveGenerators,
    visibleGenerators,
    withToast,
  } from './state.svelte'
  import { api } from './api'
  import { refreshSetups } from './state.svelte'

  const presets = $derived(session.presets?.presets ?? {})
  const samplers = $derived(session.setups?.samplers ?? [])
  const models = $derived(session.setups?.models ?? [])
  const visible = $derived(visibleGenerators())
  const activeCount = $derived(
    // depend on the list so chips re-render on toggle
    activeGenerators.list.length >= 0 ? validActiveGenerators().length : 0,
  )

  function modelName(id: string): string {
    return models.find((m) => m.id === id)?.name ?? '(missing model)'
  }

  function chipTitle(ref: GeneratorRef): string {
    if (ref.kind === 'sampler') {
      const s = samplers.find((x) => x.id === ref.id)
      if (!s) return ''
      return `sampler → ${modelName(s.model_setup_id)}  ${paramsSummary(s.params)}`
    }
    const p = presets[ref.id]
    return p ? `preset · ${p.model}  ${paramsSummary(p.params)}` : ''
  }

  // numeric overrides surfaced in the UI; anything else rides on the generator
  let temperature = $state<number | null>(null)
  let maxTokens = $state<number | null>(null)
  let n = $state<number | null>(null)

  let showSetups = $state(false)
  let setupsPrefill = $state<SetupsPrefill | null>(null)
  let menuOpen = $state(false)

  function openSetups(prefill: SetupsPrefill | null = null) {
    setupsPrefill = prefill
    showSetups = true
    menuOpen = false
  }

  function syncOverrides() {
    const o: Record<string, unknown> = {}
    if (temperature != null) o.temperature = temperature
    if (maxTokens != null) o.max_tokens = maxTokens
    if (n != null) o.n = n
    session.paramOverrides = o
  }

  function placeholderParam(key: string): string {
    const first = validActiveGenerators()[0]
    const params =
      first?.kind === 'sampler'
        ? samplers.find((s) => s.id === first.id)?.params
        : first
          ? presets[first.id]?.params
          : session.selectedPreset
            ? presets[session.selectedPreset]?.params
            : undefined
    const v = params?.[key]
    return v === undefined ? '' : `${v}`
  }

  async function genAtCursor() {
    const nodeId = myCursorNodeId()
    if (nodeId) await generateAt(nodeId)
  }

  async function deleteSampler(id: string) {
    await withToast(async () => {
      await api.deleteSamplerSetup(id)
      await refreshSetups()
    })
  }

  /** "use this preset as a starting point": prefill an editable model setup. */
  function cloneVal(ref: GeneratorRef): SetupsPrefill {
    if (ref.kind === 'preset') {
      const p = presets[ref.id]
      return {
        model: {
          name: ref.id,
          base_url: p?.base_url ?? '',
          model: p?.model ?? '',
          api_key_env: p?.api_key_env ?? null,
          params: p?.params ?? {},
        },
      }
    }
    const s = samplers.find((x) => x.id === ref.id)
    return {
      sampler: {
        name: s ? `${s.name} copy` : '',
        model_setup_id: s?.model_setup_id,
        params: s?.params ?? {},
      },
    }
  }

  function closeMenuOnOutside(e: MouseEvent) {
    if (!(e.target instanceof Element) || !e.target.closest('.gen-menu-root')) {
      menuOpen = false
    }
  }
</script>

<svelte:window
  onclick={menuOpen ? closeMenuOnOutside : undefined}
  onkeydown={(e) => {
    if (menuOpen && e.key === 'Escape') menuOpen = false
  }}
/>

<div class="controls">
  <div class="generators">
    {#each visible as g, i (generatorKey(g.ref))}
      <button
        class="chip"
        class:active={isGeneratorActive(g.ref)}
        class:sampler={g.ref.kind === 'sampler'}
        onclick={() => toggleActiveGenerator(g.ref)}
        title={`${i < 9 ? `[${i + 1}] ` : ''}${chipTitle(g.ref)}`}
        data-testid={g.ref.kind === 'sampler' ? `gc-sampler-${g.ref.id}` : `gc-preset-${g.ref.id}`}
      >
        {g.label}
      </button>
    {:else}
      <span class="hint">all generators hidden — unhide some ↓</span>
    {/each}
    <span class="gen-menu-root">
      <button
        class="menu-btn"
        onclick={() => (menuOpen = !menuOpen)}
        data-testid="open-gen-menu">generators ▾</button
      >
      {#if menuOpen}
        <div class="menu" data-testid="gen-menu">
          {#if samplers.length > 0}
            <div class="menu-head">sampler setups</div>
            {#each samplers as s (s.id)}
              {@const ref = { kind: 'sampler', id: s.id } as GeneratorRef}
              <div class="menu-row" class:hidden-row={isGeneratorHidden(ref)}>
                <label class="mr-act">
                  <input
                    type="checkbox"
                    checked={isGeneratorActive(ref)}
                    onchange={() => toggleActiveGenerator(ref)}
                    data-testid={`menu-active-sampler-${s.id}`}
                  />
                  <span class="mr-name">{s.name}</span>
                </label>
                <span class="mr-meta">→ {modelName(s.model_setup_id)}</span>
                <span class="mr-actions">
                  <button
                    title={isGeneratorHidden(ref) ? 'show chip' : 'hide chip'}
                    onclick={() => toggleHiddenGenerator(ref)}
                    data-testid={`menu-hide-sampler-${s.id}`}
                    >{isGeneratorHidden(ref) ? 'show' : 'hide'}</button
                  >
                  <button
                    title="edit"
                    onclick={() => openSetups({ editSamplerId: s.id })}>edit</button
                  >
                  <button title="duplicate then edit" onclick={() => openSetups(cloneVal(ref))}
                    >dup</button
                  >
                  <button
                    class="danger"
                    title="delete sampler"
                    onclick={() => deleteSampler(s.id)}
                    data-testid={`menu-delete-sampler-${s.id}`}>✕</button
                  >
                </span>
              </div>
            {/each}
          {/if}
          <div class="menu-head">presets (read-only)</div>
          {#each Object.keys(presets) as name (name)}
            {@const ref = { kind: 'preset', id: name } as GeneratorRef}
            <div class="menu-row" class:hidden-row={isGeneratorHidden(ref)}>
              <label class="mr-act">
                <input
                  type="checkbox"
                  checked={isGeneratorActive(ref)}
                  onchange={() => toggleActiveGenerator(ref)}
                  data-testid={`menu-active-preset-${name}`}
                />
                <span class="mr-name">{name}</span>
              </label>
              <span class="mr-meta">{presets[name].model}</span>
              <span class="mr-actions">
                <button
                  title={isGeneratorHidden(ref) ? 'show chip' : 'hide chip'}
                  onclick={() => toggleHiddenGenerator(ref)}
                  data-testid={`menu-hide-preset-${name}`}
                  >{isGeneratorHidden(ref) ? 'show' : 'hide'}</button
                >
                <button
                  title="clone into an editable model setup"
                  onclick={() => openSetups(cloneVal(ref))}
                  data-testid={`menu-clone-preset-${name}`}>→ setup</button
                >
              </span>
            </div>
          {/each}
        </div>
      {/if}
    </span>
    <button class="setups-btn" onclick={() => openSetups()} data-testid="open-setups">
      ⚙ setups
    </button>
  </div>
  <div class="params">
    <label>
      temp
      <input
        type="number"
        step="0.1"
        min="0"
        placeholder={placeholderParam('temperature')}
        bind:value={temperature}
        onchange={syncOverrides}
      />
    </label>
    <label>
      max_tokens
      <input
        type="number"
        step="8"
        min="1"
        placeholder={placeholderParam('max_tokens')}
        bind:value={maxTokens}
        onchange={syncOverrides}
      />
    </label>
    <label>
      n
      <input
        type="number"
        step="1"
        min="1"
        placeholder={placeholderParam('n')}
        bind:value={n}
        onchange={syncOverrides}
      />
    </label>
    <button
      class="primary gen"
      onclick={genAtCursor}
      disabled={!myCursorNodeId()}
      title={activeCount > 0
        ? `fan out ${activeCount} generator${activeCount > 1 ? 's' : ''} at my cursor`
        : `generate continuations at my cursor (fallback preset: ${session.selectedPreset ?? '—'})`}
      data-testid="gen-button"
    >
      {#if activeCount > 1}
        weave ×{activeCount} ⟶
      {:else}
        weave ⟶
      {/if}
    </button>
  </div>
</div>

{#if showSetups}
  <SetupsDialog
    prefill={setupsPrefill}
    onclose={() => {
      showSetups = false
      setupsPrefill = null
    }}
  />
{/if}

<style>
  .controls {
    border-bottom: 1px solid var(--border);
    padding: 0.6rem 0.7rem;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    background: var(--bg-raised);
  }
  .generators {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
    align-items: center;
  }
  .chip {
    font-size: var(--fs-small);
    padding: 0.18rem 0.55rem;
  }
  .chip.active {
    border-color: var(--accent);
    color: var(--accent);
    box-shadow: inset 0 -2px 0 var(--accent);
  }
  .chip.sampler.active {
    background: color-mix(in oklab, var(--accent) 14%, var(--bg-card));
  }
  .hint {
    color: var(--text-dim);
    font-size: var(--fs-small);
  }
  .menu-btn,
  .setups-btn {
    font-size: var(--fs-small);
    padding: 0.18rem 0.55rem;
    color: var(--text-dim);
  }
  .menu-btn {
    margin-left: auto;
  }
  .gen-menu-root {
    position: relative;
    margin-left: auto;
    display: inline-flex;
  }
  .gen-menu-root .menu-btn {
    margin-left: 0;
  }
  .menu {
    position: absolute;
    top: calc(100% + 4px);
    right: 0;
    z-index: 50;
    min-width: 22rem;
    max-height: 60vh;
    overflow-y: auto;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.4rem;
    box-shadow: 0 6px 24px rgba(0, 0, 0, 0.45);
  }
  .menu-head {
    font-size: var(--fs-tiny);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-dim);
    padding: 0.3rem 0.3rem 0.15rem;
  }
  .menu-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.2rem 0.3rem;
    border-radius: 4px;
    font-size: var(--fs-small);
  }
  .menu-row:hover {
    background: var(--bg-raised);
  }
  .menu-row.hidden-row .mr-name,
  .menu-row.hidden-row .mr-meta {
    opacity: 0.45;
  }
  .mr-act {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    cursor: pointer;
    min-width: 0;
  }
  .mr-name {
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .mr-meta {
    color: var(--text-dim);
    font-size: var(--fs-tiny);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    flex: 1;
  }
  .mr-actions {
    display: inline-flex;
    gap: 0.25rem;
    flex-shrink: 0;
  }
  .mr-actions button {
    font-size: var(--fs-tiny);
    padding: 0.1rem 0.35rem;
    background: none;
    border-color: transparent;
    color: var(--text-dim);
  }
  .mr-actions button:hover {
    border-color: var(--border);
    color: var(--text);
  }
  .mr-actions button.danger:hover {
    color: var(--danger);
  }
  .params {
    display: flex;
    gap: 0.6rem;
    align-items: flex-end;
    flex-wrap: wrap;
  }
  .params label {
    display: flex;
    flex-direction: column;
    font-size: var(--fs-tiny);
    color: var(--text-dim);
    gap: 0.15rem;
  }
  .params input {
    width: 4.6rem;
    font-size: var(--fs-ui);
    padding: 0.2rem 0.4rem;
  }
  .gen {
    margin-left: auto;
  }
</style>
