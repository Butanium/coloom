<script lang="ts">
  import type { CardBox } from './layout'
  import { STRIP_W, edgePath, layoutWeave } from './layout'
  import NodeCard from './NodeCard.svelte'
  import SelectionBar from './SelectionBar.svelte'
  import { clearSelection, selectMany } from './selection.svelte'
  import {
    collapsed,
    identity,
    myCursorNodeId,
    session,
    threadPath,
    viewCommand,
    weaveWithPlaceholders,
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

  // weave + phantom placeholder children for in-flight generations (task #24):
  // ALL layout-related reads below go through weaveAug so phantom ids resolve
  const weaveAug = $derived(weaveWithPlaceholders())
  const layout = $derived(weaveAug ? layoutWeave(weaveAug, collapsed) : null)

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
    const weave = weaveAug
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

  // ---------------------------------------------------------------- pan/zoom + rubber-band

  let pressed = false
  let dragging = false
  let dragDist = 0
  let lastX = 0
  let lastY = 0
  // shift+primary drag = rubber-band multi-select (it must NEVER pan)
  let mode: 'pan' | 'rubber' = 'pan'
  let rubberAnchor = { x: 0, y: 0 } // world coords
  let rubber = $state<{ x: number; y: number; w: number; h: number } | null>(null)

  function worldPoint(e: PointerEvent): { x: number; y: number } {
    const rect = container.getBoundingClientRect()
    return {
      x: (e.clientX - rect.left - tx) / scale,
      y: (e.clientY - rect.top - ty) / scale,
    }
  }

  function onPointerDown(e: PointerEvent) {
    // spec §2: drag with primary or middle button pans (right stays contextmenu)
    if (e.button !== 0 && e.button !== 1) return
    if (e.button === 1) e.preventDefault() // suppress browser middle-click autoscroll
    mode = e.shiftKey && e.button === 0 ? 'rubber' : 'pan'
    if (mode === 'rubber') rubberAnchor = worldPoint(e)
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
    // only become a drag (and capture the pointer) past the click threshold —
    // capturing on pointerdown would retarget pointerup and swallow all clicks
    if (!dragging && dragDist > 4) {
      dragging = true
      container.setPointerCapture(e.pointerId)
    }
    if (dragging) {
      if (mode === 'pan') {
        tx += dx
        ty += dy
      } else {
        const p = worldPoint(e)
        rubber = {
          x: Math.min(rubberAnchor.x, p.x),
          y: Math.min(rubberAnchor.y, p.y),
          w: Math.abs(p.x - rubberAnchor.x),
          h: Math.abs(p.y - rubberAnchor.y),
        }
      }
    }
    lastX = e.clientX
    lastY = e.clientY
  }

  function onPointerUp() {
    if (dragging && mode === 'rubber' && rubber && layout) {
      const r = rubber
      const hits: string[] = []
      for (const [id, box] of layout.boxes) {
        if (box.x < r.x + r.w && box.x + box.w > r.x && box.y < r.y + r.h && box.y + box.h > r.y) {
          hits.push(id)
        }
      }
      selectMany(hits)
    }
    rubber = null
    pressed = false
    dragging = false
  }

  // selection is per-weave: drop it when the open weave changes
  let selectionWeaveId: string | null = null
  $effect(() => {
    const id = session.weave?.id ?? null
    if (id !== selectionWeaveId) {
      selectionWeaveId = id
      clearSelection()
    }
  })

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
    } else if (e.shiftKey) {
      // shift+scroll: horizontal pan — a vertical wheel maps onto X (some
      // browsers/trackpads already deliver shifted scrolls as deltaX; keep it)
      tx -= e.deltaX !== 0 ? e.deltaX : e.deltaY
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

  /** Is the node's card fully inside the viewport (with a margin, so "nearly
   * outside" counts as outside)? Selection/navigation only pans when this is
   * false — a visible node must not yank the view (task #23). A card larger
   * than the viewport on an axis counts as visible on that axis when it spans
   * past both margins (it fills the screen; recentering would just jitter). */
  function nodeVisible(nodeId: string): boolean {
    const box = layout?.boxes.get(nodeId)
    if (!box) return false
    const [w, h] = viewportSize()
    if (!w || !h) return false
    const m = 40
    const x0 = box.x * scale + tx
    const y0 = box.y * scale + ty
    const x1 = (box.x + box.w) * scale + tx
    const y1 = (box.y + box.h) * scale + ty
    const visX = x1 - x0 <= w - 2 * m ? x0 >= m && x1 <= w - m : x0 <= m && x1 >= w - m
    const visY = y1 - y0 <= h - 2 * m ? y0 >= m && y1 <= h - m : y0 <= m && y1 >= h - m
    return visX && visY
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
    // center-on-demand (task #23): pan only when the node is (nearly) outside
    // the viewport — selecting/navigating to a visible node must not move the
    // view. fit/zoom commands above always act (they're explicit view asks).
    else if (!nodeVisible(pf.id)) centerOn(pf.id)
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
      {#if weaveAug && layout}
        {#each visibleCards as [id, box] (id)}
          <NodeCard
            node={weaveAug.nodes[id]}
            {box}
            onMyThread={myThread.has(id)}
            cursorsHere={cursorsByNode.get(id) ?? []}
            {suppressClick}
          />
        {/each}
      {/if}
      {#if rubber}
        <rect class="rubber" x={rubber.x} y={rubber.y} width={rubber.w} height={rubber.h} />
      {/if}
    </g>
  </svg>
  <SelectionBar />
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
  .rubber {
    fill: rgba(91, 157, 217, 0.12);
    stroke: #5b9dd9;
    stroke-width: 1.5;
    vector-effect: non-scaling-stroke;
    pointer-events: none;
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
