<script lang="ts">
  import type { CardBox } from './layout'
  import { STRIP_W, edgePath, layoutWeave } from './layout'
  import NodeCard from './NodeCard.svelte'
  import {
    collapsed,
    identity,
    myCursorNodeId,
    session,
    threadPath,
    viewCommand,
  } from './state.svelte'

  let container: HTMLDivElement
  // viewport size (reactive, for culling + fit math)
  let vw = $state(0)
  let vh = $state(0)
  // view transform: screen = world * scale + (tx, ty)
  let tx = $state(40)
  let ty = $state(40)
  let scale = $state(1)
  let pointerInside = $state(false)

  // spec §2: zoom range ε..=1.0 — arbitrary zoom-out, never above 1:1
  const MIN_SCALE = 0.05
  const MAX_SCALE = 1

  const layout = $derived(
    session.weave ? layoutWeave(session.weave, collapsed) : null,
  )

  const myThread = $derived.by(() => {
    const weave = session.weave
    const cur = myCursorNodeId()
    if (!weave || !cur) return new Set<string>()
    return new Set(threadPath(weave, cur))
  })

  // visible world rect for culling; null = paint everything (pre-mount, like
  // the spec's disable_culling first frame so fit-to-content stays measurable)
  const viewBounds = $derived.by(() => {
    if (vw === 0 || vh === 0) return null
    const m = 120 // margin covers strips, pills, toolbars hanging off cards
    return {
      minX: -tx / scale - m,
      minY: -ty / scale - m,
      maxX: (vw - tx) / scale + m,
      maxY: (vh - ty) / scale + m,
    }
  })

  // simple bounding-box culling of offscreen cards (spec §6.2, cheap version)
  const visibleCards = $derived.by(() => {
    if (!layout) return []
    const v = viewBounds
    const out: [string, CardBox][] = []
    for (const [id, box] of layout.boxes) {
      if (
        !v ||
        (box.x + box.w + STRIP_W >= v.minX &&
          box.x <= v.maxX &&
          box.y + box.h >= v.minY &&
          box.y <= v.maxY)
      ) {
        out.push([id, box])
      }
    }
    return out
  })

  const edges = $derived.by(() => {
    const weave = session.weave
    if (!weave || !layout) return []
    const v = viewBounds
    const out: { key: string; d: string; onThread: boolean }[] = []
    for (const [id, box] of layout.boxes) {
      for (const childId of weave.nodes[id].children) {
        const childBox = layout.boxes.get(childId)
        if (!childBox) continue
        if (v) {
          const yMin = Math.min(box.y, childBox.y)
          const yMax = Math.max(box.y + box.h, childBox.y + childBox.h)
          if (
            childBox.x < v.minX ||
            box.x + box.w > v.maxX ||
            yMax < v.minY ||
            yMin > v.maxY
          ) {
            continue
          }
        }
        out.push({
          key: `${id}->${childId}`,
          d: edgePath(box, childBox),
          onThread: myThread.has(id) && myThread.has(childId),
        })
      }
    }
    return out
  })

  // cursors grouped by node
  const cursorsByNode = $derived.by(() => {
    const map = new Map<string, NonNullable<typeof session.weave>['cursors'][string][]>()
    for (const cur of Object.values(session.weave?.cursors ?? {})) {
      const list = map.get(cur.node_id) ?? []
      list.push(cur)
      map.set(cur.node_id, list)
    }
    return map
  })

  // ---------------------------------------------------------------- pan/zoom

  let pressed = false
  let dragging = false
  let dragDist = 0
  let lastX = 0
  let lastY = 0

  function onPointerDown(e: PointerEvent) {
    // spec §2: drag with primary or middle button pans (right stays contextmenu)
    if (e.button !== 0 && e.button !== 1) return
    if (e.button === 1) e.preventDefault() // suppress browser middle-click autoscroll
    pressed = true
    dragging = false
    dragDist = 0
    lastX = e.clientX
    lastY = e.clientY
  }

  function onPointerMove(e: PointerEvent) {
    if (!pressed) return
    const dx = e.clientX - lastX
    const dy = e.clientY - lastY
    dragDist += Math.abs(dx) + Math.abs(dy)
    // only become a pan (and capture the pointer) past the click threshold —
    // capturing on pointerdown would retarget pointerup and swallow all clicks
    if (!dragging && dragDist > 4) {
      dragging = true
      container.setPointerCapture(e.pointerId)
    }
    if (dragging) {
      tx += dx
      ty += dy
    }
    lastX = e.clientX
    lastY = e.clientY
  }

  function onPointerUp() {
    pressed = false
    dragging = false
  }

  // card click handlers consult this: a drag is not a click
  function suppressClick() {
    return dragDist > 4
  }

  function onWheel(e: WheelEvent) {
    e.preventDefault()
    if (e.ctrlKey || e.metaKey) {
      // pinch / ctrl+wheel: zoom about the pointer position (spec §2)
      const rect = container.getBoundingClientRect()
      const px = e.clientX - rect.left
      const py = e.clientY - rect.top
      const dy = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY
      const next = Math.min(Math.max(scale * Math.exp(-dy * 0.0022), MIN_SCALE), MAX_SCALE)
      tx = px - ((px - tx) / scale) * next
      ty = py - ((py - ty) / scale) * next
      scale = next
    } else {
      // plain scroll: two-axis pan (spec §2 — wheel pans, it does not zoom)
      tx -= e.deltaX
      ty -= e.deltaY
    }
  }

  // ---------------------------------------------------------------- focus

  function viewportSize(): [number, number] {
    // the clientWidth bindings can lag one tick behind mount; fall back to DOM
    const w = vw || container?.clientWidth || 0
    const h = vh || container?.clientHeight || 0
    return [w, h]
  }

  function centerOn(nodeId: string) {
    const box = layout?.boxes.get(nodeId)
    if (!box) return
    const [w, h] = viewportSize()
    if (!w || !h) return
    tx = w / 2 - (box.x + box.w / 2) * scale
    ty = h / 2 - (box.y + box.h / 2) * scale
  }

  // spec §5: zoom so the node renders at 90% of native scale (or exact-fit if
  // it can't), centered
  function focusZoom(nodeId: string) {
    const box = layout?.boxes.get(nodeId)
    if (!box) return
    const [w, h] = viewportSize()
    if (!w || !h) return
    scale = Math.min(0.9, w / (box.w + 2 * STRIP_W), h / (box.h + 80), MAX_SCALE)
    centerOn(nodeId)
  }

  function fitWeave() {
    if (!layout || layout.boxes.size === 0) return
    const [w, h] = viewportSize()
    if (!w || !h) return
    const m = 40
    scale = Math.min(
      Math.max(
        Math.min(w / (layout.width + 2 * m), h / (layout.height + 2 * m)),
        MIN_SCALE,
      ),
      MAX_SCALE,
    )
    tx = w / 2 - (layout.width / 2) * scale
    ty = h / 2 - (layout.height / 2) * scale
  }

  // Deferred focus: requests park here until the target node has a layout box
  // (e.g. the WS event landed before the weave refetch). `bypass` skips the
  // pointer-suppression rule (explicit user commands only).
  let pendingFocus = $state<{ id: string; mode: 'center' | 'zoom'; bypass: boolean } | null>(
    null,
  )
  $effect(() => {
    const pf = pendingFocus
    if (!pf || !layout) return
    if (!layout.boxes.has(pf.id)) {
      // node exists but is hidden inside a collapsed subtree → no focus (spec §5);
      // node not in the weave yet → keep waiting for the refetch
      if (session.weave?.nodes[pf.id]) pendingFocus = null
      return
    }
    pendingFocus = null
    // pointer suppression (sacred): never auto-move the view under the pointer
    if (!pf.bypass && pointerInside) return
    if (pf.mode === 'zoom') focusZoom(pf.id)
    else centerOn(pf.id)
  })

  // initial view: center on my cursor (or the first root)
  let initialized = false
  $effect(() => {
    if (initialized || !layout || layout.boxes.size === 0) return
    initialized = true
    centerOn(myCursorNodeId() ?? session.weave!.roots[0])
  })

  // focus-follow newly added nodes (pointer-suppressed via pendingFocus)
  let lastHandledChange = 0
  $effect(() => {
    const at = session.changedAt
    const id = session.changedNodeId
    if (at === lastHandledChange || !id) return
    lastHandledChange = at
    pendingFocus = { id, mode: 'center', bypass: false }
  })

  // focus-follow MY cursor (someone moved it, or I moved it from another view).
  // Declared AFTER the changed-node effect so a same-flush cursor change
  // overwrites pendingFocus: cursor-change beats new-node (spec §5 priority).
  let lastCursorNode: string | null | undefined = undefined
  $effect(() => {
    const cur = myCursorNodeId()
    if (lastCursorNode === undefined) {
      lastCursorNode = cur
      return
    }
    if (cur === lastCursorNode) return
    lastCursorNode = cur
    if (!cur || !session.weave) return
    // my cursor moved into a collapsed subtree → auto-expand its ancestors
    for (const id of threadPath(session.weave, cur).slice(0, -1)) collapsed.delete(id)
    pendingFocus = { id: cur, mode: 'center', bypass: false }
  })

  // view command bus: fit-cursor / fit-weave / center-node (explicit commands —
  // they bypass pointer suppression)
  let lastSeq = viewCommand.seq
  $effect(() => {
    if (viewCommand.seq === lastSeq) return
    lastSeq = viewCommand.seq
    const kind = viewCommand.kind
    if (kind === 'fit-weave') {
      fitWeave()
    } else if (kind === 'fit-cursor') {
      const cur = myCursorNodeId() ?? session.weave?.roots[0] ?? null
      if (!cur) return
      if (session.weave) {
        for (const id of threadPath(session.weave, cur).slice(0, -1)) collapsed.delete(id)
      }
      pendingFocus = { id: cur, mode: 'zoom', bypass: true }
    } else if (kind === 'center-node' && viewCommand.nodeId) {
      const id = viewCommand.nodeId
      if (session.weave?.nodes[id]) {
        for (const a of threadPath(session.weave, id).slice(0, -1)) collapsed.delete(a)
      }
      pendingFocus = { id, mode: 'center', bypass: true }
    }
  })
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  class="canvas"
  bind:this={container}
  bind:clientWidth={vw}
  bind:clientHeight={vh}
  onpointerdown={onPointerDown}
  onpointermove={onPointerMove}
  onpointerup={onPointerUp}
  onpointercancel={onPointerUp}
  onpointerenter={() => (pointerInside = true)}
  onpointerleave={() => (pointerInside = false)}
  onwheel={onWheel}
