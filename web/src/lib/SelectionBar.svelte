<!-- Floating bulk-action bar for the canvas multi-select: appears while the
     selection is non-empty. Delete cascades subtrees (confirm names the count);
     selected descendants of another selected node are skipped (their ancestor's
     cascade removes them). -->
<script lang="ts">
  import { api } from './api'
  import { clearSelection, validSelection } from './selection.svelte'
  import { collapsed, session, withToast } from './state.svelte'
  import type { Weave } from './types'

  const ids = $derived(validSelection())
  let busy = $state(false)

  // ids whose selected ancestors won't already delete them via the cascade
  function topmostOnly(weave: Weave, sel: string[]): string[] {
    const set = new Set(sel)
    return sel.filter((id) => {
      let cur = weave.nodes[id]?.parents[0]
      while (cur !== undefined) {
        if (set.has(cur)) return false
        cur = weave.nodes[cur]?.parents[0]
      }
      return true
    })
  }

  async function bookmarkAll() {
    const weave = session.weave
    if (!weave || busy) return
    busy = true
    try {
      for (const id of ids) {
        if (!weave.nodes[id].bookmarked) {
          await withToast(() => api.setBookmark(weave.id, id, true))
        }
      }
    } finally {
      busy = false
    }
  }

  function collapseAll() {
    for (const id of ids) collapsed.add(id)
  }

  async function deleteAll() {
    const weave = session.weave
    if (!weave || busy) return
    const doomed = topmostOnly(weave, ids)
    const msg =
      `Delete ${ids.length} selected node(s)? ` +
      'Deletion CASCADES: every selected node’s entire subtree is removed too.'
    if (!confirm(msg)) return
    busy = true
    try {
      for (const id of doomed) {
        await withToast(() => api.removeNode(weave.id, id))
      }
      clearSelection()
    } finally {
      busy = false
    }
  }
</script>

{#if ids.length > 0}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="selbar" onpointerdown={(e) => e.stopPropagation()}>
    <span class="count">{ids.length} selected</span>
    <button onclick={() => void bookmarkAll()} disabled={busy}>bookmark all</button>
    <button onclick={collapseAll} disabled={busy}>collapse all</button>
    <button class="danger" onclick={() => void deleteAll()} disabled={busy}>delete all</button>
    <button onclick={clearSelection} disabled={busy}>clear</button>
  </div>
{/if}

<style>
  .selbar {
    position: absolute;
    bottom: 14px;
    left: 50%;
    transform: translateX(-50%);
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.35rem 0.7rem;
    background: var(--bg-raised);
    border: 1px solid var(--border);
    border-radius: 8px;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.45);
    z-index: 5;
  }
  .count {
    color: var(--text-dim);
    font-size: var(--fs-small);
    white-space: nowrap;
  }
  button {
    font-size: var(--fs-small);
    padding: 0.15rem 0.55rem;
  }
  button.danger {
    color: var(--danger);
    border-color: var(--danger);
  }
</style>
