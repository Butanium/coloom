<script lang="ts">
  // Activity feed: a WHO-did-WHAT timeline rendered newest-first. Read-only
  // lens. Two scopes: "weave" = this weave's events (session.events),
  // "all" = events across every weave (globalevents.svelte.ts — own WS).
  // Clicking an entry EXPANDS it in place (full payload, who/when, jump
  // buttons); nothing here ever moves a cursor.
  import { creatorTextColor, cursorColor } from './colors'
  import { globalFeed, startGlobalFeed, stopGlobalFeed } from './globalevents.svelte'
  import { sendViewCommand, session } from './state.svelte'
  import type { WeaveEvent } from './types'
  import { nodeText } from './types'

  const RENDER_CAP = 200

  let scope = $state<'weave' | 'global'>('weave')
  let expandedSeq = $state<number | null>(null)

  // the global WS only lives while this pane is showing the global scope
  $effect(() => {
    if (scope !== 'global') return
    startGlobalFeed()
    return () => stopGlobalFeed()
  })

  interface Entry {
    seq: number
    created: string
    type: string
    payload: Record<string, unknown>
    weaveId: string
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
      type: e.type,
      payload: p,
      weaveId: e.weave_id,
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
      case 'node_restored': {
        const ids = (p.node_ids as string[] | undefined) ?? []
        base.verb =
          ids.length === 1
            ? `restored node #${ids[0]?.slice(0, 6)}`
            : `restored ${ids.length} nodes`
        base.nodeId = (ids[0] as string) ?? null
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
      case 'template_created':
      case 'template_updated':
      case 'template_deleted':
      case 'generator_created':
      case 'generator_updated':
      case 'generator_deleted': {
        // global (non-weave) change feed: "clément edited template gpt4-base"
        const [noun, action] = e.type.split('_')
        base.actor = cursorActor(p.by)
        const verb =
          action === 'created' ? 'created' : action === 'updated' ? 'edited' : 'deleted'
        base.verb = `${verb} ${noun} ${p.name ?? p.id ?? '?'}`
        base.danger = action === 'deleted'
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
    (scope === 'global' ? globalFeed.events : session.events)
      .filter((e, i, evs) => keepEvent(e, evs[i + 1]))
      .slice(-RENDER_CAP)
      .map(describe)
      .reverse(),
  )

  /** Weave label for an entry, shown in the global scope when the event isn't
   * from the open weave ('' = the global template/generator scope). */
  function weaveLabel(entry: Entry): string | null {
    if (scope !== 'global' || entry.weaveId === '') return null
    if (entry.weaveId === session.weave?.id) return null
    return globalFeed.titles[entry.weaveId] ?? `#${entry.weaveId.slice(0, 8)}`
  }

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
    return (
      entry.nodeId !== null &&
      entry.weaveId === session.weave?.id &&
      session.weave?.nodes[entry.nodeId] !== undefined
    )
  }

  function hover(entry: Entry, on: boolean) {
    if (!targetExists(entry)) return
    if (on) session.hoveredNodeId = entry.nodeId
    else if (session.hoveredNodeId === entry.nodeId) session.hoveredNodeId = null
  }

  function toggleExpand(entry: Entry) {
    expandedSeq = expandedSeq === entry.seq ? null : entry.seq
  }

  function fmtValue(v: unknown): string {
    return typeof v === 'string' ? v : JSON.stringify(v)
  }
</script>

<div class="pane" data-testid="activity-pane">
  <div class="scope" role="group" aria-label="activity scope">
    <button
      class:active={scope === 'weave'}
      onclick={() => (scope = 'weave')}
      data-testid="activity-scope-weave">this weave</button
    >
    <button
      class:active={scope === 'global'}
      onclick={() => (scope = 'global')}
      data-testid="activity-scope-global">all weaves</button
    >
    {#if scope === 'global' && globalFeed.status === 'error'}
      <span class="feed-error" title={globalFeed.error}>feed error</span>
    {/if}
  </div>
  {#if entries.length === 0}
    <p class="empty">
      {scope === 'global'
        ? 'Nothing yet — activity across all weaves will show up here live.'
        : 'Nothing yet — activity in this weave will show up here live.'}
    </p>
  {:else}
    <ul>
      {#each entries as entry (entry.seq)}
        {@const expanded = expandedSeq === entry.seq}
        {@const wlabel = weaveLabel(entry)}
        <li
          class:danger={entry.danger}
          class:lookhere={entry.lookHere}
          class:expanded
          class:hovered={entry.nodeId !== null && session.hoveredNodeId === entry.nodeId}
          onpointerenter={() => hover(entry, true)}
          onpointerleave={() => hover(entry, false)}
        >
          <button
            class="entry"
            onclick={() => toggleExpand(entry)}
            title={expanded ? 'collapse' : 'show full details'}
            data-testid={`activity-entry-${entry.seq}`}
          >
            <span class="body">
              {#if wlabel !== null}<span class="weave-tag">{wlabel}</span>{/if}
              {#if entry.actor}<span class="actor" style:color={entry.actor.color}
                  >{entry.actor.label}</span
              >{/if}
              <span class="verb">{entry.verb}</span>
              {#if entry.snippet !== null}<span class="snippet">{entry.snippet}</span>{/if}
            </span>
            <span class="when" title={new Date(entry.created).toLocaleString()}
              >{relTime(entry.created)}</span
            >
          </button>
          {#if expanded}
            <div class="details" data-testid={`activity-details-${entry.seq}`}>
              <div class="kv">
                <span class="k">event</span><span class="v">{entry.type} (#{entry.seq})</span>
              </div>
              <div class="kv">
                <span class="k">when</span>
                <span class="v">{new Date(entry.created).toLocaleString()}</span>
              </div>
              {#if entry.actor}
                <div class="kv"><span class="k">who</span><span class="v">{entry.actor.label}</span></div>
              {/if}
              {#if wlabel !== null}
                <div class="kv"><span class="k">weave</span><span class="v">{wlabel}</span></div>
              {/if}
              {#each Object.entries(entry.payload) as [k, v] (k)}
                <div class="kv"><span class="k">{k}</span><span class="v">{fmtValue(v)}</span></div>
              {/each}
              <div class="actions">
                {#if targetExists(entry)}
                  <button
                    onclick={() => sendViewCommand('center-node', entry.nodeId)}
                    data-testid={`activity-center-${entry.seq}`}>center view on node</button
                  >
                {/if}
                {#if scope === 'global' && entry.weaveId !== '' && entry.weaveId !== session.weave?.id}
                  <a class="open-weave" href={`#/w/${entry.weaveId}`}>open weave</a>
                {/if}
              </div>
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
    overflow-x: hidden; /* NEVER scroll horizontally: long content wraps */
    max-width: 100%;
  }
  .scope {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    padding: 0.2rem 0.5rem 0.4rem;
    border-bottom: 1px solid color-mix(in srgb, var(--border) 45%, transparent);
  }
  .scope button {
    font-size: var(--fs-tiny);
    padding: 0.1rem 0.5rem;
    color: var(--text-dim);
  }
  .scope button.active {
    color: var(--text);
    border-color: var(--accent);
  }
  .feed-error {
    margin-left: auto;
    color: var(--danger);
    font-size: var(--fs-tiny);
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
    border: none;
    background: none;
    cursor: pointer;
  }
  li.hovered,
  li:not(.expanded):hover {
    background: var(--bg-card);
  }
  li.expanded {
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
  li.expanded .body {
    white-space: normal; /* expanded: wrap instead of truncating */
    overflow-wrap: anywhere;
  }
  .weave-tag {
    font-size: var(--fs-tiny);
    color: var(--accent);
    border: 1px solid color-mix(in srgb, var(--accent) 45%, transparent);
    border-radius: 3px;
    padding: 0 0.25rem;
    margin-right: 0.15rem;
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
  .details {
    padding: 0.2rem 0.5rem 0.5rem;
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    font-size: var(--fs-tiny);
  }
  .kv {
    display: flex;
    gap: 0.5rem;
    align-items: baseline;
    min-width: 0;
  }
  .kv .k {
    flex-shrink: 0;
    width: 5.5rem;
    color: var(--text-dim);
  }
  .kv .v {
    flex: 1;
    min-width: 0;
    font-family: var(--mono);
    color: var(--text);
    overflow-wrap: anywhere; /* long ids/payloads wrap, never scroll */
    word-break: break-word;
    white-space: pre-wrap;
  }
  .actions {
    display: flex;
    gap: 0.4rem;
    margin-top: 0.3rem;
  }
  .actions button,
  .actions .open-weave {
    font-size: var(--fs-tiny);
    padding: 0.1rem 0.5rem;
  }
  .open-weave {
    border: 1px solid var(--border);
    border-radius: 4px;
    color: var(--accent);
    text-decoration: none;
  }
</style>
