<script lang="ts">
  import ActivityFeed from './ActivityFeed.svelte'
  import BookmarksPane from './BookmarksPane.svelte'
  import Canvas from './Canvas.svelte'
  import ContextMenu from './ContextMenu.svelte'
  import FlatList from './FlatList.svelte'
  import GenControls from './GenControls.svelte'
  import GraphMinimap from './GraphMinimap.svelte'
  import InfoPane from './InfoPane.svelte'
  import KeybindingsDialog from './KeybindingsDialog.svelte'
  import Splitter from './Splitter.svelte'
  import TextPane from './TextPane.svelte'
  import TreeList from './TreeList.svelte'
  import { initKeyboard } from './keyboard.svelte'
  import {
    closeWeave,
    identity,
    openWeave,
    persistUi,
    sendViewCommand,
    session,
    ui,
  } from './state.svelte'
  import type { CenterTab, SidebarTab } from './state.svelte'

  let { weaveId }: { weaveId: string } = $props()

  $effect(() => {
    void openWeave(weaveId)
    return () => closeWeave()
  })

  $effect(() => initKeyboard())

  let showKeybindings = $state(false)

  const SIDEBAR_TABS: { key: SidebarTab; label: string }[] = [
    { key: 'tree', label: 'tree' },
    { key: 'children', label: 'children' },
    { key: 'bookmarks', label: 'marks' },
    { key: 'activity', label: 'activity' },
    { key: 'info', label: 'info' },
  ]
  const CENTER_TABS: { key: CenterTab; label: string }[] = [
    { key: 'canvas', label: 'canvas' },
    { key: 'graph', label: 'graph' },
  ]

  const stats = $derived.by(() => {
    const w = session.weave
    if (!w) return null
    const nodes = Object.values(w.nodes)
    return {
      nodes: nodes.length,
      bookmarked: w.bookmarks.length,
      cursors: Object.keys(w.cursors).length,
      humans: nodes.filter((n) => n.creator.type === 'human').length,
      models: nodes.filter((n) => n.creator.type === 'model').length,
    }
  })

  function clampWidth(v: number, min: number, max: number) {
    return Math.min(Math.max(v, min), max)
  }
</script>

