<!-- The thread text pane: MY cursor's thread (root → cursor) as one continuous,
     FREE-FORM editable document (contenteditable plaintext-only). Per-node
     creator colors, per-token probability opacity, boundary ticks, cross-pane
     hover sync, rich hoverable tooltips (token + node), local caret with
     generate-at-caret, and free-form edits diffed back into the weave as
     split/append/hybrid-edit/copy ops (see editbuffer.ts). An empty thread
     (blank weave / no cursor) is the same editable surface: typing creates
     the root node. -->
<script lang="ts">
  import NodeTooltip from './NodeTooltip.svelte'
  import TokenTooltip from './TokenTooltip.svelte'
  import { api } from './api'
  import { creatorTextColor, tokenOpacity } from './colors'
  import {
    buildBuffer,
    classifyEdit,
    planEdit,
    type EditOp,
    type ParentRef,
  } from './editbuffer'
  import {
    generateAt,
    identity,
    moveMyCursor,
    myCursorNodeId,
    openContextMenu,
    session,
    threadPath,
    withToast,
  } from './state.svelte'
  import type { Token, WeaveNode } from './types'
  import { nodeText } from './types'

  let scroller: HTMLDivElement | undefined = $state()
  let docEl: HTMLDivElement | undefined = $state()
  let pointerInside = $state(false)

  const thread = $derived.by(() => {
    const weave = session.weave
    const cur = myCursorNodeId()
    if (!weave || !cur) return []
    return threadPath(weave, cur).map((id) => weave.nodes[id])
  })

  // shown via CSS ::before while the doc is :empty — never part of the buffer
  const placeholder = $derived.by(() => {
    const weave = session.weave
    if (!weave) return ''
    if (weave.roots.length === 0) return 'nothing woven yet — just start typing'
    if (thread.length === 0)
      return `no cursor for “${identity.name}” yet — click a node in the tree to place it, or type here to start a new root`
    return ''
  })

  // -------------------------------------------------- free-form edit state
  // While `dirty` (user typed, edit not yet applied) or `applying` (executing
  // the plan over REST), we must NOT re-render the doc from weave state — a
  // WS-driven refetch would clobber the caret mid-edit. The guard freezes the
  // rendered thread to the last weave-consistent snapshot.
  let dirty = $state(false)
  let applying = $state(false)
  let editTimer: ReturnType<typeof setTimeout> | undefined
  const EDIT_DEBOUNCE_MS = 600

  // The thread actually rendered: frozen while editing so the DOM the user is
  // typing into doesn't get rebuilt under them.
  let frozenThread: WeaveNode[] = $state([])
  const renderThread = $derived(dirty || applying ? frozenThread : thread)

  $effect(() => {
    // keep a fresh snapshot to freeze from, but only while NOT editing
    if (!dirty && !applying) frozenThread = thread
  })

  function onInput() {
    if (!docEl) return
    if (!dirty) {
      frozenThread = thread // snapshot the pre-edit thread to diff against
      dirty = true
    }
    clearTimeout(editTimer)
    editTimer = setTimeout(applyEdit, EDIT_DEBOUNCE_MS)
  }

  // Block-aware text reconstruction. The browser sometimes splits multiline
  // contenteditable input into block <div>s; textContent/Range.toString() join
  // those WITHOUT the newline, dropping paragraph breaks. We walk the DOM and
  // emit a '\n' when crossing into a new block sibling. `stopAt` (node, offset)
  // bounds the walk for caret-offset measurement; omit it to read the whole doc.
  function docTextUpTo(stopNode?: Node, stopOffset?: number): string {
    if (!docEl) return ''
    let out = ''
    let prevBlock: Element | null = null
    const walker = document.createTreeWalker(docEl, NodeFilter.SHOW_TEXT)
    let node = walker.nextNode()
    while (node) {
      // resolve each text node's nearest block ancestor (docEl itself for inline
      // top-level content); a change of block between consecutive text nodes is a
      // paragraph break the browser inserted as a <div> — emit the lost '\n'.
      let block: Element = node.parentElement ?? docEl
      while (block !== docEl && getComputedStyle(block).display === 'inline')
        block = block.parentElement ?? docEl
      if (prevBlock !== null && block !== prevBlock) out += '\n'
      prevBlock = block
      const text = node.textContent ?? ''
      if (stopNode && node === stopNode) return out + text.slice(0, stopOffset)
      out += text
      node = walker.nextNode()
    }
    return out
  }

  function docInnerText(): string {
    return docTextUpTo()
  }

  // caret as a char offset into docInnerText() space (survives a re-render)
  function caretCharOffset(): number | null {
    if (!docEl) return null
    const sel = document.getSelection()
    if (!sel || sel.rangeCount === 0) return null
    const range = sel.getRangeAt(0)
    if (!docEl.contains(range.startContainer)) return null
    // if the caret sits in an element container, resolve to a text node length
    const c = range.startContainer
    if (c.nodeType === Node.TEXT_NODE) return docTextUpTo(c, range.startOffset).length
    return docTextUpTo().length // element container: fall back to end-of-doc
  }

  function setCaretCharOffset(target: number) {
    if (!docEl) return
    const walker = document.createTreeWalker(docEl, NodeFilter.SHOW_TEXT)
    let acc = 0
    let node = walker.nextNode()
    while (node) {
      const len = node.textContent?.length ?? 0
      if (acc + len >= target) {
        const sel = document.getSelection()
        const r = document.createRange()
        r.setStart(node, Math.max(0, Math.min(target - acc, len)))
        r.collapse(true)
        sel?.removeAllRanges()
        sel?.addRange(r)
        return
      }
      acc += len
      node = walker.nextNode()
    }
    // past the end: caret to the very end
    const sel = document.getSelection()
    const r = document.createRange()
    r.selectNodeContents(docEl)
    r.collapse(false)
    sel?.removeAllRanges()
    sel?.addRange(r)
  }

  async function applyEdit() {
    clearTimeout(editTimer)
    if (!docEl || !session.weave || applying) return
    const weaveId = session.weave.id
    // innerText (not textContent): when the browser splits multiline input into
    // block <div>s, textContent concatenates across them WITHOUT the newlines —
    // silently dropping pasted/typed paragraph breaks. innerText reconstructs them.
    const newText = docInnerText()
    const buf = buildBuffer(frozenThread)
    const kind = classifyEdit(buf, newText)
    if (kind === 'noop') {
      dirty = false
      return
    }
    const savedCaret = caretCharOffset()
    const plan = planEdit(buf, newText, identity.name)
    applying = true
    try {
      await executePlan(weaveId, plan.ops)
    } finally {
      applying = false
      dirty = false
      caret = null
    }
    pendingDoc = { text: newText, caret: savedCaret }
    // the WS refetch rebuilds the spans; restore the caret afterwards
    if (savedCaret !== null) {
      queueMicrotask(() => requestAnimationFrame(() => setCaretCharOffset(savedCaret)))
    }
  }

  // After an edit applies, the DOM can hold user-typed text Svelte doesn't track
  // (Chromium extends the LAST TOKEN SPAN's text node when typing at the doc
  // end; an empty thread gets raw text nodes) — once the refetched thread paints
  // the same text into proper spans it shows DOUBLED. When the thread catches up
  // with what we wrote and the DOM text diverges, rebuild every span from weave
  // state ({#key docEpoch}), sweep leftover untracked nodes, restore the caret.
  let pendingDoc = $state<{ text: string; caret: number | null } | null>(null)
  let docEpoch = $state(0)
  $effect(() => {
    const pending = pendingDoc
    if (pending === null || dirty || applying || !docEl) return
    if (buildBuffer(renderThread).text !== pending.text) return // refetch pending
    pendingDoc = null
    if (docInnerText() === pending.text) return // DOM already consistent
    docEpoch++
    queueMicrotask(() =>
      requestAnimationFrame(() => {
        if (!docEl) return
        for (const child of [...docEl.childNodes]) {
          if (child.nodeType === Node.COMMENT_NODE) continue
          if (
            child instanceof Element &&
            (child.hasAttribute('data-node-id') || child.querySelector('[data-node-id]'))
          )
            continue
          child.remove()
        }
        if (pending.caret !== null && document.activeElement === docEl)
          setCaretCharOffset(pending.caret)
      }),
    )
  })

  // execute the abstract plan over REST, resolving parent refs as we go
  async function executePlan(weaveId: string, ops: EditOp[]) {
    const splitHeadIds: Record<number, string> = {} // op index -> head node id
    let lastCreated: string | null = null

    function resolve(ref: ParentRef): string | null {
      if (ref.kind === 'id') return ref.id
      if (ref.kind === 'root') return null
      if (ref.kind === 'splitHead') return splitHeadIds[ref.opIndex] ?? null
      if (ref.kind === 'lastCreated') return lastCreated
      return null
    }

    for (let i = 0; i < ops.length; i++) {
      const op = ops[i]
      if (op.op === 'split') {
        const { head } = await api.splitNode(weaveId, op.nodeId, op.at)
        splitHeadIds[i] = head.id
      } else if (op.op === 'updateText') {
        await api.updateNode(weaveId, op.nodeId, { type: 'snippet', text: op.text })
      } else if (op.op === 'append') {
        const node = await api.addNode(weaveId, op.text, {
          parentId: resolve(op.parent),
          creator: { type: 'human', label: identity.name },
          moveCursor: op.moveCursor ? identity.name : undefined,
        })
        lastCreated = node.id
      } else if (op.op === 'buildEdited') {
        const node = await api.addNode(weaveId, '', {
          content: op.content,
          parentId: resolve(op.parent),
          creator: op.creator,
          metadata: op.metadata,
          moveCursor: op.moveCursor ? identity.name : undefined,
        })
        lastCreated = node.id
      } else if (op.op === 'copy') {
        const src = session.weave?.nodes[op.sourceId]
        if (!src) continue
        const content = copyContent(src)
        const node = await api.addNode(weaveId, '', {
          content,
          parentId: resolve(op.parent),
          creator: src.creator,
          metadata: { ...src.metadata, copied_from: src.id },
          moveCursor: op.moveCursor ? identity.name : undefined,
        })
        lastCreated = node.id
      } else if (op.op === 'moveCursor') {
        const id = resolve(op.target)
        if (id) await moveMyCursor(id)
      }
    }
  }

  // copy a node's content for the new branch: tokens carry over but every token
  // flagged inexact (logprobs derived from a now-diverged prefix); snippets as-is
  function copyContent(node: WeaveNode) {
    if (node.content.type === 'snippet') return { type: 'snippet' as const, text: node.content.text }
    return {
      type: 'tokens' as const,
      tokens: node.content.tokens.map((t) => ({ ...t, inexact: true })),
    }
  }

  function onDocBlur() {
    if (dirty) void applyEdit()
  }

  // -------------------------------------------------- local caret (click = caret only)
  let caret = $state<{ nodeId: string; offset: number } | null>(null)
  const caretNode = $derived(
    caret && session.weave ? (session.weave.nodes[caret.nodeId] ?? null) : null,
  )

  $effect(() => {
    function onSelectionChange() {
      if (!docEl || dirty || applying) return // mapping is stale mid-edit
      const sel = document.getSelection()
      if (!sel || sel.rangeCount === 0) return
      const range = sel.getRangeAt(0) // first (sorted) end of the selection
      const c = range.startContainer
      const el = c instanceof Element ? c : c.parentElement
      const span = el?.closest('[data-node-id]')
      if (!span || !docEl.contains(span)) return // selection elsewhere: keep last caret
      const r = document.createRange()
      r.setStart(span, 0)
      r.setEnd(range.startContainer, range.startOffset)
      caret = { nodeId: span.getAttribute('data-node-id')!, offset: r.toString().length }
    }
    document.addEventListener('selectionchange', onSelectionChange)
    return () => document.removeEventListener('selectionchange', onSelectionChange)
  })

  // generate exactly at the caret: split mid-node (token boundary for Tokens,
  // char offset for Snippet), then generate on the head; node end = plain generate
  let genBusy = $state(false)
  async function generateAtCaret() {
    const weave = session.weave
    const c = caret
    if (!weave || !c || genBusy) return
    const node = weave.nodes[c.nodeId]
    if (!node) return
    genBusy = true
    try {
      const text = nodeText(node)
      if (c.offset >= text.length) {
        await generateAt(node.id) // caret at node end
        return
      }
      let splitAt: number
      if (node.content.type === 'tokens') {
        // last token boundary at or before the caret
        let acc = 0
        splitAt = 0
        for (const t of node.content.tokens) {
          if (acc + t.text.length > c.offset) break
          acc += t.text.length
          splitAt++
        }
      } else {
        // snippet: char offset -> code-point offset (server splits on code points)
        let cp = 0
        let i = 0
        while (i < c.offset && i < text.length) {
          const code = text.codePointAt(i)!
          i += code > 0xffff ? 2 : 1
          cp++
        }
        splitAt = cp
      }
      if (splitAt <= 0) {
        // caret at (or rounded to) the node start: generate from the parent
        await generateAt(node.parents[0] ?? node.id)
        return
      }
      const split = await withToast(() => api.splitNode(weave.id, node.id, splitAt))
      if (!split) return
      caret = null
      await generateAt(split.head.id, { moveCursor: true })
    } finally {
      genBusy = false
    }
  }

  // -------------------------------------------------- hover + tooltips
  // hover-intent: small delay before show AND before hide; pointer travelling
  // into the popover cancels the hide (contiguous hover region).
  const TIP_SHOW_MS = 350
  const TIP_HIDE_MS = 250

  type Tip = {
    kind: 'token' | 'node'
    nodeId: string
    tokenIndex: number
    x: number
    y: number
    anchorTop: number
  }
  let tip = $state<Tip | null>(null)
  let hoveredToken = $state<{ nodeId: string; index: number } | null>(null)
  let showTimer: ReturnType<typeof setTimeout> | undefined
  let hideTimer: ReturnType<typeof setTimeout> | undefined

  function scheduleTip(kind: 'token' | 'node', nodeId: string, tokenIndex: number, el: HTMLElement) {
    clearTimeout(hideTimer)
    if (tip && tip.kind === kind && tip.nodeId === nodeId && tip.tokenIndex === tokenIndex) return
    clearTimeout(showTimer)
    showTimer = setTimeout(() => {
      if (!el.isConnected) return
      const r = el.getBoundingClientRect()
      tip = { kind, nodeId, tokenIndex, x: r.left, y: r.bottom + 5, anchorTop: r.top }
    }, TIP_SHOW_MS)
  }

  function scheduleHide() {
    clearTimeout(showTimer)
    hideTimer = setTimeout(() => (tip = null), TIP_HIDE_MS)
  }

  function tipEnter() {
    clearTimeout(hideTimer)
    if (tip) session.hoveredNodeId = tip.nodeId // popover keeps the node highlight alive
  }

  function tipLeave() {
    if (tip && session.hoveredNodeId === tip.nodeId) session.hoveredNodeId = null
    scheduleHide()
  }

  function closeTip() {
    clearTimeout(showTimer)
    clearTimeout(hideTimer)
    tip = null
  }

  $effect(() => () => {
    clearTimeout(showTimer)
    clearTimeout(hideTimer)
    clearTimeout(editTimer)
  })

  // close a tooltip whose node vanished from the weave
  $effect(() => {
    if (tip && (!session.weave || !session.weave.nodes[tip.nodeId])) tip = null
  })

  function nodeTipWanted(node: WeaveNode): boolean {
    // snippet nodes (and empty tokens nodes, which render no token spans)
    return node.content.type === 'snippet' || nodeText(node) === ''
  }

  function onNodeEnter(node: WeaveNode, el: HTMLElement) {
    session.hoveredNodeId = node.id
    if (nodeTipWanted(node)) scheduleTip('node', node.id, -1, el)
  }

  function onNodeLeave(node: WeaveNode) {
    if (session.hoveredNodeId === node.id) session.hoveredNodeId = null
    if (nodeTipWanted(node)) scheduleHide()
  }

  function onTokenEnter(nodeId: string, i: number, el: HTMLElement) {
    hoveredToken = { nodeId, index: i }
    scheduleTip('token', nodeId, i, el)
  }

  function onTokenLeave(nodeId: string, i: number) {
    if (hoveredToken && hoveredToken.nodeId === nodeId && hoveredToken.index === i) {
      hoveredToken = null
    }
    scheduleHide()
  }

  function isTokenHot(nodeId: string, i: number): boolean {
    if (hoveredToken && hoveredToken.nodeId === nodeId && hoveredToken.index === i) return true
    return tip !== null && tip.kind === 'token' && tip.nodeId === nodeId && tip.tokenIndex === i
  }

  function isInexact(t: Token): boolean {
    return t.inexact === true
  }

  // -------------------------------------------------- click-to-focus
  // clicks on the scroller's empty padding (e.g. a blank weave's one-line doc)
  // still land in the editor, caret at the end
  function onScrollerClick(e: MouseEvent) {
    if (e.target !== scroller || !docEl) return
    docEl.focus()
    const sel = document.getSelection()
    const r = document.createRange()
    r.selectNodeContents(docEl)
    r.collapse(false)
    sel?.removeAllRanges()
    sel?.addRange(r)
  }

  // -------------------------------------------------- auto-scroll
  // follow the changed node (scrollIntoView) or the growing thread tail —
  // but NEVER while the reader's pointer is inside the pane (sacred rule), and
  // never while mid-edit (caret stays put).
  let lastChangedAt = 0
  let lastTailId: string | null = null
  $effect(() => {
    const at = session.changedAt
    const changedId = session.changedNodeId
    const tailId = renderThread.length > 0 ? renderThread[renderThread.length - 1].id : null
    const inside = pointerInside
    const editing = dirty || applying
    const sc = scroller
    const isNewChange = at !== lastChangedAt
    lastChangedAt = at
    const tailChanged = tailId !== lastTailId
    lastTailId = tailId
    if (!sc || inside || editing) return // pointer/edit-suppression: drop the scroll
    if (isNewChange && changedId) {
      const el = sc.querySelector(`[data-node-id="${CSS.escape(changedId)}"]`)
      if (el) {
        el.scrollIntoView({ block: 'nearest' })
        return
      }
    }
    if (tailChanged) sc.scrollTop = sc.scrollHeight
  })
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  class="pane"
  onpointerenter={() => (pointerInside = true)}
  onpointerleave={() => (pointerInside = false)}
