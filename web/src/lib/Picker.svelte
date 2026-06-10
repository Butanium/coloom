<script lang="ts">
  import { api } from './api'
  import { getSetting, logoutProfile, profile, setSetting } from './profile.svelte'
  import { withToast } from './state.svelte'
  import type { WeaveInfo } from './types'

  let weaves = $state<WeaveInfo[] | null>(null)
  let loadError = $state<string | null>(null)
  let newTitle = $state('')
  let newFolder = $state('')
  let filter = $state('')
  // inline move-to-folder editor (one row at a time)
  let movingId = $state<string | null>(null)
  let moveDraft = $state('')
  const collapsedFolders = $state<{ list: string[] }>({
    list: getSetting('pickerCollapsed', [] as string[]),
  })

  function folderOf(w: WeaveInfo): string {
    return typeof w.metadata.folder === 'string' ? w.metadata.folder : ''
  }

  // group by metadata.folder (path-like string, e.g. "research/personas");
  // loose weaves (no folder) come first, then folders alphabetically
  const groups = $derived.by(() => {
    if (!weaves) return []
    const q = filter.trim().toLowerCase()
    const shown = weaves.filter(
      (w) =>
        !q ||
        w.title.toLowerCase().includes(q) ||
        w.description.toLowerCase().includes(q) ||
        folderOf(w).toLowerCase().includes(q),
    )
    const map = new Map<string, WeaveInfo[]>()
    for (const w of shown) {
      const f = folderOf(w)
      map.set(f, [...(map.get(f) ?? []), w])
    }
    return [...map.entries()].sort(([a], [b]) =>
      a === '' ? -1 : b === '' ? 1 : a.localeCompare(b),
    )
  })

  function toggleFolder(f: string) {
    collapsedFolders.list = collapsedFolders.list.includes(f)
      ? collapsedFolders.list.filter((x) => x !== f)
      : [...collapsedFolders.list, f]
    setSetting('pickerCollapsed', [...collapsedFolders.list])
  }

  function startMove(w: WeaveInfo) {
    movingId = w.id
    moveDraft = folderOf(w)
  }

  async function applyMove(w: WeaveInfo) {
    const folder = moveDraft.trim()
    await withToast(() =>
      api.updateWeave(w.id, { metadata: { ...w.metadata, folder } }),
    )
    movingId = null
    await refresh()
  }

  async function refresh() {
    loadError = null
    try {
      weaves = await api.listWeaves()
    } catch (e) {
      loadError = `${e}`
    }
  }

  async function create(event: SubmitEvent) {
    event.preventDefault()
    const title = newTitle.trim() || 'Untitled weave'
    const folder = newFolder.trim()
    const info = await withToast(() =>
      api.createWeave(title, '', folder ? { folder } : undefined),
    )
    if (info) {
      newTitle = ''
      location.hash = `#/w/${info.id}`
    }
  }

  async function remove(w: WeaveInfo) {
    if (!confirm(`Delete weave “${w.title}”? This removes all its nodes.`)) return
    await withToast(() => api.deleteWeave(w.id))
    await refresh()
  }

  $effect(() => {
    void refresh()
  })
</script>

