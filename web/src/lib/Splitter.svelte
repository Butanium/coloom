<script lang="ts">
  // Vertical drag handle between panes; calls back with the pointer's x delta.
  let { onresize, ondone }: { onresize: (dx: number) => void; ondone?: () => void } =
    $props()

  let lastX = 0

  function onPointerDown(e: PointerEvent) {
    lastX = e.clientX
    const el = e.currentTarget as HTMLElement
    el.setPointerCapture(e.pointerId)
  }

  function onPointerMove(e: PointerEvent) {
    const el = e.currentTarget as HTMLElement
    if (!el.hasPointerCapture(e.pointerId)) return
    onresize(e.clientX - lastX)
    lastX = e.clientX
  }

  function onPointerUp(e: PointerEvent) {
    const el = e.currentTarget as HTMLElement
    if (el.hasPointerCapture(e.pointerId)) {
      el.releasePointerCapture(e.pointerId)
      ondone?.()
    }
  }
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  class="splitter"
  onpointerdown={onPointerDown}
  onpointermove={onPointerMove}
  onpointerup={onPointerUp}
  onpointercancel={onPointerUp}
></div>

<style>
  .splitter {
    width: 5px;
    cursor: col-resize;
    background: var(--border);
    flex-shrink: 0;
    touch-action: none;
  }
  .splitter:hover {
    background: var(--accent);
  }
</style>
