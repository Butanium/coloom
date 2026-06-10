<script lang="ts">
  // Bookmarks pane: every bookmarked node as a clickable row, in bookmark
  // order. Doubles as a human↔agent handoff surface: bookmark a node, your
  // co-weaver clicks it and their view (and cursor) jumps there.
  import { creatorTextColor } from './colors'
  import {
    moveMyCursor,
    openContextMenu,
    sendViewCommand,
    session,
    toggleBookmark,
  } from './state.svelte'
  import type { WeaveNode } from './types'
  import { nodeText } from './types'

  // missing ids (bookmark/delete races) are silently skipped, like Tapestry
  const rows = $derived.by<WeaveNode[]>(() => {
    const w = session.weave
    if (!w) return []
    return w.bookmarks.map((id) => w.nodes[id]).filter((n) => n !== undefined)
  })

  // ticking clock for relative timestamps
  let now = $state(Date.now())
  $effect(() => {
    const t = setInterval(() => (now = Date.now()), 30_000)
    return () => clearInterval(t)
  })

  function relTime(iso: string): string {
    const s = Math.max(0, (now - Date.parse(iso)) / 1000)
    if (s < 45) return 'just now'
    if (s < 3600) return `${Math.round(s / 60)}m ago`
    if (s < 86400) return `${Math.round(s / 3600)}h ago`
    return `${Math.round(s / 86400)}d ago`
  }

  function snippet(node: WeaveNode): string {
    const text = nodeText(node).trim()
    return text === '' ? '(no text)' : text
  }

  async function jumpTo(node: WeaveNode) {
    await moveMyCursor(node.id)
    sendViewCommand('center-node', node.id)
  }
</script>

<div class="pane">
  {#if rows.length === 0}
    <p class="empty">
      No bookmarks yet. Bookmark a node (right-click it in any view) and it shows
      up here for everyone — a shared "look at this" shelf for handing branches
      between weavers.
    </p>
  {:else}
    <ul>
      {#each rows as node (node.id)}
        <li
          class:hovered={session.hoveredNodeId === node.id}
          onpointerenter={() => (session.hoveredNodeId = node.id)}
          onpointerleave={() => {
            if (session.hoveredNodeId === node.id) session.hoveredNodeId = null
          }}
        >
          <button
            class="row"
            onclick={() => void jumpTo(node)}
            oncontextmenu={(e) => {
              e.preventDefault()
              openContextMenu(node.id, e.clientX, e.clientY)
            }}
            title="move my cursor here & center the view"
          >
            <span class="text" style:color={creatorTextColor(node.creator)}
              >{snippet(node)}</span
            >
            <span class="when" title={new Date(node.created).toLocaleString()}
              >{relTime(node.created)}</span
            >
          </button>
          <button
            class="unmark"
            onclick={(e) => {
              e.stopPropagation()
              void toggleBookmark(node.id)
            }}
            title="remove bookmark"
            aria-label="remove bookmark">✕</button
          >
        </li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .pane {
    padding: 0.25rem 0;
  }
  .empty {
    padding: 0.75rem;
    color: var(--text-dim);
    font-size: var(--fs-small);
    line-height: 1.5;
  }
  ul {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  li {
    position: relative;
    display: flex;
    align-items: stretch;
    border-bottom: 1px solid color-mix(in srgb, var(--border) 45%, transparent);
  }
  li.hovered {
    background: var(--bg-card);
  }
  .row {
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    padding: 0.4rem 0.5rem;
    border: none;
    background: none;
    text-align: left;
    cursor: pointer;
    font-size: var(--fs-ui);
  }
  .text {
    flex: 1;
    min-width: 0;
    font-family: var(--mono);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .when {
    flex-shrink: 0;
    color: var(--text-dim);
    font-size: var(--fs-small);
    white-space: nowrap;
  }
  .unmark {
    flex-shrink: 0;
    width: 1.6rem;
    border: none;
    background: none;
    color: var(--text-dim);
    font-size: var(--fs-small);
    cursor: pointer;
    opacity: 0;
  }
  li:hover .unmark,
  li.hovered .unmark {
    opacity: 1;
  }
  .unmark:hover {
    color: var(--danger);
  }
</style>