<main>
  <h1>coloom</h1>
  <p class="tagline">
    a loom for human + AI co-weaving — weaving as <b>{profile.name}</b>
    <button
      class="switch"
      onclick={() => void logoutProfile()}
      data-testid="switch-profile">switch profile</button
    >
  </p>

  <form onsubmit={create}>
    <input placeholder="new weave title…" bind:value={newTitle} />
    <input
      class="folder-input"
      placeholder="folder (optional)"
      bind:value={newFolder}
      data-testid="new-weave-folder"
    />
    <button class="primary" type="submit">create</button>
  </form>

  {#if loadError}
    <p class="error">server unreachable: {loadError}</p>
    <button onclick={refresh}>retry</button>
  {:else if weaves === null}
    <p class="dim">loading weaves…</p>
  {:else if weaves.length === 0}
    <p class="dim">no weaves yet — create one above</p>
  {:else}
    <input
      class="filter"
      placeholder="filter weaves…"
      bind:value={filter}
      data-testid="weave-filter"
    />
    {#each groups as [folder, items] (folder)}
      {#if folder !== ''}
        <button
          class="folder-head"
          onclick={() => toggleFolder(folder)}
          data-testid={`folder-${folder}`}
        >
          <span class="tri">{collapsedFolders.list.includes(folder) ? '▸' : '▾'}</span>
          {folder}
          <span class="count">{items.length}</span>
        </button>
      {/if}
      {#if folder === '' || !collapsedFolders.list.includes(folder)}
        <ul class:indented={folder !== ''}>
          {#each items as w (w.id)}
            <li>
              <a href={`#/w/${w.id}`}>
                <span class="title">{w.title}</span>
                {#if w.description}<span class="desc">{w.description}</span>{/if}
                <span class="meta">{new Date(w.created).toLocaleString()} · <code>{w.id.slice(0, 8)}</code></span>
              </a>
              {#if movingId === w.id}
                <form
                  class="move"
                  onsubmit={(e) => {
                    e.preventDefault()
                    void applyMove(w)
                  }}
                >
                  <input
                    bind:value={moveDraft}
                    placeholder="folder ('' = none)"
                    data-testid={`move-input-${w.id}`}
                  />
                  <button type="submit" data-testid={`move-save-${w.id}`}>ok</button>
                  <button type="button" onclick={() => (movingId = null)}>cancel</button>
                </form>
              {:else}
                <button onclick={() => startMove(w)} data-testid={`move-${w.id}`}>
                  folder
                </button>
              {/if}
              <button class="danger" onclick={() => remove(w)}>delete</button>
            </li>
          {/each}
        </ul>
      {/if}
    {/each}
  {/if}
</main>

<style>
  main {
    max-width: 44rem;
    margin: 3rem auto;
    padding: 0 1rem;
  }
  h1 {
    margin-bottom: 0;
  }
  .tagline {
    color: var(--text-dim);
    margin-top: 0.2rem;
  }
  .tagline b {
    color: var(--accent);
  }
  .switch {
    margin-left: 0.6rem;
    font-size: var(--fs-small);
    padding: 0.12rem 0.5rem;
    color: var(--text-dim);
  }
  .folder-input {
    max-width: 11rem;
  }
  .filter {
    width: 100%;
    box-sizing: border-box;
    margin: 0.8rem 0 0.2rem;
    font-size: var(--fs-ui);
  }
  .folder-head {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    width: 100%;
    text-align: left;
    background: none;
    border: none;
    border-bottom: 1px solid var(--border);
    border-radius: 0;
    margin-top: 0.9rem;
    padding: 0.25rem 0.1rem;
    font-size: var(--fs-ui);
    font-weight: 600;
    color: var(--text);
  }
  .folder-head .tri {
    color: var(--text-dim);
  }
  .folder-head .count {
    color: var(--text-dim);
    font-weight: 400;
    font-size: var(--fs-small);
  }
  ul.indented {
    margin-left: 1.1rem;
  }
  .move {
    display: flex;
    gap: 0.25rem;
    align-items: center;
  }
  .move input {
    width: 10rem;
    font-size: var(--fs-small);
    padding: 0.15rem 0.4rem;
  }
  .move button {
    font-size: var(--fs-small);
    padding: 0.15rem 0.45rem;
  }
  form {
    display: flex;
    gap: 0.5rem;
    margin: 1.5rem 0;
  }
  form input {
    flex: 1;
  }
  ul {
    list-style: none;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }
  li {
    display: flex;
    align-items: center;
    gap: 0.8rem;
    background: var(--bg-raised);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.7rem 1rem;
  }
  li a {
    flex: 1;
    display: flex;
    flex-direction: column;
    color: inherit;
    text-decoration: none;
  }
  .title {
    font-weight: 600;
  }
  .desc {
    color: var(--text-dim);
    font-size: var(--fs-ui);
  }
  .meta {
    color: var(--text-dim);
    font-size: var(--fs-small);
  }
  .error {
    color: var(--danger);
  }
  .dim {
    color: var(--text-dim);
  }
</style>
