<script lang="ts">
  import {
    BOOKMARK_COLOR,
    BOOKMARK_ON_THREAD_COLOR,
    creatorColor,
    creatorLabel,
    creatorTextColor,
    cursorColor,
    tokenOpacity,
  } from './colors'
  import type { CardBox } from './layout'
  import { BTN, PAD, STRIP_W } from './layout'
  import {
    collapsed,
    createNode,
    deleteNode,
    generateAt,
    identity,
    moveMyCursor,
    myCursorNodeId,
    openContextMenu,
    session,
    toggleBookmark,
    toggleCollapsed,
  } from './state.svelte'
  import type { Cursor, WeaveNode } from './types'
  import { genParams, nodeText } from './types'

  let {
    node,
    box,
    onMyThread,
    cursorsHere,
    suppressClick,
  }: {
    node: WeaveNode
    box: CardBox
    onMyThread: boolean
    cursorsHere: Cursor[]
    suppressClick: () => boolean
  } = $props()

  // TEXT fill: creator hue mixed into the light theme text → readable at any
  // tokenOpacity over the dark bg (creatorColor stays for the border stroke).
  const color = $derived(creatorTextColor(node.creator))
  const hovered = $derived(session.hoveredNodeId === node.id)
  const myCursorHere = $derived(cursorsHere.some((c) => c.name === identity.name))
  const text = $derived(nodeText(node))
  const isCollapsed = $derived(collapsed.has(node.id))
  const hasChildren = $derived(node.children.length > 0)
  // collapsed node with children: the "…" stub is ALWAYS visible (spec §4)
  const showStub = $derived(isCollapsed && hasChildren)
  const firstChild = $derived(node.children[0] ?? null)
  // stub line/fill highlight: peek at the first hidden child (spec §4)
  const stubPeek = $derived(firstChild !== null && session.hoveredNodeId === firstChild)
  // connector accent: for the stub case, "any child on my thread" — on a path
  // that means the node is on my thread but isn't its endpoint
  const childOnThread = $derived(onMyThread && myCursorNodeId() !== node.id)

  // border state machine (spec §3.2): bookmark color BEATS thread color;
  // a cursor here doubles the stroke width. Creator color tints the TEXT.
  const stroke = $derived(
    node.bookmarked
      ? onMyThread
        ? BOOKMARK_ON_THREAD_COLOR
        : BOOKMARK_COLOR
      : onMyThread
        ? 'var(--accent)'
        : 'var(--border)',
  )
  const strokeWidth = $derived(cursorsHere.length > 0 ? 3 : 1.5)

  const gp = $derived(genParams(node))
  // hybrid edited marker: a model node later hand-edited carries metadata.edited_by
  const editedBy = $derived(
    typeof node.metadata.edited_by === 'string' ? node.metadata.edited_by : null,
  )
  const tooltip = $derived.by(() => {
    const label = creatorLabel(node.creator) + (editedBy ? ' (edited)' : '')
    const lines = [`${label} · ${new Date(node.created).toLocaleString()}`]
    if (gp) {
      lines.push(
        Object.entries(gp)
          .map(([k, v]) => `${k}=${v}`)
          .join(' · '),
      )
    }
    return lines.join('\n')
  })

  // cursor pills (all named cursors) — adaptive widths, stacked above the card.
  // width tracks the 13px pill-label font (≈7.6px/char advance + 14px padding)
  const pills = $derived.by(() => {
    let x = 0
    return cursorsHere.map((c) => {
      const label = c.name.length > 10 ? `${c.name.slice(0, 9)}…` : c.name
      const w = Math.round(14 + label.length * 7.6)
      const pill = { name: c.name, label, x, w }
      x += w + 4
      return pill
    })
  })

  // ---------------------------------------------------------------- actions

  function modified(e: MouseEvent) {
    return e.ctrlKey || e.shiftKey || e.altKey || e.metaKey
  }

  function genHere(moveCursor: boolean) {
    collapsed.delete(node.id) // generating always opens the node (spec §4)
    void generateAt(node.id, { moveCursor })
  }

  function onCardClick() {
    if (suppressClick()) return
    void moveMyCursor(node.id)
  }

  function onContextMenu(e: MouseEvent) {
    e.preventDefault()
    openContextMenu(node.id, e.clientX, e.clientY)
  }

  function onGenClick(e: MouseEvent) {
    if (suppressClick()) return
    genHere(modified(e))
  }

  // middle-click = generate AND move my cursor (spec §4 / §3.5 modifier rule)
  function onGenAux(e: MouseEvent) {
    if (e.button !== 1) return
    e.preventDefault()
    if (suppressClick()) return // a middle-drag (pan) is not a click
    genHere(true)
  }

  function onAddChild() {
    if (suppressClick()) return
    collapsed.delete(node.id)
    void createNode('', { parentId: node.id, moveCursor: true })
  }

  function onStubClick() {
    if (suppressClick()) return
    collapsed.delete(node.id)
  }

  const toolbar = $derived.by(() => {
    const btns: {
      key: string
      label: string
      title: string
      danger?: boolean
      active?: boolean
      onClick: (e: MouseEvent) => void
      onAux?: (e: MouseEvent) => void
    }[] = [
      {
        key: 'gen',
        label: '+',
        title: 'generate completions (middle/modifier-click: also move my cursor here)',
        onClick: onGenClick,
        onAux: onGenAux,
      },
      {
        key: 'add',
        label: '↳',
        title: 'add empty child + move my cursor there',
        onClick: onAddChild,
      },
      {
        key: 'mark',
        label: node.bookmarked ? '★' : '☆',
        title: node.bookmarked ? 'remove bookmark' : 'bookmark node',
        active: node.bookmarked,
        onClick: () => {
          if (!suppressClick()) void toggleBookmark(node.id)
        },
      },
      {
        key: 'del',
        label: '✕',
        title: 'delete node (and its subtree)',
        danger: true,
        onClick: () => {
          if (!suppressClick()) void deleteNode(node.id)
        },
      },
    ]
    if (hasChildren) {
      btns.push({
        key: 'fold',
        label: isCollapsed ? '▸' : '▾',
        title: isCollapsed
          ? `expand subtree (${node.children.length} hidden)`
          : 'collapse subtree',
        active: isCollapsed,
        onClick: () => {
          if (!suppressClick()) toggleCollapsed(node.id)
        },
      })
    }
    return btns
  })

  const TOOL_W = 28
  const TOOL_H = 24
  const cy = $derived(box.y + box.h / 2)
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<g
  class="card"
  class:hovered
  data-node-id={node.id}
  onpointerenter={() => (session.hoveredNodeId = node.id)}
  onpointerleave={() => (session.hoveredNodeId = null)}
  oncontextmenu={onContextMenu}
