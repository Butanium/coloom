<script lang="ts">
  // Generation controls: one row of GENERATOR chips (per-profile, inheriting
  // from templates — docs/generators-api.md). Chip body click = FOCUS (the
  // quick param row binds to the focused generator); the small leading dot
  // toggles ACTIVE (generation fans out one request per active generator).
  // A chip whose ancestor was changed by someone else shows a stale tint
  // until focused. Plus the manage menu, the quick row, and the weave button.
  import { paramsSummary } from './ParamsEditor.svelte'
  import GeneratorsDrawer, { type GeneratorsPrefill } from './GeneratorsDrawer.svelte'
  import { api } from './api'
  import { dragnum } from './dragnum'
  import { getSetting, setSetting } from './profile.svelte'
  import {
    confirmDeleteGenerator,
    focusGenerator,
    focusedGenerator,
    generateAt,
    generatorUi,
    inheritedView,
    isGeneratorActive,
    isGeneratorHidden,
    myCursorNodeId,
    refreshGenerators,
    session,
    toggleActiveGenerator,
    toggleHiddenGenerator,
    validActiveGenerators,
    visibleGenerators,
    withToast,
  } from './state.svelte'
  import type { Generator } from './types'

  const allGenerators = $derived(session.generators ?? [])
  const visible = $derived(visibleGenerators())
  const focused = $derived(focusedGenerator())
  const activeCount = $derived(validActiveGenerators().length)

  function chipTitle(g: Generator, i: number): string {
    const stale = generatorUi.stale[g.id]
    const staleLine = stale ? `\n${stale.kind} ${stale.name} changed by ${stale.by}` : ''
    const unusable = g.usable ? '' : '\nunusable — set base_url + model in the drawer'
    const digit = i < 9 ? `[${i + 1}] ` : ''
    return `${digit}${g.resolved.model ?? '(no model)'}  ${paramsSummary(g.resolved.params)}${unusable}${staleLine}`
  }

  // drawer open/closed roams with the profile (login happens before the editor mounts)
  let showDrawer = $state(getSetting<boolean>('generatorsDrawerOpen', false) === true)
  let drawerPrefill = $state<GeneratorsPrefill | null>(null)
  let menuOpen = $state(false)

  function setDrawer(open: boolean) {
    showDrawer = open
    if (!open) drawerPrefill = null
    setSetting('generatorsDrawerOpen', open)
  }

  function openDrawer(prefill: GeneratorsPrefill | null = null) {
    drawerPrefill = prefill // new object identity → {#key} remounts the drawer
    setDrawer(true)
    menuOpen = false
  }

  // ---- quick row: temp / max_tokens / n EDIT THE FOCUSED GENERATOR --------
  // Values shown = the focused generator's own param overrides; placeholders =
  // the resolved inherited value. Edits persist via debounced PATCH; emptying
  // a field clears the override (params key → null).

  const QUICK_KEYS = ['temperature', 'max_tokens', 'n'] as const
  type QuickKey = (typeof QUICK_KEYS)[number]

  const quick = $state<Record<QuickKey, number | null>>({
    temperature: null,
    max_tokens: null,
    n: null,
  })

  // re-seed the inputs whenever focus moves or the generator's data changes
  // (e.g. a PATCH response / remote event refreshed the list)
  $effect(() => {
    for (const key of QUICK_KEYS) {
      const v = focused?.params[key]
      quick[key] = typeof v === 'number' ? v : null
    }
  })

  let patchTimers: Partial<Record<QuickKey, ReturnType<typeof setTimeout>>> = {}

  /** Debounced PATCH of one quick param on the focused generator (~400ms);
   * null clears the override back to inherited. */
  function pushQuickParam(key: QuickKey) {
    const target = focused
    if (!target) return
    const value = quick[key]
    clearTimeout(patchTimers[key])
    patchTimers[key] = setTimeout(() => {
      delete patchTimers[key]
      void withToast(async () => {
        await api.updateGenerator(target.id, { params: { [key]: value } })
        await refreshGenerators()
      })
    }, 400)
  }

  function inheritedQuick(key: QuickKey): string {
    if (!focused) return ''
    const v = inheritedView(focused).params[key]
    return v === undefined || v === null ? '' : `${v}`
  }

  /** drag-seed: the value a drag starts from when no override is set yet */
  function seedParam(key: QuickKey, fallback: number): number {
    const v = Number(inheritedQuick(key))
    return Number.isFinite(v) && inheritedQuick(key) !== '' ? v : fallback
  }

  async function genAtCursor() {
    const nodeId = myCursorNodeId()
    if (nodeId) await generateAt(nodeId)
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

<!-- inline SVG icons: font-independent (no emoji glyphs — tofu on this box) -->
{#snippet eyeIcon(hidden: boolean)}
  <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
    <path
      d="M1.5 8s2.4-4.2 6.5-4.2S14.5 8 14.5 8 12.1 12.2 8 12.2 1.5 8 1.5 8z"
      fill="none"
      stroke="currentColor"
      stroke-width="1.3"
    />
    <circle cx="8" cy="8" r="1.9" fill="currentColor" />
    {#if hidden}
      <line x1="2.5" y1="13.5" x2="13.5" y2="2.5" stroke="currentColor" stroke-width="1.3" />
    {/if}
  </svg>
{/snippet}

{#snippet pencilIcon()}
  <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
    <path
      d="M2.5 13.5l.8-3.2 7.4-7.4 2.4 2.4-7.4 7.4-3.2.8z"
      fill="none"
      stroke="currentColor"
      stroke-width="1.3"
      stroke-linejoin="round"
    />
    <line x1="9.4" y1="4.2" x2="11.8" y2="6.6" stroke="currentColor" stroke-width="1.3" />
  </svg>
{/snippet}

{#snippet trashIcon()}
  <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
    <path d="M2.5 4.5h11" stroke="currentColor" stroke-width="1.3" />
    <path d="M6 4.5V3h4v1.5" fill="none" stroke="currentColor" stroke-width="1.3" />
    <path
      d="M4.5 4.5l.9 9h5.2l.9-9"
      fill="none"
      stroke="currentColor"
      stroke-width="1.3"
      stroke-linejoin="round"
    />
    <path d="M6.7 7v4.5M9.3 7v4.5" stroke="currentColor" stroke-width="1" />
  </svg>
{/snippet}

<div class="controls">
  <div class="generators">
    {#each visible as g, i (g.id)}
      <span
        class="chip"
        class:active={isGeneratorActive(g.id)}
        class:focused={focused?.id === g.id}
        class:stale={generatorUi.stale[g.id] !== undefined}
        class:unusable={!g.usable}
        title={chipTitle(g, i)}
        data-testid={`gc-gen-${g.name}`}
        data-generator-id={g.id}
      >
        <button
          class="dot"
          class:on={isGeneratorActive(g.id)}
          onclick={() => toggleActiveGenerator(g.id)}
          title={isGeneratorActive(g.id) ? 'deactivate (no fan-out)' : 'activate for generation'}
          aria-label={`toggle ${g.name} active`}
          data-testid={`gc-dot-${g.name}`}
        ></button>
        <button
          class="body"
          onclick={() => focusGenerator(g.id)}
          title={generatorUi.stale[g.id]
            ? `${generatorUi.stale[g.id].kind} ${generatorUi.stale[g.id].name} changed by ${generatorUi.stale[g.id].by} — focus to dismiss`
            : `focus: the param row edits ${g.name}`}
          data-testid={`gc-body-${g.name}`}
        >
          {g.name}
        </button>
      </span>
    {:else}
      <span class="hint">
        {allGenerators.length > 0
          ? 'all generators hidden — unhide some from the menu'
          : 'no generators yet — open the drawer to create one'}
      </span>
    {/each}
    <span class="gen-menu-root">
      <button
        class="menu-btn"
        onclick={() => (menuOpen = !menuOpen)}
        data-testid="open-gen-menu">generators ▾</button
      >
      {#if menuOpen}
        <div class="menu" data-testid="gen-menu">
          {#each allGenerators as g (g.id)}
            <div class="menu-row" class:hidden-row={isGeneratorHidden(g.id)}>
              <label class="mr-act">
                <input
                  type="checkbox"
                  checked={isGeneratorActive(g.id)}
                  onchange={() => toggleActiveGenerator(g.id)}
                  data-testid={`menu-active-${g.name}`}
                />
                <span class="mr-name">{g.name}</span>
              </label>
              <span class="mr-meta">{g.resolved.model ?? '(no model)'}</span>
              <span class="mr-actions">
                <button
                  class="icon"
                  title={isGeneratorHidden(g.id) ? 'show chip' : 'hide chip (stays usable, no chip)'}
                  aria-label={isGeneratorHidden(g.id) ? `show ${g.name}` : `hide ${g.name}`}
                  onclick={() => toggleHiddenGenerator(g.id)}
                  data-testid={`menu-hide-${g.name}`}
                >
                  {@render eyeIcon(isGeneratorHidden(g.id))}
                </button>
                <button
                  class="icon"
                  title="edit in the drawer"
                  aria-label={`edit ${g.name}`}
                  onclick={() => openDrawer({ editGeneratorId: g.id })}
                  data-testid={`menu-edit-${g.name}`}
                >
                  {@render pencilIcon()}
                </button>
                <button
                  class="icon danger"
                  title="delete (shift+click skips the confirm)"
                  aria-label={`delete ${g.name}`}
                  onclick={(e) => confirmDeleteGenerator(g, { skipConfirm: e.shiftKey })}
                  data-testid={`menu-delete-${g.name}`}
                >
                  {@render trashIcon()}
                </button>
              </span>
            </div>
          {:else}
            <div class="menu-row"><span class="mr-meta">no generators</span></div>
          {/each}
          <div class="menu-foot">
            <button onclick={() => openDrawer()} data-testid="menu-open-drawer">
              manage generators + templates…
            </button>
          </div>
        </div>
      {/if}
    </span>
    <button
      class="drawer-btn"
      class:open={showDrawer}
      onclick={() => (showDrawer ? setDrawer(false) : openDrawer())}
      title={showDrawer ? 'collapse the generators drawer' : 'open the generators drawer'}
      data-testid="open-generators"
    >
      generators drawer {showDrawer ? '▾' : '▴'}
    </button>
  </div>
  <div class="params">
    <span class="bound" title="the quick row edits the focused generator's params">
      {#if focused}
        editing <b>{focused.name}</b>
      {:else}
        no generator
      {/if}
    </span>
    <label title="drag ↔ to adjust, click to type — empty falls back to the inherited value">
      temp
      <input
        type="number"
        step="0.1"
        min="0"
        placeholder={inheritedQuick('temperature')}
        bind:value={quick.temperature}
        onchange={() => pushQuickParam('temperature')}
        disabled={!focused}
        use:dragnum={{
          speed: 0.01,
          min: 0,
          decimals: 2,
          get: () => quick.temperature,
          set: (v) => ((quick.temperature = v), pushQuickParam('temperature')),
          seed: () => seedParam('temperature', 1),
        }}
        data-testid="param-temp"
      />
    </label>
    <label title="drag ↔ to adjust, click to type — empty falls back to the inherited value">
      max_tokens
      <input
        type="number"
        step="8"
        min="1"
        placeholder={inheritedQuick('max_tokens')}
        bind:value={quick.max_tokens}
        onchange={() => pushQuickParam('max_tokens')}
        disabled={!focused}
        use:dragnum={{
          speed: 1,
          min: 1,
          decimals: 0,
          get: () => quick.max_tokens,
          set: (v) => ((quick.max_tokens = v), pushQuickParam('max_tokens')),
          seed: () => seedParam('max_tokens', 32),
        }}
        data-testid="param-max-tokens"
      />
    </label>
    <label title="drag ↔ to adjust, click to type — empty falls back to the inherited value">
      n
      <input
        type="number"
        step="1"
        min="1"
        placeholder={inheritedQuick('n')}
        bind:value={quick.n}
        onchange={() => pushQuickParam('n')}
        disabled={!focused}
        use:dragnum={{
          speed: 0.05,
          min: 1,
          decimals: 0,
          get: () => quick.n,
          set: (v) => ((quick.n = v), pushQuickParam('n')),
          seed: () => seedParam('n', 1),
        }}
        data-testid="param-n"
      />
    </label>
    <button
      class="primary gen"
      onclick={genAtCursor}
      disabled={!myCursorNodeId() || (activeCount === 0 && !focused)}
      title={activeCount > 0
        ? `fan out ${activeCount} generator${activeCount > 1 ? 's' : ''} at my cursor`
        : focused
          ? `generate with ${focused.name} (focused — nothing active)`
          : 'no generator available'}
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

{#if showDrawer}
  {#key drawerPrefill}
    <GeneratorsDrawer prefill={drawerPrefill} onclose={() => setDrawer(false)} />
  {/key}
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
    display: inline-flex;
    align-items: center;
    gap: 0;
    border: 1px solid var(--border);
    border-radius: 5px;
    background: var(--bg-card);
    font-size: var(--fs-small);
    overflow: hidden;
  }
  .chip button {
    border: none;
    background: none;
    border-radius: 0;
    font-size: inherit;
    color: inherit;
  }
  .chip .dot {
    padding: 0.18rem 0.3rem 0.18rem 0.5rem;
    display: inline-flex;
    align-items: center;
  }
  .chip .dot::before {
    content: '';
    width: 0.55em;
    height: 0.55em;
    border-radius: 50%;
    border: 1.5px solid var(--text-dim);
    background: transparent;
    display: inline-block;
  }
  .chip .dot.on::before {
    border-color: var(--accent);
    background: var(--accent);
  }
  .chip .body {
    padding: 0.18rem 0.55rem 0.18rem 0.15rem;
    cursor: pointer;
  }
  .chip.active {
    border-color: var(--accent);
    color: var(--accent);
    box-shadow: inset 0 -2px 0 var(--accent);
  }
  .chip.focused {
    outline: 2px solid color-mix(in oklab, var(--accent) 75%, white);
    outline-offset: 1px;
  }
  .chip.stale {
    background: color-mix(in oklab, var(--warning, #c90) 18%, var(--bg-card));
    border-color: var(--warning, #c90);
  }
  .chip.unusable .body {
    text-decoration: line-through;
    opacity: 0.6;
  }
  .hint {
    color: var(--text-dim);
    font-size: var(--fs-small);
  }
  .menu-btn,
  .drawer-btn {
    font-size: var(--fs-small);
    padding: 0.18rem 0.55rem;
    color: var(--text-dim);
  }
  .gen-menu-root {
    position: relative;
    margin-left: auto;
    display: inline-flex;
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
  .mr-actions button.icon {
    display: inline-flex;
    align-items: center;
    padding: 0.15rem 0.3rem;
  }
  .mr-actions button.icon.danger:hover {
    color: var(--danger);
  }
  .menu-foot {
    border-top: 1px solid var(--border);
    margin-top: 0.3rem;
    padding-top: 0.3rem;
  }
  .menu-foot button {
    width: 100%;
    font-size: var(--fs-small);
    padding: 0.2rem 0.4rem;
    color: var(--text-dim);
    background: none;
    border-color: transparent;
    text-align: left;
  }
  .menu-foot button:hover {
    color: var(--text);
  }
  .params {
    display: flex;
    gap: 0.6rem;
    align-items: flex-end;
    flex-wrap: wrap;
  }
  .bound {
    font-size: var(--fs-tiny);
    color: var(--text-dim);
    align-self: center;
    padding-bottom: 0.1rem;
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
  .params input:global(.dragnum):not(:focus) {
    cursor: ew-resize;
  }
  .params input:global(.dragnum-dragging) {
    cursor: ew-resize;
    user-select: none;
    border-color: var(--accent);
  }
  .gen {
    margin-left: auto;
  }
</style>
