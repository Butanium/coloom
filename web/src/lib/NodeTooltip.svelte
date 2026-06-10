<!-- Hoverable popover for a snippet (non-token) node: creator in its color,
     the generation config (genParams) as a compact key:value table, created
     timestamp, and metadata entries. -->
<script lang="ts">
  import { creatorTextColor, creatorLabel } from './colors'
  import { session } from './state.svelte'
  import { genParams } from './types'

  let {
    nodeId,
    x,
    y,
    anchorTop,
    onenter,
    onleave,
  }: {
    nodeId: string
    x: number
    y: number
    anchorTop: number
    onenter: () => void
    onleave: () => void
  } = $props()

  const node = $derived(session.weave?.nodes[nodeId] ?? null)
  const params = $derived(node ? genParams(node) : null)
  const metaEntries = $derived(node ? Object.entries(node.metadata) : [])

  function fmt(v: unknown): string {
    return typeof v === 'string' ? v : JSON.stringify(v)
  }

  // free-form-edit provenance (editbuffer.ts): a hybrid/edited node carries
  // edited_by + edited_from; a copied downstream node carries copied_from.
  const editedBy = $derived(
    node && typeof node.metadata.edited_by === 'string'
      ? (node.metadata.edited_by as string)
      : null,
  )
  function provLine(): string | null {
    if (!node) return null
    const m = node.metadata
    if (m.copied_from) return `copied from ${fmt(m.copied_from).slice(0, 10)}`
    if (m.edited_from) {
      const from = Array.isArray(m.edited_from)
        ? m.edited_from.map((x) => String(x).slice(0, 8)).join(', ')
        : String(m.edited_from).slice(0, 8)
      return `edited from ${from}`
    }
    return null
  }

  // clamp into the viewport; flip above the anchor if it would overflow below
  let el = $state<HTMLDivElement>()
  let pos = $state({ left: -9999, top: -9999 })
  $effect(() => {
    if (!el) return
    const w = el.offsetWidth
    const h = el.offsetHeight
    const left = Math.max(8, Math.min(x, window.innerWidth - w - 8))
    let top = y
    if (y + h > window.innerHeight - 8) top = Math.max(8, anchorTop - h - 6)
    pos = { left, top }
  })
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  class="tip"
  bind:this={el}
  style:left={`${pos.left}px`}
  style:top={`${pos.top}px`}
  onpointerenter={onenter}
  onpointerleave={onleave}
  role="tooltip"
>
  {#if node}
    <div class="row">
      <span style:color={creatorTextColor(node.creator)}>{creatorLabel(node.creator)}</span>
      <span class="dim">({node.creator.type})</span>
    </div>
    {#if editedBy}<div class="row prov">edited by {editedBy}</div>{/if}
    {#if provLine()}<div class="row prov dim">{provLine()}</div>{/if}
    {#if params}
      <div class="kv">
        {#each Object.entries(params) as [k, v] (k)}
          <div class="k">{k}</div>
          <div class="v">{fmt(v)}</div>
        {/each}
      </div>
    {/if}
    <div class="row dim">created {new Date(node.created).toLocaleString()}</div>
    {#if metaEntries.length > 0}
      <hr />
      <div class="kv">
        {#each metaEntries as [k, v] (k)}
          <div class="k">{k}</div>
          <div class="v">{fmt(v)}</div>
        {/each}
      </div>
    {/if}
    <div class="row dim mono">{node.id.slice(0, 8)}</div>
  {/if}
</div>

<style>
  .tip {
    position: fixed;
    z-index: 1000;
    max-width: 380px;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.45rem 0.55rem;
    font-size: var(--fs-ui);
    line-height: 1.5;
    box-shadow: 0 4px 18px rgba(0, 0, 0, 0.5);
  }
  .row {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .dim {
    color: var(--text-dim);
  }
  .prov {
    font-style: italic;
  }
  .mono {
    font-family: var(--mono);
    font-size: var(--fs-tiny);
  }
  hr {
    border: none;
    border-top: 1px solid var(--border);
    margin: 0.35rem 0;
  }
  .kv {
    display: grid;
    grid-template-columns: auto 1fr;
    column-gap: 0.6rem;
    margin: 0.2rem 0;
  }
  .kv .k {
    color: var(--text-dim);
  }
  .kv .v {
    font-family: var(--mono);
    font-size: var(--fs-tiny);
    word-break: break-word;
  }
</style>
