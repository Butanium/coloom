<script lang="ts">
  // Keybindings settings dialog: rows grouped by category, click a combo to
  // arm capture (global shortcuts mute via capture.suppressed), next keydown
  // becomes the binding, Escape while capturing unbinds. Conflicts show red.
  import {
    ACTIONS,
    type Action,
    type ActionId,
    bindingFor,
    bindingFromEvent,
    bindingId,
    capture,
    formatBinding,
    resetBindings,
    setBinding,
  } from './keybindings.svelte'

  let { onclose }: { onclose: () => void } = $props()

  let capturing = $state<ActionId | null>(null)

  // static grouping, ACTIONS order preserved within and across categories
  const groups: { category: string; actions: Action[] }[] = []
  for (const a of ACTIONS) {
    const g = groups.find((x) => x.category === a.category)
    if (g) g.actions.push(a)
    else groups.push({ category: a.category, actions: [a] })
  }

  const conflictIds = $derived.by(() => {
    const counts = new Map<string, number>()
    for (const a of ACTIONS) {
      const b = bindingFor(a.id)
      if (b !== null) counts.set(bindingId(b), (counts.get(bindingId(b)) ?? 0) + 1)
    }
    return new Set([...counts].filter(([, n]) => n > 1).map(([k]) => k))
  })

  function isConflict(id: ActionId): boolean {
    const b = bindingFor(id)
    return b !== null && conflictIds.has(bindingId(b))
  }

  function arm(id: ActionId) {
    capturing = id
    capture.suppressed = true
  }

  function disarm() {
    capturing = null
    capture.suppressed = false
  }

  // whatever closes us (overlay click, x, parent unmount), never leave the
  // global shortcut table muted
  $effect(() => () => {
    capture.suppressed = false
  })

  function onkeydown(e: KeyboardEvent) {
    if (capturing === null) {
      if (e.key === 'Escape') onclose()
      return
    }
    e.preventDefault()
    e.stopPropagation()
    const k = e.key
    if (k === 'Control' || k === 'Shift' || k === 'Alt' || k === 'Meta') return // wait for the full combo
    // act first, then clear the capture state
    if (k === 'Escape') setBinding(capturing, null)
    else setBinding(capturing, bindingFromEvent(e))
    disarm()
  }
</script>

<svelte:window {onkeydown} />

<div
  class="overlay"
  role="presentation"
  onclick={(e) => {
    if (e.target !== e.currentTarget) return
    disarm()
    onclose()
  }}
>
  <div class="dialog" role="dialog" aria-modal="true" aria-label="keyboard shortcuts" data-testid="kb-dialog">
    <header>
      <h2>keyboard shortcuts</h2>
      <button class="reset" onclick={resetBindings} data-testid="kb-reset">reset all</button>
      <button class="x" onclick={onclose} aria-label="close" data-testid="kb-close">×</button>
    </header>
    <p class="hint">
      click a combo, then press the new keys — press escape while capturing to unbind
    </p>
    <div class="body">
      {#each groups as group (group.category)}
        <section>
          <h3>{group.category}</h3>
          {#each group.actions as a (a.id)}
            {@const b = bindingFor(a.id)}
            <div class="row" data-testid={`kb-row-${a.id}`}>
              <span class="label">{a.label}</span>
              <button
                class="combo"
                class:capturing={capturing === a.id}
                class:conflict={isConflict(a.id)}
                class:unbound={b === null && capturing !== a.id}
                onclick={() => arm(a.id)}
                data-testid={`kb-binding-${a.id}`}
              >
                {capturing === a.id ? 'press a key…' : b === null ? 'unbound' : formatBinding(b)}
              </button>
            </div>
          {/each}
        </section>
      {/each}
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
    width: min(560px, 94vw);
    max-height: 88vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  header {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.6rem 0.9rem;
    border-bottom: 1px solid var(--border);
  }
  header h2 {
    margin: 0;
    font-size: var(--fs-ui);
    font-weight: 600;
    margin-right: auto;
  }
  .reset {
    font-size: var(--fs-small);
    padding: 0.2rem 0.6rem;
  }
  .x {
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
  .hint {
    margin: 0;
    padding: 0.45rem 0.9rem;
    font-size: var(--fs-small);
    color: var(--text-dim);
    border-bottom: 1px dashed var(--border);
  }
  .body {
    padding: 0.6rem 0.9rem 0.9rem;
    overflow-y: auto;
  }
  h3 {
    font-size: var(--fs-small);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-dim);
    margin: 0.7rem 0 0.3rem;
  }
  section:first-child h3 {
    margin-top: 0;
  }
  .row {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.15rem 0;
  }
  .label {
    font-size: var(--fs-ui);
    flex: 1;
    min-width: 0;
  }
  .combo {
    font-family: var(--mono);
    font-size: var(--fs-small);
    padding: 0.15rem 0.55rem;
    min-width: 9rem;
    text-align: center;
  }
  .combo.capturing {
    border-color: var(--accent);
    color: var(--accent);
  }
  .combo.conflict {
    border-color: var(--danger);
    color: var(--danger);
  }
  .combo.unbound {
    color: var(--text-dim);
    border-style: dashed;
  }
</style>