<header>
  <a class="back" href="#/">← weaves</a>
  <h1>{session.weave?.title ?? '…'}</h1>
  <div class="spacer"></div>
  {#if session.inflight > 0 || session.activeGens.length > 0}
    <span class="inflight" title={session.activeGens.map((g) => `${g.requester ?? '?'} @ ${g.node_id.slice(0, 6)} (${g.preset ?? 'default'})`).join('\n')}>
      ⟳ {session.activeGens.length || session.inflight} weaving{session.activeGens.length
        ? `: ${[...new Set(session.activeGens.map((g) => g.requester ?? '?'))].join(', ')}`
        : '…'}
    </span>
  {/if}
  <span class="conn" data-state={session.connection} title={session.connection}></span>
  <span class="identity" title="your profile (switch from the weaves page)">
    I am <b>{identity.name}</b>
  </span>
  <button
    class="keys-btn"
    onclick={() => (showKeybindings = true)}
    title="keyboard shortcuts"
    data-testid="kb-open">keybindings</button
  >
</header>

{#if showKeybindings}
  <KeybindingsDialog onclose={() => (showKeybindings = false)} />
{/if}

{#if session.loadError}
  <div class="center-msg">
    <p class="error">{session.loadError}</p>
    <a href="#/">back to weaves</a>
  </div>
{:else if session.loading || !session.weave}
  <div class="center-msg"><p class="dim">loading weave…</p></div>
{:else}
  <div class="panes">
    <div class="sidebar" style:width={`${ui.sidebarWidth}px`}>
      <nav class="tabs">
        {#each SIDEBAR_TABS as t (t.key)}
          <button
            class:active={ui.sidebarTab === t.key}
            onclick={() => {
              ui.sidebarTab = t.key
              persistUi()
            }}>{t.label}</button
          >
        {/each}
      </nav>
      <div class="tab-body">
        {#if ui.sidebarTab === 'tree'}
          <TreeList />
        {:else if ui.sidebarTab === 'children'}
          <FlatList />
        {:else if ui.sidebarTab === 'bookmarks'}
          <BookmarksPane />
        {:else if ui.sidebarTab === 'activity'}
          <ActivityFeed />
        {:else}
          <InfoPane />
        {/if}
      </div>
    </div>
    <Splitter
      onresize={(dx) => (ui.sidebarWidth = clampWidth(ui.sidebarWidth + dx, 180, 560))}
      ondone={persistUi}
    />
    <div class="center">
      <nav class="tabs center-tabs">
        {#each CENTER_TABS as t (t.key)}
          <button
            class:active={ui.centerTab === t.key}
            onclick={() => {
              ui.centerTab = t.key
              persistUi()
            }}>{t.label}</button
          >
        {/each}
        <div class="spacer"></div>
        <button onclick={() => sendViewCommand('fit-cursor')} title="center on my cursor (ctrl+9)">⌖ cursor</button>
        <button onclick={() => sendViewCommand('fit-weave')} title="fit whole weave (ctrl+0)">⛶ weave</button>
      </nav>
      <div class="center-body">
        {#if ui.centerTab === 'canvas'}
          <Canvas />
        {:else}
          <GraphMinimap />
        {/if}
      </div>
    </div>
    <Splitter
      onresize={(dx) => (ui.textWidth = clampWidth(ui.textWidth - dx, 280, 800))}
      ondone={persistUi}
    />
    <div class="side-pane" style:width={`${ui.textWidth}px`}>
      <GenControls />
      <TextPane />
    </div>
  </div>
  <footer>
    {#if stats}
      <span>{stats.nodes} nodes · {stats.bookmarked} bookmarked · {stats.cursors} cursors</span>
      <span class="attribution">{stats.humans} human · {stats.models} model</span>
    {/if}
    <div class="spacer"></div>
    <span class="dim">{session.weave.id.slice(0, 8)}</span>
  </footer>
  <ContextMenu />
{/if}

<style>
  header {
    display: flex;
    align-items: center;
    gap: 1rem;
    padding: 0.45rem 1rem;
    border-bottom: 1px solid var(--border);
    background: var(--bg-raised);
  }
  header h1 {
    font-size: 1.1rem;
    margin: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .back {
    color: var(--text-dim);
    text-decoration: none;
    white-space: nowrap;
  }
  .spacer {
    flex: 1;
  }
  .inflight {
    color: var(--accent);
    font-size: var(--fs-ui);
    white-space: nowrap;
  }
  .conn {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .conn[data-state='live'] {
    background: #5fbf77;
  }
  .conn[data-state='connecting'],
  .conn[data-state='reconnecting'] {
    background: var(--accent);
    animation: pulse 1s infinite alternate;
  }
  @keyframes pulse {
    to {
      opacity: 0.3;
    }
  }
  .identity {
    color: var(--text-dim);
    font-size: var(--fs-ui);
    display: flex;
    align-items: center;
    gap: 0.4rem;
    white-space: nowrap;
  }

  .panes {
    flex: 1;
    display: flex;
    min-height: 0;
  }
  .sidebar {
    display: flex;
    flex-direction: column;
    min-height: 0;
    flex-shrink: 0;
  }
  .center {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
  }
  .center-body {
    flex: 1;
    position: relative;
    min-height: 0;
  }
  .side-pane {
    display: flex;
    flex-direction: column;
    min-height: 0;
    flex-shrink: 0;
  }

  .tabs {
    display: flex;
    flex-wrap: wrap; /* never overflow under the splitter (tabs stay clickable) */
    gap: 0.15rem;
    padding: 0.3rem 0.4rem;
    border-bottom: 1px solid var(--border);
    background: var(--bg-raised);
    align-items: center;
  }
  .tabs button {
    font-size: var(--fs-ui);
    padding: 0.25rem 0.5rem;
    border: 1px solid transparent;
    background: none;
    color: var(--text-dim);
  }
  .tabs button.active {
    color: var(--text);
    border-color: var(--border);
    background: var(--bg-card);
  }
  .tab-body {
    flex: 1;
    overflow-y: auto;
    min-height: 0;
  }

  footer {
    display: flex;
    gap: 1rem;
    padding: 0.3rem 1rem;
    border-top: 1px solid var(--border);
    background: var(--bg-raised);
    font-size: var(--fs-small);
    color: var(--text-dim);
  }
  .center-msg {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 0.5rem;
  }
  .error {
    color: var(--danger);
  }
  .dim {
    color: var(--text-dim);
  }
</style>
