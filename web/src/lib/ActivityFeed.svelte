<script lang="ts">
  // Activity feed: session.events rendered newest-first as a WHO-did-WHAT
  // timeline. Read-only lens — clicking an entry centers the view on its node
  // but never moves any cursor.
  import { creatorTextColor, cursorColor } from './colors'
  import { sendViewCommand, session } from './state.svelte'
  import type { WeaveEvent } from './types'
  import { nodeText } from './types'

  const RENDER_CAP = 200

  interface Entry {
    seq: number
    created: string
    actor: { label: string; color: string } | null
    verb: string
    snippet: string | null // node text excerpt, rendered monospace
    nodeId: string | null // hover/center target
    danger: boolean
    lookHere: boolean // the "A moved B's cursor" gesture
  }

  function snippetOf(nodeId: unknown): string {
    if (typeof nodeId !== 'string') return '?'
    const node = session.weave?.nodes[nodeId]
    if (!node) return `#${nodeId.slice(0, 6)}` // deleted / unknown node: id stub
    const text = nodeText(node).trim()
    return text === '' ? `#${nodeId.slice(0, 6)}` : text
  }

  function cursorActor(name: unknown): { label: string; color: string } {
    const label = typeof name === 'string' && name !== '' ? name : 'someone'
    return { label, color: cursorColor(label) }
  }

  function describe(e: WeaveEvent): Entry {
    const p = e.payload
    const base: Entry = {
      seq: e.seq,
      created: e.created,
      actor: null,
      verb: '',
      snippet: null,
      nodeId: null,
      danger: false,
      lookHere: false,
    }
    switch (e.type) {
      case 'node_added': {
        const nodeId = p.node_id as string
        const node = session.weave?.nodes[nodeId]
        if (node && node.creator.type !== 'unknown') {
          base.actor = { label: node.creator.label, color: creatorTextColor(node.creator) }
        }
        base.verb = p.parent_id ? 'added a branch under' : 'added a root'
        base.snippet = p.parent_id ? snippetOf(p.parent_id) : null
        base.nodeId = nodeId
        return base
      }
      case 'node_removed': {
        const ids = (p.node_ids as string[] | undefined) ?? []
        base.verb =
          ids.length === 1
            ? `removed node #${ids[0]?.slice(0, 6)}`
            : `removed ${ids.length} nodes`
        return base
      }
      case 'node_split': {
        base.verb = 'split'
        base.snippet = snippetOf(p.node_id)
        base.nodeId = (p.node_id as string) ?? null
        return base
      }
      case 'node_updated': {
        base.verb =
          p.bookmarked === true
            ? 'bookmarked'
            : p.bookmarked === false
              ? 'unbookmarked'
              : 'updated'
        base.snippet = snippetOf(p.node_id)
        base.nodeId = (p.node_id as string) ?? null
        return base
      }
      case 'cursor_moved': {
        const name = p.name as string
        const movedBy = p.moved_by as string | null | undefined
        base.nodeId = (p.node_id as string) ?? null
        base.snippet = snippetOf(p.node_id)
        if (movedBy && movedBy !== name) {
          // the "look here" gesture
          base.actor = cursorActor(movedBy)
          base.verb = `moved ${name}'s cursor to`
          base.lookHere = true
        } else if (movedBy === name) {
          base.actor = cursorActor(name)
          base.verb = 'moved their cursor to'
        } else {
          // server-side move (e.g. cursor refuge after a delete)
          base.verb = `${name}'s cursor moved to`
        }
        return base
      }
      case 'cursor_removed':
        base.verb = `${p.name}'s cursor was removed`
        return base
      case 'gen_started': {
        base.actor = cursorActor(p.requester)
        base.verb = 'is weaving at'
        base.snippet = snippetOf(p.node_id)
        base.nodeId = (p.node_id as string) ?? null
        return base
      }
      case 'gen_finished': {
        base.actor = cursorActor(p.requester)
        base.nodeId = (p.node_id as string) ?? null
        if (p.error) {
          base.verb = `generation failed: ${p.error}`
          base.danger = true
        } else {
          const n = (p.node_ids as string[] | undefined)?.length ?? 0
          base.verb = `done: ${n} ${n === 1 ? 'branch' : 'branches'} at`
          base.snippet = snippetOf(p.node_id)
        }
        return base
      }
      case 'weave_created':
        base.verb = `weave created: ${p.title ?? ''}`
        return base
      case 'weave_updated':
        base.verb = 'weave info updated'
        return base
      case 'weave_deleted':
        base.verb = 'weave deleted'
        return base
      default:
        base.verb = e.type
        return base
    }
  }

  // Plain navigation spams cursor_moved; show a move only when it immediately
  // precedes a real (non-cursor) event — "where they went to do that". Summons
  // (A moved B's cursor) are deliberate gestures and always show. The newest
  // moves stay hidden until a real action lands (reactive: kept retroactively).
  function keepEvent(e: WeaveEvent, next: WeaveEvent | undefined): boolean {
    if (e.type !== 'cursor_moved') return true
    const movedBy = (e.payload.moved_by as string | null | undefined) ?? null
    if (movedBy !== null && movedBy !== e.payload.name) return true // summon
    return next !== undefined && next.type !== 'cursor_moved'
  }

  const entries = $derived(
    session.events
      .filter((e, i, evs) => keepEvent(e, evs[i + 1]))
      .slice(-RENDER_CAP)
      .map(describe)
      .reverse(),
  )

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

  function targetExists(entry: Entry): boolean {
    return entry.nodeId !== null && session.weave?.nodes[entry.nodeId] !== undefined
  }

  function hover(entry: Entry, on: boolean) {
    if (!targetExists(entry)) return
    if (on) session.hoveredNodeId = entry.nodeId
    else if (session.hoveredNodeId === entry.nodeId) session.hoveredNodeId = null
  }
