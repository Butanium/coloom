<!-- Inline two-step destructive button (replaces native confirm() popups):
     first click ARMS it (label switches to confirmLabel, danger styling),
     a second click fires onconfirm; blur or Escape disarm without firing.
     Used for weave delete (Picker) and profile delete (Login). Node deletions
     deliberately do NOT use this — undo (undo.svelte.ts) replaces confirmation
     there entirely. -->
<script lang="ts">
  interface Props {
    label: string
    confirmLabel?: string
    title?: string
    armedTitle?: string
    class?: string
    testid?: string
    onconfirm: () => void
  }
  let {
    label,
    confirmLabel = 'sure?',
    title,
    armedTitle = 'click again to confirm — Escape cancels',
    class: cls = '',
    testid,
    onconfirm,
  }: Props = $props()

  let armed = $state(false)

  function click(e: MouseEvent) {
    e.stopPropagation()
    e.preventDefault()
    if (!armed) {
      armed = true
      return
    }
    armed = false
    onconfirm()
  }

  function keydown(e: KeyboardEvent) {
    if (armed && e.key === 'Escape') {
      e.stopPropagation()
      armed = false
    }
  }
</script>

<button
  type="button"
  class={`armable ${cls}`}
  class:armed
  title={armed ? armedTitle : title}
  data-testid={testid}
  data-armed={armed || undefined}
  onclick={click}
  onblur={() => (armed = false)}
  onkeydown={keydown}
>
  {armed ? confirmLabel : label}
</button>

<style>
  .armable.armed {
    color: var(--danger);
    border-color: var(--danger);
    background: color-mix(in srgb, var(--danger) 12%, transparent);
  }
</style>
