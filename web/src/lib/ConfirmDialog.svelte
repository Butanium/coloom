<script lang="ts">
  // The single in-app confirm dialog (mounted once in Editor). Renders
  // confirm.svelte.ts state; Escape / backdrop-click cancel, Enter confirms.
  import { confirmState, settleConfirm } from './confirm.svelte'

  function onkeydown(e: KeyboardEvent) {
    if (!confirmState.open) return
    if (e.key === 'Escape') {
      settleConfirm(false)
      e.stopImmediatePropagation() // the global Escape handler stays out of it
      e.preventDefault()
    } else if (e.key === 'Enter') {
      settleConfirm(true)
      e.preventDefault()
    }
  }
</script>

<svelte:window {onkeydown} />

{#if confirmState.open}
  <div
    class="backdrop"
    onclick={(e) => {
      if (e.target === e.currentTarget) settleConfirm(false)
    }}
    role="presentation"
    data-testid="confirm-backdrop"
  >
    <div class="dialog" role="alertdialog" aria-label={confirmState.title} data-testid="confirm-dialog">
      <h3>{confirmState.title}</h3>
      {#if confirmState.body}
        <p class="body">{confirmState.body}</p>
      {/if}
      <div class="actions">
        <button onclick={() => settleConfirm(false)} data-testid="confirm-cancel">cancel</button>
        <button
          class="primary"
          class:danger={confirmState.danger}
          onclick={() => settleConfirm(true)}
          data-testid="confirm-ok"
        >
          {confirmState.confirmLabel}
        </button>
      </div>
      <p class="hint">tip: shift+click a delete button to skip this confirmation</p>
    </div>
  </div>
{/if}

<style>
  .backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.45);
    z-index: 100; /* dialogs layer */
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .dialog {
    background: var(--bg-raised);
    border: 1px solid var(--border);
    border-radius: 8px;
    box-shadow: 0 12px 40px rgba(0, 0, 0, 0.55);
    padding: 1rem 1.2rem;
    max-width: 32rem;
    min-width: 20rem;
  }
  h3 {
    margin: 0 0 0.5rem;
    font-size: var(--fs-ui);
    font-weight: 600;
  }
  .body {
    margin: 0 0 0.8rem;
    font-size: var(--fs-small);
    color: var(--text-dim);
    white-space: pre-wrap;
  }
  .actions {
    display: flex;
    justify-content: flex-end;
    gap: 0.5rem;
  }
  button.danger {
    background: var(--danger);
    border-color: var(--danger);
    color: white;
  }
  .hint {
    margin: 0.7rem 0 0;
    font-size: var(--fs-tiny);
    color: var(--text-dim);
    text-align: right;
  }
</style>
