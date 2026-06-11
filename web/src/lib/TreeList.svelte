<script lang="ts">
  // Tree list (ui-specs/lists.md §5, coloom deltas applied): a sliding window
  // of the weave centered near MY cursor, with a search mode on top.
  import { untrack } from 'svelte'
  import {
    creatorColor,
    creatorLabel,
    creatorTextColor,
    cursorColor,
    tokenOpacity,
  } from './colors'
  import {
    collapsed,
    createNode,
    deleteNode,
    generateAt,
    identity,
    moveMyCursor,
    openContextMenu,
    sendViewCommand,
    session,
    threadPath,
    toggleBookmark,
    toggleCollapsed,
    ui,
    weaveWithPlaceholders,
  } from './state.svelte'
  import type { Cursor, WeaveNode } from './types'
  import { genParams, nodeText } from './types'

  const MAX_DEPTH = 10 // Tapestry default max_tree_depth
  const SEARCH_CAP = 200
  const TOKEN_RENDER_CAP = 100 // single-line rows never show more anyway

  let container = $state<HTMLDivElement | null>(null)
  // plain (non-reactive) on purpose: read only inside callbacks/effects, and
  // pointer enter/leave must never retrigger renders or autoscroll effects
  let pointerInside = false

  // weave + phantom placeholder children for in-flight generations (task #24);
  // the subtree renderer reads THIS so skeleton rows appear under the target.
  // Search / window-roots stay on the real weave (placeholders aren't targets).
  const weaveAug = $derived(weaveWithPlaceholders())

  const myCursorId = $derived(session.weave?.cursors[identity.name]?.node_id ?? null)
  const myThread = $derived.by(() => {
    const w = session.weave
    if (!w || myCursorId === null || !w.nodes[myCursorId]) return new Set<string>()
    return new Set(threadPath(w, myCursorId))
  })
  const cursorsByNode = $derived.by(() => {
    const m = new Map<string, Cursor[]>()
    for (const c of Object.values(session.weave?.cursors ?? {})) {
      const arr = m.get(c.node_id)
      if (arr) arr.push(c)
      else m.set(c.node_id, [c])
    }
    return m
  })

  // Local view-root override: "Show parents" / "Show more" re-root the VIEW
  // without moving any cursor (coloom delta); resets when my cursor moves.
  let viewRoot = $state<string | null>(null)

  const displayRoots = $derived.by(() => {
    const w = session.weave
    if (!w) return []
    if (viewRoot !== null && w.nodes[viewRoot]) return [viewRoot]
    const node = myCursorId !== null ? w.nodes[myCursorId] : undefined
    if (node) {
      const parentId: string | undefined = node.parents[0]
      if (parentId !== undefined && w.nodes[parentId]) {
        const grandId: string | undefined = w.nodes[parentId].parents[0]
        if (grandId !== undefined && w.nodes[grandId]) {
          // window: cursor's parent — or grandparent when the cursor is a leaf
          return node.children.length > 0 ? [parentId] : [grandId]
        }
      }
    }
    return w.roots.filter((r) => w.nodes[r] !== undefined)
  })

  const displayRootParent = $derived.by(() => {
    const w = session.weave
    if (!w || displayRoots.length !== 1) return null
    const p: string | undefined = w.nodes[displayRoots[0]]?.parents[0]
    return p !== undefined && w.nodes[p] ? p : null
  })

  const searchResults = $derived.by(() => {
    const w = session.weave
    const q = ui.searchQuery.trim().toLowerCase()
    if (!w || q === '') return null
    return Object.values(w.nodes)
      .filter((n) => nodeText(n).toLowerCase().includes(q))
      .slice(0, SEARCH_CAP)
  })

  // ------------------------------------------------------------- autoscroll
  // Pointer-suppression rule: never auto-scroll while the pointer is inside.

  function scrollToNode(id: string) {
    if (pointerInside || !container) return false
    const el = container.querySelector(`[data-node-id="${CSS.escape(id)}"]`)
    if (!el) return false
    el.scrollIntoView({ block: 'nearest' })
    return true
  }

  let lastCursorSeen: string | null | undefined = undefined
  $effect(() => {
    const c = myCursorId
    const w = session.weave
    if (c === lastCursorSeen) return
    lastCursorSeen = c
    viewRoot = null // re-center the window on cursor moves
    if (!w || c === null) return
    // force-open the whole thread through my cursor (spec shared/mod.rs:494)
    untrack(() => {
      for (const id of threadPath(w, c)) collapsed.delete(id)
    })
    requestAnimationFrame(() => scrollToNode(c))
  })

  let handledChangedAt = 0
  $effect(() => {
    void session.weave // re-attempt once the refetched weave renders the row
    const at = session.changedAt
    const id = session.changedNodeId
    if (id === null || at === handledChangedAt) return
    requestAnimationFrame(() => {
      if (pointerInside) {
        handledChangedAt = at // suppressed, don't retry later
        return
      }
      if (scrollToNode(id)) handledChangedAt = at
    })
  })

  // ---------------------------------------------------------------- actions

  function hasMod(e: MouseEvent): boolean {
    return e.ctrlKey || e.metaKey || e.shiftKey || e.altKey
  }

  function onGenerate(node: WeaveNode, moveCursor: boolean) {
    collapsed.delete(node.id) // generating opens the node
    void generateAt(node.id, { moveCursor })
  }

  function onAddChild(node: WeaveNode, moveCursor: boolean) {
    collapsed.delete(node.id) // adding a child opens the parent
    void createNode('', { parentId: node.id, moveCursor })
  }

  function onRowClick(node: WeaveNode, fromSearch: boolean) {
    void moveMyCursor(node.id)
    if (fromSearch) sendViewCommand('center-node', node.id)
  }

  function singleTokenProb(node: WeaveNode): string | null {
    if (node.content.type !== 'tokens' || node.content.tokens.length !== 1) return null
    const lp = node.content.tokens[0].logprob
    if (lp == null) return null
    return `${(Math.exp(lp) * 100).toFixed(1)}%`
  }

  function rowTitle(node: WeaveNode): string {
    const lines = [creatorLabel(node.creator)]
    const gp = genParams(node)
    if (gp) {
      lines.push(
        Object.entries(gp)
          .map(([k, v]) => `${k}=${v}`)
          .join('  '),
      )
    }
    if (node.content.type === 'tokens' && node.content.tokens.length === 1) {
      const t = node.content.tokens[0]
      if (t.logprob != null) {
        lines.push(
          `${JSON.stringify(t.text)}  p=${(Math.exp(t.logprob) * 100).toFixed(1)}%  logprob=${t.logprob.toFixed(3)}`,
        )
      }
    }
    lines.push(new Date(node.created).toLocaleString())
    return lines.join('\n')
  }
