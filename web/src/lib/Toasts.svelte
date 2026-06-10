<script lang="ts">
  import { dismissToast, toasts } from './state.svelte'
</script>

<div class="toasts">
  {#each toasts.items as t (t.id)}
    <div class="toast" role="alert">
      <span>{t.message}</span>
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
    border: 1px solid var(--danger);
    border-radius: 8px;
    padding: 0.6rem 0.8rem;
    font-size: var(--fs-ui);
    word-break: break-word;
  }
  .close {
    background: none;
    border: none;
    padding: 0 0.2rem;
    color: var(--text-dim);
    line-height: 1;
  }
</style>
