<script lang="ts">
  // Flat list (ui-specs/lists.md §8, coloom deltas applied): all children of
  // MY cursor node as rows (weave roots when I have no cursor), with the
  // cursor's parent at the top for going back up a level.
  import {
    creatorColor,
    creatorLabel,
    creatorTextColor,
    cursorColor,
    tokenOpacity,
  } from './colors'
  import {
    createNode,
    deleteNode,
    generateAt,
    identity,
    moveMyCursor,
    openContextMenu,
    session,
    toggleBookmark,
  } from './state.svelte'
  import type { Cursor, WeaveNode } from './types'
  import { genParams, nodeText } from './types'

  const TOKEN_RENDER_CAP = 100

  let container = $state<HTMLDivElement | null>(null)
  // plain (non-reactive) on purpose: read only inside callbacks/effects
  let pointerInside = false

  const myCursorId = $derived(session.weave?.cursors[identity.name]?.node_id ?? null)
  const cursorNode = $derived.by(() => {
    const w = session.weave
    if (!w || myCursorId === null) return undefined
    return w.nodes[myCursorId]
  })
  // the row for going back up: my cursor's parent (silently absent at a root)
  const parentNode = $derived.by(() => {
    const w = session.weave
    const p: string | undefined = cursorNode?.parents[0]
    if (!w || p === undefined) return undefined
    return w.nodes[p]
  })
  const items = $derived.by(() => {
    const w = session.weave
    if (!w) return []
    const ids = cursorNode ? cursorNode.children : w.roots
    // silently skip missing ids (races with concurrent deletes)
    return ids.map((id) => w.nodes[id]).filter((n): n is WeaveNode => n !== undefined)
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
    if (c === lastCursorSeen) return
    lastCursorSeen = c
    if (c === null) return
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

{#snippet row(node: WeaveNode, isParentRow: boolean)}
  {@const hovered = session.hoveredNodeId === node.id}
  {@const cursorsHere = cursorsByNode.get(node.id) ?? []}
  {@const prob = singleTokenProb(node)}
  {@const text = nodeText(node)}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div
    class="row"
    class:hovered
    class:is-cursor={myCursorId === node.id}
    class:parent-row={isParentRow}
    data-node-id={node.id}
    style:--row-color={creatorColor(node.creator)}
    onpointerenter={() => (session.hoveredNodeId = node.id)}
    onpointerleave={() => {
      if (session.hoveredNodeId === node.id) session.hoveredNodeId = null
    }}
    onclick={() => void moveMyCursor(node.id)}
    oncontextmenu={(e) => {
      e.preventDefault()
      openContextMenu(node.id, e.clientX, e.clientY)
    }}
  >
    {#if isParentRow}<span class="up" title="move my cursor back up to the parent"
        >↑</span
      >{/if}
    {#each cursorsHere as c (c.name)}
      <span class="cdot" style:background={cursorColor(c.name)} title={c.name}></span>
    {/each}
    <span class="label" style:color={creatorTextColor(node.creator)} title={rowTitle(node)}>
      {#if text === ''}<span class="notext">no text</span>{:else if node.content.type === 'tokens'}{#each node.content.tokens.slice(0, TOKEN_RENDER_CAP) as t, i (i)}<span
            style:opacity={tokenOpacity(t)}>{t.text}</span
          >{/each}{:else}{text}{/if}
    </span>
    <span class="right">
      {#if hovered}
        <span class="strip">
          <button
            title="generate completions (middle/mod-click: also move my cursor)"
            onclick={(e) => {
              e.stopPropagation()
              void generateAt(node.id, { moveCursor: hasMod(e) })
            }}
            onauxclick={(e) => {
              if (e.button !== 1) return
              e.stopPropagation()
              e.preventDefault()
              void generateAt(node.id, { moveCursor: true })
            }}
            onmousedown={(e) => {
              if (e.button === 1) e.preventDefault()
            }}>✦</button
          >
          <button
            title="add empty child (middle/mod-click: also move my cursor)"
            onclick={(e) => {
              e.stopPropagation()
              void createNode('', { parentId: node.id, moveCursor: hasMod(e) })
            }}
            onauxclick={(e) => {
              if (e.button !== 1) return
              e.stopPropagation()
              e.preventDefault()
              void createNode('', { parentId: node.id, moveCursor: true })
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

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  class="pane"
  bind:this={container}
  style:--my-cursor-color={cursorColor(identity.name)}
  onpointerenter={() => (pointerInside = true)}
  onpointerleave={() => (pointerInside = false)}
>
  {#if session.weave}
    <div class="heading">
      {#if cursorNode}children of my cursor{:else}roots (I have no cursor yet){/if}
    </div>
    {#if parentNode}
      {@render row(parentNode, true)}
    {/if}
    {#each items as node (node.id)}
      {@render row(node, false)}
    {/each}
    {#if items.length === 0}
      <div class="empty">
        {#if cursorNode}
          my cursor node has no children — generate (✦ on a row, or g) or add one
        {:else}
          empty weave — add a root from the tree view or type in the text pane
        {/if}
      </div>
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
  .heading {
    position: sticky;
    top: 0;
    z-index: 2;
    padding: 5px 6px;
    background: var(--bg);
    border-bottom: 1px solid var(--border);
    color: var(--text-dim);
    font-size: var(--fs-tiny);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .row {
    display: flex;
    align-items: center;
    gap: 4px;
    min-height: 24px;
    padding: 3px 6px 3px 6px;
    cursor: pointer;
    border-top: 1px solid color-mix(in srgb, var(--border) 40%, transparent);
  }
  .row.hovered {
    background: color-mix(in srgb, var(--text) 8%, transparent);
  }
  .row.is-cursor {
    box-shadow: inset 0 0 0 1px var(--my-cursor-color);
    background: color-mix(in srgb, var(--row-color) 16%, transparent);
  }
  .row.parent-row {
    border-top: none;
    border-bottom: 1px solid var(--border);
  }
  .up {
    flex-shrink: 0;
    color: var(--text-dim);
    font-size: var(--fs-small);
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
  .empty {
    padding: 0.6rem;
    color: var(--text-dim);
    font-size: var(--fs-small);
  }
</style>
