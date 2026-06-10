<script lang="ts">
  // Info pane: editable weave title/description, a key/value metadata editor,
  // and weave statistics. All edits are server-canonical: commit on blur/Enter
  // via PATCH /weaves/{id}; the WS-driven refetch brings the result back.
  import { creatorColor, creatorLabel } from './colors'
  import { api } from './api'
  import { session, withToast } from './state.svelte'

  // ---- drafts, resynced only when the server value actually changes
  // (so a refetch caused by unrelated weave activity never clobbers mid-edit text)

  let titleDraft = $state('')
  let lastTitle: string | null = null
  $effect(() => {
    const t = session.weave?.title ?? ''
    if (t !== lastTitle) {
      lastTitle = t
      titleDraft = t
    }
  })

  let descDraft = $state('')
  let lastDesc: string | null = null
  $effect(() => {
    const d = session.weave?.description ?? ''
    if (d !== lastDesc) {
      lastDesc = d
      descDraft = d
    }
  })

  interface MetaRow {
    key: string
    value: string
  }

  function displayValue(v: unknown): string {
    return typeof v === 'string' ? v : JSON.stringify(v)
  }

  let metaRows = $state<MetaRow[]>([])
  let lastMetaJson: string | null = null
  $effect(() => {
    const meta = session.weave?.metadata ?? {}
    const json = JSON.stringify(meta)
    if (json !== lastMetaJson) {
      lastMetaJson = json
      metaRows = Object.entries(meta).map(([key, v]) => ({ key, value: displayValue(v) }))
    }
  })

  let newKey = $state('')
  let newValue = $state('')

  // ---- commits

  async function commitTitle() {
    const w = session.weave
    if (!w) return
    const title = titleDraft.trim()
    if (title === '' || title === w.title) {
      titleDraft = w.title
      return
    }
    await withToast(() => api.updateWeave(w.id, { title }))
  }

  async function commitDescription() {
    const w = session.weave
    if (!w || descDraft === w.description) return
    await withToast(() => api.updateWeave(w.id, { description: descDraft }))
  }

  /** Rebuild the metadata map from the rows; unedited values keep their
   * original (possibly non-string) type, edited/new ones are stored as strings. */
  async function commitMetadata() {
    const w = session.weave
    if (!w) return
    const out: Record<string, unknown> = {}
    for (const row of metaRows) {
      const key = row.key.trim()
      if (key === '') continue
      const orig = w.metadata[key]
      out[key] = orig !== undefined && displayValue(orig) === row.value ? orig : row.value
    }
    if (JSON.stringify(out) === JSON.stringify(w.metadata)) return
    await withToast(() => api.updateWeave(w.id, { metadata: out }))
  }

  async function removeMetaRow(index: number) {
    metaRows = metaRows.filter((_, i) => i !== index)
    await commitMetadata()
  }

  async function addMetaRow() {
    const key = newKey.trim()
    if (key === '') return
    metaRows = [...metaRows.filter((r) => r.key !== key), { key, value: newValue }]
    newKey = ''
    newValue = ''
    await commitMetadata()
  }

  function blurOnEnter(e: KeyboardEvent) {
    if (e.key === 'Enter') {
      e.preventDefault()
      ;(e.currentTarget as HTMLElement).blur() // blur handler commits
    }
  }

  // ---- statistics

  const stats = $derived.by(() => {
    const w = session.weave
    if (!w) return null
    const nodes = Object.values(w.nodes)
    const byCreator = new Map<string, { label: string; color: string; count: number }>()
    let snippets = 0
    let tokens = 0
    for (const n of nodes) {
      if (n.content.type === 'snippet') snippets++
      else tokens++
      const key = `${n.creator.type}:${creatorLabel(n.creator)}`
      const cur = byCreator.get(key)
      if (cur) cur.count++
      else
        byCreator.set(key, {
          label: creatorLabel(n.creator),
          color: creatorColor(n.creator),
          count: 1,
        })
    }
    return {
      total: nodes.length,
      bookmarked: w.bookmarks.length,
      cursors: Object.keys(w.cursors).length,
      snippets,
      tokens,
      creators: [...byCreator.values()].sort((a, b) => b.count - a.count),
    }
  })
</script>