>
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="scroller" bind:this={scroller} onclick={onScrollerClick}>
    {#if session.weave}
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <div
        class="doc"
        bind:this={docEl}
        contenteditable="plaintext-only"
        spellcheck="false"
        data-placeholder={placeholder}
        oninput={onInput}
        onblur={onDocBlur}
      >
        {#key docEpoch}{#each renderThread as node (node.id)}
          <!-- svelte-ignore a11y_no_static_element_interactions -->
          <span
            class="node"
            data-node-id={node.id}
            class:hovered={session.hoveredNodeId === node.id}
            class:caret-node={caret?.nodeId === node.id}
            class:boundary={renderThread.length > 1}
            style:color={creatorTextColor(node.creator)}
            onpointerenter={(e) => onNodeEnter(node, e.currentTarget)}
            onpointerleave={() => onNodeLeave(node)}
            oncontextmenu={(e) => {
              e.preventDefault()
              openContextMenu(node.id, e.clientX, e.clientY)
            }}
          >{#if nodeText(node) === ''}<span class="empty">(no text)</span>{:else if node.content.type === 'tokens'}{#each node.content.tokens as t, i}<span
                  class="token"
                  class:tok-hover={isTokenHot(node.id, i)}
                  class:inexact={isInexact(t)}
                  style:opacity={tokenOpacity(t)}
                  onpointerenter={(e) => onTokenEnter(node.id, i, e.currentTarget)}
                  onpointerleave={() => onTokenLeave(node.id, i)}>{t.text}</span
                >{/each}{:else}{nodeText(node)}{/if}</span
          >{/each}{/key}
      </div>
    {/if}
  </div>

  {#if dirty || applying}
    <div class="editing-bar">editing… {applying ? 'syncing' : ''}</div>
  {/if}

  {#if caret && caretNode && !dirty && !applying}
    <div class="caret-bar">
      <span class="caret-info">
        caret in <b style:color={creatorTextColor(caretNode.creator)}>{caretNode.id.slice(0, 6)}</b>
        @ {caret.offset}
      </span>
      <button class="mini" onclick={() => void generateAtCaret()} disabled={genBusy}>
        {genBusy ? '…' : '⚡ generate here'}
      </button>
      <button class="mini" onclick={() => (caret = null)} title="dismiss caret">✕</button>
    </div>
  {/if}
</div>

{#if tip}
  {#if tip.kind === 'token'}
    <TokenTooltip
      nodeId={tip.nodeId}
      tokenIndex={tip.tokenIndex}
      x={tip.x}
      y={tip.y}
      anchorTop={tip.anchorTop}
      onenter={tipEnter}
      onleave={tipLeave}
      onclose={closeTip}
    />
  {:else}
    <NodeTooltip
      nodeId={tip.nodeId}
      x={tip.x}
      y={tip.y}
      anchorTop={tip.anchorTop}
      onenter={tipEnter}
      onleave={tipLeave}
    />
  {/if}
{/if}

<style>
  .pane {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  .scroller {
    flex: 1;
    overflow-y: auto;
    padding: 1rem;
    cursor: text;
  }
  .doc {
    font-family: var(--mono);
    font-size: var(--fs-doc);
    line-height: 1.65;
    white-space: pre-wrap;
    word-break: break-word;
    outline: none;
    min-height: 1.65em;
  }
  .doc:empty::before {
    /* pure CSS hint: never part of the editable buffer */
    content: attr(data-placeholder);
    color: var(--text-dim);
    pointer-events: none;
  }
  .node {
    cursor: text;
  }
  .node.hovered {
    background: #2c2c3c;
    border-radius: 2px;
  }
  .node.caret-node {
    background: rgba(232, 163, 61, 0.08);
    border-radius: 2px;
  }
  .node.hovered.caret-node {
    background: #30303e;
  }
  .node.boundary {
    /* thin attribution-boundary tick at the start of each node */
    border-left: 1px solid var(--text-dim);
    margin-left: 1px;
  }
  .token.tok-hover {
    text-decoration: underline;
    text-decoration-color: var(--text-dim);
    text-underline-offset: 3px;
  }
  .token.inexact {
    /* logprob carried over from a diverged prefix: dotted underline marks it */
    text-decoration: underline dotted;
    text-decoration-color: var(--text-dim);
    text-underline-offset: 3px;
  }
  .token.inexact.tok-hover {
    text-decoration: underline dotted;
  }
  .empty {
    font-style: italic;
    opacity: 0.6;
  }
  .editing-bar {
    padding: 0.2rem 0.7rem;
    border-top: 1px solid var(--border);
    background: var(--bg-raised);
    font-size: var(--fs-small);
    color: var(--accent);
  }
  .caret-bar {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.25rem 0.7rem;
    border-top: 1px solid var(--border);
    background: var(--bg-raised);
    font-size: var(--fs-small);
    color: var(--text-dim);
  }
  .caret-info {
    flex: 1;
    font-family: var(--mono);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .caret-info b {
    font-weight: 600;
  }
  button.mini {
    font-size: var(--fs-tiny);
    padding: 0.1rem 0.45rem;
  }
</style>
