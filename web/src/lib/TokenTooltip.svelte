<!-- Rich hoverable popover for a token: debug-quoted text, probability, ids,
     the counterfactual row (top_logprobs as clickable branch buttons), and a
     one-line genParams summary. Stays open while the pointer is inside it. -->
<script lang="ts">
  import { creatorTextColor, creatorLabel } from './colors'
  import { branchAtToken, session } from './state.svelte'
  import { genParams } from './types'
  import type { Token, TopLogprob } from './types'

  let {
    nodeId,
    tokenIndex,
    x,
    y,
    anchorTop,
    onenter,
    onleave,
    onclose,
  }: {
    nodeId: string
    tokenIndex: number
    x: number
    y: number
    anchorTop: number
    onenter: () => void
    onleave: () => void
    onclose: () => void
  } = $props()

  const node = $derived(session.weave?.nodes[nodeId] ?? null)
  const token = $derived.by<Token | null>(() => {
    if (!node || node.content.type !== 'tokens') return null
    return node.content.tokens[tokenIndex] ?? null
  })
  const params = $derived(node ? genParams(node) : null)
  const paramsLine = $derived(
    params
      ? Object.entries(params)
          .map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`)
          .join('  ')
      : null,
  )

  function pct(logprob: number): string {
    return `${(Math.exp(logprob) * 100).toFixed(2)}%`
  }

  function isChosen(alt: TopLogprob): boolean {
    if (!token) return false
    if (alt.token_id != null && token.token_id != null) return alt.token_id === token.token_id
    return alt.text === token.text
  }

  function pick(alt: TopLogprob) {
    // snapshot the lazy getter props BEFORE onclose() nulls the parent's tip
    // state (reading them after would throw on null — this was a real bug)
    const id = nodeId
    const index = tokenIndex
    onclose()
    void branchAtToken(id, index, alt)
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
  {#if node && token}
    {#if token.top_logprobs.length > 0}
      <div class="alts">
        {#each token.top_logprobs as alt, j (j)}
          <button
            class="alt"
            class:chosen={isChosen(alt)}
            disabled={isChosen(alt)}
            onclick={() => pick(alt)}
            title={isChosen(alt)
              ? 'the sampled token'
              : `branch here with ${JSON.stringify(alt.text)} instead`}
          >
            <span class="alt-text">{JSON.stringify(alt.text)}</span>
            <span class="alt-prob">({pct(alt.logprob)})</span>
          </button>
        {/each}
      </div>
      <hr />
    {/if}
    <div class="row quoted">{JSON.stringify(token.text)}</div>
    {#if token.logprob != null}
      <div class="row">
        probability: {pct(token.logprob)}
        <span class="dim">(logprob {token.logprob.toFixed(3)})</span>
      </div>
    {/if}
    {#if token.token_id != null}<div class="row">token_id: {token.token_id}</div>{/if}
    {#if token.entropy != null}<div class="row">entropy: {token.entropy.toFixed(3)}</div>{/if}
    {#if token.inexact}
      <div class="row inexact-note">logprob from pre-edit context (inexact)</div>
    {/if}
    <hr />
    <div class="row">
      <span style:color={creatorTextColor(node.creator)}>{creatorLabel(node.creator)}</span>
      <span class="dim">· {new Date(node.created).toLocaleString()}</span>
    </div>
    {#if paramsLine}<div class="row dim params">{paramsLine}</div>{/if}
  {:else if node}
    <div class="row dim">token no longer present in this node</div>
  {/if}
</div>

<style>
  .tip {
    position: fixed;
    z-index: 1000;
    max-width: 420px;
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
  .quoted {
    font-family: var(--mono);
    color: var(--text);
  }
  .params {
    font-family: var(--mono);
    font-size: var(--fs-tiny);
    white-space: normal;
  }
  .dim {
    color: var(--text-dim);
  }
  .inexact-note {
    color: var(--text-dim);
    font-style: italic;
  }
  hr {
    border: none;
    border-top: 1px solid var(--border);
    margin: 0.35rem 0;
  }
  .alts {
    display: flex;
    gap: 0.25rem;
    overflow-x: auto;
    padding-bottom: 0.2rem;
    max-width: 100%;
  }
  .alt {
    flex-shrink: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    font-family: var(--mono);
    font-size: var(--fs-tiny);
    padding: 0.15rem 0.4rem;
    background: var(--bg-raised);
  }
  .alt .alt-prob {
    color: var(--text-dim);
  }
  .alt.chosen {
    border-color: var(--accent);
    cursor: default;
  }
  .alt.chosen .alt-text {
    color: var(--accent);
  }
</style>