<div class="pane">
  {#if session.weave}
    <section>
      <label class="field">
        <span class="label">title</span>
        <input bind:value={titleDraft} onblur={() => void commitTitle()} onkeydown={blurOnEnter} />
      </label>
      <label class="field">
        <span class="label">description</span>
        <textarea
          bind:value={descDraft}
          rows="3"
          onblur={() => void commitDescription()}
          onkeydown={(e) => {
            if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
              e.preventDefault()
              ;(e.currentTarget as HTMLElement).blur()
            }
          }}
          placeholder="what is this weave about?"
        ></textarea>
      </label>
    </section>

    <section>
      <h3>metadata</h3>
      {#each metaRows as row, i (i)}
        <div class="meta-row">
          <input
            class="k"
            bind:value={row.key}
            onblur={() => void commitMetadata()}
            onkeydown={blurOnEnter}
            placeholder="key"
          />
          <input
            class="v"
            bind:value={row.value}
            onblur={() => void commitMetadata()}
            onkeydown={blurOnEnter}
            placeholder="value"
          />
          <button
            class="rm"
            onclick={() => void removeMetaRow(i)}
            title="remove entry"
            aria-label="remove entry">✕</button
          >
        </div>
      {/each}
      <div class="meta-row add">
        <input class="k" bind:value={newKey} onkeydown={(e) => e.key === 'Enter' && void addMetaRow()} placeholder="new key" />
        <input class="v" bind:value={newValue} onkeydown={(e) => e.key === 'Enter' && void addMetaRow()} placeholder="value" />
        <button class="rm" onclick={() => void addMetaRow()} disabled={newKey.trim() === ''} title="add entry">+</button>
      </div>
    </section>

    {#if stats}
      <section>
        <h3>statistics</h3>
        <table>
          <tbody>
            <tr><td>nodes</td><td>{stats.total}</td></tr>
            <tr
              ><td>content</td><td
                >{stats.snippets} snippet{stats.snippets === 1 ? '' : 's'} · {stats.tokens}
                tokens</td
              ></tr
            >
            <tr><td>bookmarked</td><td>{stats.bookmarked}</td></tr>
            <tr><td>cursors</td><td>{stats.cursors}</td></tr>
          </tbody>
        </table>
        {#if stats.creators.length > 0}
          <div class="creators">
            {#each stats.creators as c (c.label + c.color)}
              <span class="creator">
                <span class="swatch" style:background={c.color}></span>
                {c.label}: {c.count}
              </span>
            {/each}
          </div>
        {/if}
      </section>
    {/if}
  {/if}
</div>

<style>
  .pane {
    padding: 0.7rem;
    display: flex;
    flex-direction: column;
    gap: 0.9rem;
    font-size: var(--fs-ui);
  }
  section {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }
  h3 {
    margin: 0;
    font-size: var(--fs-small);
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-dim);
  }
  .field {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }
  .label {
    font-size: var(--fs-small);
    color: var(--text-dim);
  }
  input,
  textarea {
    font-size: var(--fs-ui);
    padding: 0.3rem 0.45rem;
  }
  textarea {
    resize: vertical;
    min-height: 3em;
  }
  .meta-row {
    display: flex;
    gap: 0.3rem;
    align-items: center;
  }
  .meta-row .k {
    flex: 0 0 38%;
    min-width: 0;
    font-family: var(--mono);
  }
  .meta-row .v {
    flex: 1;
    min-width: 0;
    font-family: var(--mono);
  }
  .meta-row .rm {
    flex-shrink: 0;
    width: 1.5rem;
    padding: 0.15rem 0;
    border: none;
    background: none;
    color: var(--text-dim);
    cursor: pointer;
  }
  .meta-row .rm:hover:not(:disabled) {
    color: var(--danger);
  }
  .meta-row.add .rm:hover:not(:disabled) {
    color: var(--accent);
  }
  .meta-row .rm:disabled {
    opacity: 0.4;
    cursor: default;
  }
  table {
    border-collapse: collapse;
    width: 100%;
  }
  td {
    padding: 0.12rem 0;
    color: var(--text);
  }
  td:first-child {
    color: var(--text-dim);
    width: 40%;
  }
  .creators {
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem 0.8rem;
    margin-top: 0.15rem;
  }
  .creator {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    white-space: nowrap;
  }
  .swatch {
    width: 11px;
    height: 11px;
    border-radius: 2px;
    flex-shrink: 0;
  }
</style>