>
  {#if hovered}
    <!-- invisible halo: keeps the hover region contiguous across the gap to the
         toolbar below the card, so it stays mounted while the pointer travels -->
    <rect
      class="halo"
      x={box.x - 8}
      y={box.y - 8}
      width={box.w + 16}
      height={box.h + TOOL_H + 20}
    />
  {/if}
  <!-- invisible hover strip right of the card (spec §4): always present, width
       5*pad — entering it directly (without crossing the card) reveals the
       `+`; being inside the same <g> keeps the card's hover state alive -->
  <rect
    class="strip"
    x={box.x + box.w}
    y={box.y}
    width={STRIP_W}
    height={box.h}
  />
  <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
  <g onclick={onCardClick} class="clickable">
    <rect
      x={box.x}
      y={box.y}
      width={box.w}
      height={box.h}
      rx="7"
      class="bg"
      class:hovered
      style:stroke
      style:stroke-width={strokeWidth}
    />
    {#if myCursorHere}
      <rect
        x={box.x - 4.5}
        y={box.y - 4.5}
        width={box.w + 9}
        height={box.h + 9}
        rx="10"
        class="cursor-ring"
        style:stroke={'var(--accent)'}
      />
    {/if}
    <foreignObject x={box.x} y={box.y} width={box.w} height={box.h}>
      <div class="text" style:color style:-webkit-line-clamp={Math.floor((box.h - 16) / 18)}>
        {#if text === ''}
          <span class="empty">(no text)</span>
        {:else if node.content.type === 'tokens'}
          {#each node.content.tokens as t}<span style:opacity={tokenOpacity(t)}
              >{t.text}</span
            >{/each}
        {:else}
          {text}
        {/if}
      </div>
    </foreignObject>
    <title>{tooltip}</title>
  </g>

  {#if node.bookmarked}
    <text x={box.x + box.w - 16} y={box.y + 16} class="bookmark">★</text>
  {/if}

  <!-- cursor pills above the card: every named cursor, always visible.
       pointer-events none — pills are indicators, and they overlap the card
       above (gap 14 < pill extent 21): an interactive pill swallowed its clicks -->
  {#each pills as pill (pill.name)}
    <g class="pill" transform={`translate(${box.x + pill.x}, ${box.y - 13})`}>
      <rect x="0" y="-10" width={pill.w} height="18" rx="9" fill={cursorColor(pill.name)} />
      <text x={pill.w / 2} y="3.5" class="pill-label">{pill.label}</text>
    </g>
  {/each}

  <!-- strip contents: connector line + (`+` on hover | always-visible `…` stub) -->
  {#if showStub || hovered}
    <line
      class="connector"
      class:on-thread={showStub ? childOnThread : onMyThread}
      x1={box.x + box.w}
      y1={cy}
      x2={box.x + box.w + PAD}
      y2={cy}
    />
  {/if}
  {#if showStub}
    <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
    <g
      class="stub clickable"
      class:peek={stubPeek}
      transform={`translate(${box.x + box.w + PAD}, ${cy - BTN / 2})`}
      onclick={onStubClick}
      onpointerenter={() => {
        if (firstChild) session.hoveredNodeId = firstChild
      }}
      onpointerleave={() => (session.hoveredNodeId = node.id)}
    >
      <rect width={BTN} height={BTN} rx="5" />
      <text x={BTN / 2} y={BTN / 2 + 2} class="stub-label">…</text>
      <title>expand node ({node.children.length} hidden)</title>
    </g>
  {:else if hovered}
    <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
    <g
      class="gen clickable"
      transform={`translate(${box.x + box.w + PAD}, ${cy - BTN / 2})`}
      onclick={onGenClick}
      onauxclick={onGenAux}
      onpointerdown={(e) => {
        if (e.button === 1) e.preventDefault()
      }}
    >
      <rect width={BTN} height={BTN} rx="5" />
      <text x={BTN / 2} y={BTN / 2 + 5} class="gen-label">+</text>
      <title>generate completions (middle/modifier-click: also move my cursor here)</title>
    </g>
  {/if}

  {#if hovered}
    <!-- hover toolbar below the card: generate / add child / bookmark / delete / collapse -->
    {#each toolbar as btn, i (btn.key)}
      <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
      <g
        class="tool clickable"
        class:danger={btn.danger}
        class:active={btn.active}
        transform={`translate(${box.x + i * (TOOL_W + 2)}, ${box.y + box.h + 4})`}
        onclick={btn.onClick}
        onauxclick={btn.onAux}
        onpointerdown={(e) => {
          if (e.button === 1) e.preventDefault()
        }}
      >
        <rect width={TOOL_W} height={TOOL_H} rx="4" />
        <text x={TOOL_W / 2} y={TOOL_H / 2 + 4} class="tool-label">{btn.label}</text>
        <title>{btn.title}</title>
      </g>
    {/each}
  {/if}
</g>

<style>
  .clickable {
    cursor: pointer;
  }
  .halo,
  .strip {
    fill: none;
    pointer-events: all;
  }
  .bg {
    fill: var(--bg-card);
  }
  .bg.hovered {
    fill: #2c2c3c;
  }
  .cursor-ring {
    fill: none;
    stroke-width: 1.5;
    stroke-dasharray: 5 3;
    pointer-events: none;
  }
  .text {
    font-family: var(--mono);
    font-size: 15px;
    line-height: 20px;
    padding: 8px;
    height: 100%;
    box-sizing: border-box;
    overflow: hidden;
    white-space: pre-wrap;
    word-break: break-word;
    user-select: none;
    /* clamp to whole lines so the last visible line isn't sliced in half */
    display: -webkit-box;
    -webkit-box-orient: vertical;
  }
  .empty {
    font-style: italic;
    opacity: 0.6;
  }
  .bookmark {
    fill: var(--accent);
    font-size: 15px;
    pointer-events: none;
  }
  .pill {
    pointer-events: none;
  }
  .pill-label {
    fill: #14141a;
    font-size: 13px;
    font-weight: 700;
    text-anchor: middle;
    pointer-events: none;
  }
  .connector {
    stroke: var(--border);
    stroke-width: 1.5;
    pointer-events: none;
  }
  .connector.on-thread {
    stroke: var(--accent);
  }
  .gen rect {
    fill: var(--accent);
  }
  .gen-label {
    fill: #1a1208;
    font-size: 15px;
    font-weight: 700;
    text-anchor: middle;
    pointer-events: none;
  }
  .stub rect {
    fill: var(--bg-raised);
    stroke: var(--border);
  }
  .stub.peek rect {
    fill: #2c2c3c;
    stroke: var(--text-dim);
  }
  .stub-label {
    fill: var(--text-dim);
    font-size: 15px;
    font-weight: 700;
    text-anchor: middle;
    pointer-events: none;
  }
  .tool rect {
    fill: var(--bg-raised);
    stroke: var(--border);
  }
  .tool:hover rect {
    stroke: var(--text-dim);
  }
  .tool.active rect {
    stroke: var(--accent);
  }
  .tool.active .tool-label {
    fill: var(--accent);
  }
  .tool.danger:hover rect {
    stroke: var(--danger);
  }
  .tool.danger:hover .tool-label {
    fill: var(--danger);
  }
  .tool-label {
    fill: var(--text-dim);
    font-size: 14px;
    text-anchor: middle;
    pointer-events: none;
  }
</style>
