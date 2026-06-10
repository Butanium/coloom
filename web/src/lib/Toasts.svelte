<script lang="ts">
  import type { Toast } from './state.svelte'
  import { dismissToast, toasts } from './state.svelte'

  function runAction(t: Toast) {
    t.action?.run()
    dismissToast(t.id)
  }
</script>

<div class="toasts">
  {#each toasts.items as t (t.id)}
    <div class="toast" class:info={t.kind === 'info'} role="alert">
      <span>{t.message}</span>
      {#if t.action}
        <button
          class="action"
          onclick={() => runAction(t)}
          data-testid="toast-action">{t.action.label}</button
        >
      {/if}
      <button class="close" onclick={() => dismissToast(t.id)}>×</button>
    </div>
  {/each}
</div>

<style>
  .toasts {
    position: fixed;
    bottom: 1rem;
    right: 1rem;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    z-index: 100;
    max-width: 26rem;
  }
  .toast {
    display: flex;
    align-items: flex-start;
    gap: 0.5rem;
    background: var(--bg-card);
    border: 1px solid var(--danger); /* errors (the default) */
    border-radius: 8px;
    padding: 0.6rem 0.8rem;
    font-size: var(--fs-ui);
    word-break: break-word;
  }
  .toast.info {
    border-color: var(--border); /* neutral notices: deleted/restored/undo */
  }
  .action {
    flex-shrink: 0;
    font-size: var(--fs-small);
    padding: 0.1rem 0.55rem;
    color: var(--accent);
    border-color: var(--accent);
  }
  .close {
    background: none;
    border: none;
    padding: 0 0.2rem;
    color: var(--text-dim);
    line-height: 1;
  }
</style>
