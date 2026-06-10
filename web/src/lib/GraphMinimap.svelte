<script lang="ts">
  // Zoomed-out structural minimap (ui-specs/shell-menus-graph.md §3): every node
  // as a unit square, straight edges, per-cursor highlighted threads. Geometry
  // reuses layoutWeave card centers scaled down to a ~1.5-unit pitch (TL's
  // Sugiyama vertex_spacing) — no separate layout algorithm. The collapse set is
  // deliberately ignored: the minimap always shows the whole weave.

  import { creatorColor, creatorLabel, creatorTextColor, cursorColor } from './colors'
  import { CARD_GAP_X, CARD_GAP_Y, CARD_W, cardHeight, layoutWeave } from './layout'
  import {
    contextMenu,
    identity,
    moveMyCursor,
    myCursorNodeId,
    openContextMenu,
    sendViewCommand,
    session,
    threadPath,
    viewCommand,
  } from './state.svelte'
  import type { Cursor } from './types'
  import { genParams, nodeText } from './types'

  // card center → minimap units: 1.5-unit pitch between depth columns and
  // between minimum-height siblings (taller cards just spread a bit more)
  const KX = 1.5 / (CARD_W + CARD_GAP_X)
  const KY = 1.5 / (cardHeight('') + CARD_GAP_Y)
  const EMPTY = new Set<string>() // minimap ignores the collapse set
  const MARGIN = 1.5 // units of padding around the weave bounds
  const FOCUS_SCALE = 15 // px/unit on fit-cursor ≈ "15 columns visible" (spec §3.5)

  let container: HTMLDivElement | undefined
  // view transform: screen = unit * scale + (tx, ty)
  let tx = $state(0)
  let ty = $state(0)
  let scale = $state(12)
  let pointerInside = $state(false)

  interface Square {
    id: string
    cx: number
    cy: number
    fill: string
    onMyThread: boolean
    bookmarked: boolean
    // cursor outline: my cursor wins, else the most recently moved one
    outline: string | null
    cursorNames: string[]
  }
  interface Edge {
    key: string
    x1: number
    y1: number
    x2: number
    y2: number
    color?: string
  }

  const geo = $derived.by(() => {
    const weave = session.weave
    if (!weave) return null
    const layout = layoutWeave(weave, EMPTY)
    const centers = new Map<string, { x: number; y: number }>()
    for (const [id, b] of layout.boxes) {
      centers.set(id, { x: (b.x + b.w / 2) * KX, y: (b.y + b.h / 2) * KY })
    }

    const cursors = Object.values(weave.cursors)
    const cursorsByNode = new Map<string, Cursor[]>()
    for (const c of cursors) {
      const list = cursorsByNode.get(c.node_id) ?? []
      list.push(c)
      cursorsByNode.set(c.node_id, list)
    }
    const myCur = weave.cursors[identity.name]?.node_id ?? null
    const myThread = new Set(myCur ? threadPath(weave, myCur) : [])

    const baseEdges: Edge[] = []
    for (const [id, a] of centers) {
      for (const childId of weave.nodes[id].children) {
        const b = centers.get(childId)
        if (!b) continue
        baseEdges.push({ key: `${id}>${childId}`, x1: a.x, y1: a.y, x2: b.x, y2: b.y })
      }
    }
    // one lit thread per participant cursor; mine sorted last so it paints on top
    const threadEdges: Edge[] = []
    const ordered = [...cursors].sort(
      (a, b) => (a.name === identity.name ? 1 : 0) - (b.name === identity.name ? 1 : 0),
    )
    for (const cur of ordered) {
      const path = threadPath(weave, cur.node_id)
      const color = cursorColor(cur.name)
      for (let i = 1; i < path.length; i++) {
        const a = centers.get(path[i - 1])
        const b = centers.get(path[i])
        if (!a || !b) continue
        threadEdges.push({
          key: `${cur.name}:${path[i - 1]}>${path[i]}`,
          x1: a.x,
          y1: a.y,
          x2: b.x,
          y2: b.y,
          color,
        })
      }
    }

    const squares: Square[] = []
    const byId = new Map<string, Square>()
    for (const [id, c] of centers) {
      const node = weave.nodes[id]
      const here = cursorsByNode.get(id) ?? []
      let outline: string | null = null
      if (here.length > 0) {
        const pick =
          here.find((x) => x.name === identity.name) ??
          [...here].sort((a, b) => b.updated.localeCompare(a.updated))[0]
        outline = cursorColor(pick.name)
      }
      const sq: Square = {
        id,
        cx: c.x,
        cy: c.y,
        fill: creatorColor(node.creator),
        onMyThread: myThread.has(id),
        bookmarked: node.bookmarked,
        outline,
        cursorNames: here.map((x) => x.name),
      }
      squares.push(sq)
      byId.set(id, sq)
    }

    let minX = Infinity
    let minY = Infinity
    let maxX = -Infinity
    let maxY = -Infinity
    for (const c of centers.values()) {
      minX = Math.min(minX, c.x - 0.5)
      minY = Math.min(minY, c.y - 0.5)
      maxX = Math.max(maxX, c.x + 0.5)
      maxY = Math.max(maxY, c.y + 0.5)
    }
    if (centers.size === 0) minX = minY = maxX = maxY = 0
    return {
      squares,
      byId,
      baseEdges,
      threadEdges,
      bounds: {
        minX: minX - MARGIN,
        minY: minY - MARGIN,
        maxX: maxX + MARGIN,
        maxY: maxY + MARGIN,
      },
    }
  })

  // ---------------------------------------------------------------- pan/zoom

  let pressed = false
  let dragging = false
  let dragDist = 0
  let lastX = 0
  let lastY = 0

  function onPointerDown(e: PointerEvent) {
    if (e.button === 1) e.preventDefault() // no browser autoscroll on middle press
    if (e.button !== 0) return
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
    // capture only past the drag threshold — capturing on pointerdown would
    // retarget pointerup and swallow all square clicks
    if (!dragging && dragDist > 4) {
      dragging = true
      tip = null
      container?.setPointerCapture(e.pointerId)
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

  function onWheel(e: WheelEvent) {
    e.preventDefault()
    if (!container) return
    if (e.ctrlKey || e.metaKey) {
      // zoom anchored at the pointer (also catches trackpad pinch)
      const rect = container.getBoundingClientRect()
      const px = e.clientX - rect.left
      const py = e.clientY - rect.top
      const next = Math.min(Math.max(scale * Math.exp(-e.deltaY * 0.0024), 1), 80)
      tx = px - ((px - tx) / scale) * next
      ty = py - ((py - ty) / scale) * next
      scale = next
    } else {
      tx -= e.deltaX
      ty -= e.deltaY
    }
  }

  function centerOn(nodeId: string, newScale?: number) {
    const sq = geo?.byId.get(nodeId)
    if (!sq || !container) return
    if (newScale !== undefined) scale = newScale
    const rect = container.getBoundingClientRect()
    tx = rect.width / 2 - sq.cx * scale
    ty = rect.height / 2 - sq.cy * scale
  }

  function fitWeave() {
    if (!geo || !container || geo.squares.length === 0) return
    const { minX, minY, maxX, maxY } = geo.bounds
    const rect = container.getBoundingClientRect()
    const s = Math.min(rect.width / (maxX - minX), rect.height / (maxY - minY))
    scale = Math.min(s, 40) // tiny weaves shouldn't become wall-sized squares
    tx = rect.width / 2 - ((minX + maxX) / 2) * scale
    ty = rect.height / 2 - ((minY + maxY) / 2) * scale
  }

  // fit the whole weave once on mount (first non-empty layout)
  let initialized = false
  $effect(() => {
    if (initialized || !geo || geo.squares.length === 0) return
    initialized = true
    fitWeave()
  })

  // view command bus: fit-weave / fit-cursor / center-node
  let lastSeq = viewCommand.seq // ignore commands sent before this pane mounted
  $effect(() => {
    const { kind, nodeId, seq } = viewCommand
    if (seq === lastSeq || !kind) return
    lastSeq = seq
    if (kind === 'fit-weave') fitWeave()
    else if (kind === 'fit-cursor') {
      const cur = myCursorNodeId()
      if (cur) centerOn(cur, FOCUS_SCALE)
    } else if (kind === 'center-node' && nodeId) centerOn(nodeId)
  })

  // focus-follow changed nodes — never while the pointer is inside this view
  let lastHandledChange = 0
  $effect(() => {
    const at = session.changedAt
    const id = session.changedNodeId
    if (at === lastHandledChange || !id || !geo) return
    if (!geo.byId.has(id)) return // layout lags the event; rerun on refetch
    lastHandledChange = at
    if (!pointerInside) centerOn(id)
  })

  // ---------------------------------------------------------------- gestures

  function onSquareClick(e: MouseEvent, id: string) {
    if (dragDist > 4) return // a pan is not a click
    if (e.ctrlKey || e.metaKey || e.altKey) {
      sendViewCommand('center-node', id) // teleport without moving the cursor
      return
    }
    void moveMyCursor(id)
  }

  function onSquareAux(e: MouseEvent, id: string) {
    if (e.button !== 1) return
    e.preventDefault()
    sendViewCommand('center-node', id)
  }

  function onSquareContext(e: MouseEvent, id: string) {
    e.preventDefault()
    tip = null
    openContextMenu(id, e.clientX, e.clientY)
  }

  // ---------------------------------------------------------------- hover + tooltip

  let tip = $state<{ id: string; x: number; y: number } | null>(null)

  function onSquareEnter(e: PointerEvent, id: string) {
    if (dragging) return
    session.hoveredNodeId = id
    moveTip(e, id)
  }

  function moveTip(e: PointerEvent, id: string) {
    if (dragging || contextMenu.open || !container) {
      tip = null
      return
    }
    const rect = container.getBoundingClientRect()
    tip = { id, x: e.clientX - rect.left, y: e.clientY - rect.top }
  }

  function onSquareLeave(id: string) {
    if (session.hoveredNodeId === id) session.hoveredNodeId = null
    if (tip?.id === id) tip = null
  }

  const tipNode = $derived(tip && session.weave ? (session.weave.nodes[tip.id] ?? null) : null)
  const tipParams = $derived(tipNode ? genParams(tipNode) : null)
  const tipCursors = $derived(tip ? (geo?.byId.get(tip.id)?.cursorNames ?? []) : [])
  const tipExcerpt = $derived.by(() => {
    if (!tipNode) return ''
    const text = nodeText(tipNode)
    return text.length > 180 ? `${text.slice(0, 180)}…` : text
  })
  const tipFlipX = $derived(tip && container ? tip.x > container.clientWidth - 300 : false)
  const tipFlipY = $derived(tip && container ? tip.y > container.clientHeight - 160 : false)
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  class="graph"
  bind:this={container}
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
      {#if geo}
        {#each geo.baseEdges as edge (edge.key)}
          <line class="edge" x1={edge.x1} y1={edge.y1} x2={edge.x2} y2={edge.y2} />
        {/each}
        {#each geo.threadEdges as edge (edge.key)}
          <line
            class="edge thread"
            x1={edge.x1}
            y1={edge.y1}
            x2={edge.x2}
            y2={edge.y2}
            stroke={edge.color}
          />
        {/each}
        {#each geo.squares as sq (sq.id)}
          <!-- svelte-ignore a11y_click_events_have_key_events -->
          <g
            class="node"
            data-node-id={sq.id}
            onclick={(e) => onSquareClick(e, sq.id)}
            onauxclick={(e) => onSquareAux(e, sq.id)}
            oncontextmenu={(e) => onSquareContext(e, sq.id)}
            onpointerenter={(e) => onSquareEnter(e, sq.id)}
            onpointermove={(e) => moveTip(e, sq.id)}
            onpointerleave={() => onSquareLeave(sq.id)}
          >
            <rect
              x={sq.cx - 0.5}
              y={sq.cy - 0.5}
              width="1"
              height="1"
              rx="0.12"
              fill={sq.fill}
              fill-opacity={sq.onMyThread ? 1 : 0.7}
              class:hovered={session.hoveredNodeId === sq.id}
              stroke={session.hoveredNodeId === sq.id ? 'var(--text)' : (sq.outline ?? 'none')}
              stroke-width={session.hoveredNodeId === sq.id ? 2.5 : 2}
            />
            {#if sq.bookmarked}
              <!-- TL's cut-out bookmark ribbon with a V-notch (spec §3.3) -->
              <polygon
                class="ribbon"
                points={`${sq.cx - 0.25},${sq.cy - 0.35} ${sq.cx + 0.25},${sq.cy - 0.35} ${sq.cx + 0.25},${sq.cy + 0.35} ${sq.cx},${sq.cy + 0.2} ${sq.cx - 0.25},${sq.cy + 0.35}`}
              />
            {/if}
          </g>
        {/each}
      {/if}
    </g>
  </svg>

  {#if geo && geo.squares.length === 0}
    <div class="empty-hint">nothing woven yet — the minimap fills in as nodes land</div>
  {/if}

  {#if tip && tipNode}
    <div
      class="tooltip"
      style:left={`${tip.x}px`}
      style:top={`${tip.y}px`}
      style:transform={`translate(${tipFlipX ? 'calc(-100% - 14px)' : '14px'}, ${tipFlipY ? 'calc(-100% - 14px)' : '14px'})`}
    >
      <div class="tip-head">
        <span style:color={creatorTextColor(tipNode.creator)}>{creatorLabel(tipNode.creator)}</span>
        {#if tipNode.bookmarked}<span class="tip-mark">⚑</span>{/if}
        {#each tipCursors as name (name)}
          <span class="tip-cursor" style:color={cursorColor(name)}>@{name}</span>
        {/each}
      </div>
      {#if tipExcerpt}
        <div class="tip-text">{tipExcerpt}</div>
      {/if}
      {#if tipParams}
        <div class="tip-params">
          {#each Object.entries(tipParams) as [k, v] (k)}
            <span>{k}={String(v)}</span>
          {/each}
        </div>
      {/if}
    </div>
  {/if}
</div>

<style>
  .graph {
    position: absolute;
    inset: 0;
    overflow: hidden;
    cursor: grab;
    touch-action: none;
    background: var(--bg);
  }
  .graph:active {
    cursor: grabbing;
  }
  svg {
    width: 100%;
    height: 100%;
    display: block;
  }
  .edge {
    stroke: var(--border);
    stroke-width: 1.5;
    vector-effect: non-scaling-stroke;
  }
  .edge.thread {
    stroke-width: 2;
    stroke-opacity: 0.85;
  }
  .node {
    cursor: pointer;
  }
  .node rect {
    vector-effect: non-scaling-stroke;
  }
  .ribbon {
    fill: var(--bg);
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
  .tooltip {
    position: absolute;
    pointer-events: none;
    max-width: 300px;
    background: var(--bg-raised);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.4rem 0.55rem;
    font-size: var(--fs-ui);
    z-index: 10;
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.45);
  }
  .tip-head {
    display: flex;
    gap: 0.5rem;
    align-items: baseline;
    flex-wrap: wrap;
    font-weight: 600;
  }
  .tip-mark {
    color: var(--accent);
  }
  .tip-cursor {
    font-weight: 400;
  }
  .tip-text {
    font-family: var(--mono);
    color: var(--text);
    white-space: pre-wrap;
    word-break: break-word;
    margin-top: 0.25rem;
    max-height: 6.5em;
    overflow: hidden;
  }
  .tip-params {
    display: flex;
    flex-wrap: wrap;
    gap: 0.2rem 0.6rem;
    margin-top: 0.25rem;
    color: var(--text-dim);
    font-family: var(--mono);
  }
</style>