</script>

{#snippet entryBody(entry: Entry)}
  <span class="body">
    {#if entry.actor}<span class="actor" style:color={entry.actor.color}
        >{entry.actor.label}</span
    >{/if}
    <span class="verb">{entry.verb}</span>
    {#if entry.snippet !== null}<span class="snippet">{entry.snippet}</span>{/if}
  </span>
  <span class="when" title={new Date(entry.created).toLocaleString()}
    >{relTime(entry.created)}</span
  >
{/snippet}

<div class="pane">
  {#if entries.length === 0}
    <p class="empty">Nothing yet — activity in this weave will show up here live.</p>
  {:else}
    <ul>
      {#each entries as entry (entry.seq)}
        <li
          class:danger={entry.danger}
          class:lookhere={entry.lookHere}
          class:hovered={entry.nodeId !== null && session.hoveredNodeId === entry.nodeId}
          onpointerenter={() => hover(entry, true)}
          onpointerleave={() => hover(entry, false)}
        >
          {#if targetExists(entry)}
            <button
              class="entry"
              onclick={() => sendViewCommand('center-node', entry.nodeId)}
              title="center the view on this node"
            >
              {@render entryBody(entry)}
            </button>
          {:else}
            <div class="entry">
              {@render entryBody(entry)}
            </div>
          {/if}
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
  }
  ul {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  li {
    border-bottom: 1px solid color-mix(in srgb, var(--border) 45%, transparent);
    color: var(--text-dim);
  }
  .entry {
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    width: 100%;
    padding: 0.38rem 0.5rem;
    font-size: var(--fs-small);
    text-align: left;
    color: inherit;
  }
  button.entry {
    border: none;
    background: none;
    cursor: pointer;
  }
  li.hovered,
  li:has(button.entry):hover {
    background: var(--bg-card);
  }
  li.lookhere {
    border-left: 2px solid var(--accent);
  }
  li.danger .verb {
    color: var(--danger);
  }
  .body {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .actor {
    font-weight: 600;
  }
  .snippet {
    font-family: var(--mono);
    color: var(--text);
    background: var(--bg-raised);
    border-radius: 3px;
    padding: 0 0.25rem;
  }
  .when {
    flex-shrink: 0;
    font-size: var(--fs-tiny);
    white-space: nowrap;
  }
</style>