</script>

{#snippet rowLabel(node: WeaveNode)}
  {@const text = nodeText(node)}
  <span class="label" style:color={creatorTextColor(node.creator)} title={rowTitle(node)}>
    {#if text === ''}<span class="notext">no text</span>{:else if node.content.type === 'tokens'}{#each node.content.tokens.slice(0, TOKEN_RENDER_CAP) as t, i (i)}<span
          style:opacity={tokenOpacity(t)}>{t.text}</span
        >{/each}{:else}{text}{/if}
  </span>
{/snippet}

{#snippet row(node: WeaveNode, depth: number, fromSearch: boolean)}
  {@const hovered = session.hoveredNodeId === node.id}
  {@const cursorsHere = cursorsByNode.get(node.id) ?? []}
  {@const prob = singleTokenProb(node)}
  {@const pending = node.metadata.gen_pending === true}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div
    class="row"
    class:hovered
    class:pending
    class:on-thread={myThread.has(node.id)}
    class:is-cursor={myCursorId === node.id}
    data-node-id={node.id}
    style:--row-color={creatorColor(node.creator)}
    style:padding-left={`${4 + depth * 14}px`}
    onpointerenter={() => (session.hoveredNodeId = node.id)}
    onpointerleave={() => {
      if (session.hoveredNodeId === node.id) session.hoveredNodeId = null
    }}
    onclick={() => {
      if (!pending) onRowClick(node, fromSearch) // placeholders are not targets
    }}
    oncontextmenu={(e) => {
      e.preventDefault()
      if (!pending) openContextMenu(node.id, e.clientX, e.clientY)
    }}
  >
    {#if !fromSearch && node.children.length > 0}
      <button
        class="tri"
        title={collapsed.has(node.id) ? 'expand' : 'collapse'}
        onclick={(e) => {
          e.stopPropagation()
          toggleCollapsed(node.id)
        }}>{collapsed.has(node.id) ? '▸' : '▾'}</button
      >
    {:else if !fromSearch}
      <span class="tri-spacer"></span>
    {/if}
    {#each cursorsHere as c (c.name)}
      <span class="cdot" style:background={cursorColor(c.name)} title={c.name}></span>
    {/each}
    {@render rowLabel(node)}
    <span class="right">
      {#if hovered && !pending}
        <span class="strip">
          <button
            title="generate completions (middle/mod-click: also move my cursor)"
            onclick={(e) => {
              e.stopPropagation()
              onGenerate(node, hasMod(e))
            }}
            onauxclick={(e) => {
              if (e.button !== 1) return
              e.stopPropagation()
              e.preventDefault()
              onGenerate(node, true)
            }}
            onmousedown={(e) => {
              if (e.button === 1) e.preventDefault()
            }}>✦</button
          >
          <button
            title="add empty child (middle/mod-click: also move my cursor)"
            onclick={(e) => {
              e.stopPropagation()
              onAddChild(node, hasMod(e))
            }}
            onauxclick={(e) => {
              if (e.button !== 1) return
              e.stopPropagation()
              e.preventDefault()
              onAddChild(node, true)
            }}
            onmousedown={(e) => {
              if (e.button === 1) e.preventDefault()
            }}>＋</button
          >
          <button
            title={node.bookmarked ? 'remove bookmark' : 'bookmark node'}
            onclick={(e) => {
              e.stopPropagation()
              void toggleBookmark(node.id)
            }}>{node.bookmarked ? '★' : '☆'}</button
          >
          <button
            class="del"
            title="delete node (and its subtree)"
            onclick={(e) => {
              e.stopPropagation()
              void deleteNode(node.id)
            }}>✕</button
          >
        </span>
      {:else}
        {#if prob !== null}<span class="prob">{prob}</span>{/if}
        {#if node.bookmarked}<span class="bm" title="bookmarked">★</span>{/if}
      {/if}
    </span>
  </div>
{/snippet}

{#snippet subtree(id: string, depth: number)}
  {@const node = weaveAug?.nodes[id]}
  {#if node}
    {@render row(node, depth, false)}
    {#if node.children.length > 0 && !collapsed.has(id)}
      {#if depth >= MAX_DEPTH}
        <button
          class="pseudo"
          style:padding-left={`${4 + (depth + 1) * 14}px`}
          onclick={() => (viewRoot = id)}
          onpointerenter={() => (session.hoveredNodeId = node.children[0] ?? null)}
          onpointerleave={() => {
            if (session.hoveredNodeId === node.children[0]) session.hoveredNodeId = null
          }}>⋯ show more</button
        >
      {:else}
        {#each node.children as child (child)}
          {@render subtree(child, depth + 1)}
        {/each}
      {/if}
    {/if}
  {/if}
{/snippet}

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  class="pane"
  bind:this={container}
  style:--my-cursor-color={cursorColor(identity.name)}
  onpointerenter={() => (pointerInside = true)}
  onpointerleave={() => (pointerInside = false)}
>
  <div class="search">
    <input
      data-search-input
      placeholder="search nodes (/)"
      bind:value={ui.searchQuery}
    />
    {#if ui.searchQuery !== ''}
      <button class="clear" title="clear search" onclick={() => (ui.searchQuery = '')}
        >✕</button
      >
    {/if}
  </div>

  {#if session.weave}
    {#if searchResults !== null}
      {#if searchResults.length === 0}
        <div class="empty">no nodes match “{ui.searchQuery.trim()}”</div>
      {:else}
        {#each searchResults as node (node.id)}
          {@render row(node, 0, true)}
        {/each}
        {#if searchResults.length === SEARCH_CAP}
          <div class="empty">…showing first {SEARCH_CAP} matches</div>
        {/if}
      {/if}
    {:else if Object.keys(session.weave.nodes).length === 0}
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <div
        class="row empty-row"
        class:hovered={session.hoveredNodeId === '__empty__'}
        onpointerenter={() => (session.hoveredNodeId = '__empty__')}
        onpointerleave={() => {
          if (session.hoveredNodeId === '__empty__') session.hoveredNodeId = null
        }}
      >
        <span class="label notext">no nodes</span>
        <span class="right">
          {#if session.hoveredNodeId === '__empty__'}
            <span class="strip">
              <button
                title="add a root node"
                onclick={() => void createNode('', { parentId: null, moveCursor: true })}
                >＋</button
              >
            </span>
          {/if}
        </span>
      </div>
    {:else}
      {#if displayRootParent !== null}
        <button class="pseudo" onclick={() => (viewRoot = displayRootParent)}
          >↑ show parents</button
        >
      {/if}
      {#each displayRoots as r (r)}
        {@render subtree(r, 0)}
      {/each}
    {/if}
  {/if}
</div>

<style>
  .pane {
    display: flex;
    flex-direction: column;
    min-height: 100%;
    font-size: var(--fs-ui);
  }
  .search {
    position: sticky;
    top: 0;
    z-index: 2;
    display: flex;
    gap: 4px;
    padding: 5px;
    background: var(--bg);
    border-bottom: 1px solid var(--border);
  }
  .search input {
    flex: 1;
    min-width: 0;
    font-size: var(--fs-ui);
    padding: 3px 7px;
  }
  .search .clear {
    font-size: var(--fs-small);
    padding: 0 7px;
  }

  .row {
    display: flex;
    align-items: center;
    gap: 4px;
    min-height: 24px;
    padding: 3px 6px 3px 4px;
    cursor: pointer;
    border-top: 1px solid color-mix(in srgb, var(--border) 40%, transparent);
  }
  .row.hovered {
    background: color-mix(in srgb, var(--text) 8%, transparent);
  }
  .row.on-thread {
    background: color-mix(in srgb, var(--row-color) 16%, transparent);
  }
  .row.on-thread.hovered {
    background: color-mix(in srgb, var(--row-color) 28%, transparent);
  }
  .row.is-cursor {
    box-shadow: inset 0 0 0 1px var(--my-cursor-color);
  }
  .row.pending {
    /* in-flight generation placeholder: dimmed pulse, not a click target */
    cursor: default;
  }
  .row.pending .label {
    font-style: italic;
    animation: gen-pulse 1.2s ease-in-out infinite;
  }
  @keyframes gen-pulse {
    0%,
    100% {
      opacity: 0.35;
    }
    50% {
      opacity: 0.85;
    }
  }

  .tri {
    flex-shrink: 0;
    width: 14px;
    padding: 0;
    background: none;
    border: none;
    color: var(--text-dim);
    font-size: var(--fs-tiny);
    line-height: 1;
    cursor: pointer;
  }
  .tri-spacer {
    flex-shrink: 0;
    width: 14px;
  }
  .cdot {
    flex-shrink: 0;
    width: 7px;
    height: 7px;
    border-radius: 50%;
  }
  .label {
    flex: 1;
    min-width: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    font-family: var(--mono);
    font-size: var(--fs-small);
  }
  .notext {
    color: var(--text-dim);
    font-style: italic;
    font-family: inherit;
  }
  .right {
    display: flex;
    align-items: center;
    gap: 3px;
    flex-shrink: 0;
  }
  .strip {
    display: flex;
    gap: 2px;
  }
  .strip button {
    padding: 1px 6px;
    font-size: var(--fs-small);
    line-height: 1.4;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 4px;
  }
  .strip button.del:hover {
    border-color: var(--danger);
    color: var(--danger);
  }
  .prob {
    color: var(--text-dim);
    font-size: var(--fs-tiny);
    font-family: var(--mono);
  }
  .bm {
    color: var(--accent);
    font-size: var(--fs-small);
  }

  .pseudo {
    display: block;
    width: 100%;
    text-align: left;
    padding: 4px 6px;
    background: none;
    border: none;
    border-radius: 0;
    border-top: 1px solid color-mix(in srgb, var(--border) 40%, transparent);
    color: var(--text-dim);
    font-size: var(--fs-small);
    cursor: pointer;
  }
  .pseudo:hover {
    background: color-mix(in srgb, var(--text) 8%, transparent);
    color: var(--text);
  }
  .empty {
    padding: 0.6rem;
    color: var(--text-dim);
    font-size: var(--fs-small);
  }
</style>