>
  <svg>
    <g transform={`translate(${tx}, ${ty}) scale(${scale})`}>
      <!-- wires paint under all cards (spec §6.1) -->
      {#each edges as edge (edge.key)}
        <path class="edge" class:on-thread={edge.onThread} d={edge.d} />
      {/each}
      {#if session.weave && layout}
        {#each visibleCards as [id, box] (id)}
          <NodeCard
            node={session.weave.nodes[id]}
            {box}
            onMyThread={myThread.has(id)}
            cursorsHere={cursorsByNode.get(id) ?? []}
            {suppressClick}
          />
        {/each}
      {/if}
    </g>
  </svg>
  {#if layout && layout.boxes.size === 0}
    <div class="empty-hint">
      this weave is empty — write an opening in the text pane to start, {identity.name}
    </div>
  {/if}
</div>

<style>
  .canvas {
    position: absolute;
    inset: 0;
    overflow: hidden;
    cursor: grab;
    touch-action: none;
  }
  .canvas:active {
    cursor: grabbing;
  }
  svg {
    width: 100%;
    height: 100%;
    display: block;
  }
  .edge {
    fill: none;
    stroke: var(--border);
    stroke-width: 1.5;
  }
  .edge.on-thread {
    stroke: var(--accent);
    stroke-width: 2;
  }
  .empty-hint {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-dim);
    pointer-events: none;
    padding: 2rem;
    text-align: center;
  }
</style>
