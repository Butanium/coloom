// Pure free-form-edit diff logic for the thread document — NO DOM, NO api here.
//
// The thread (root→cursor) renders as one continuous editable buffer. When the
// user edits it we diff (oldBuffer, newText) into an abstract EDIT PLAN of
// split/append/update/copy ops that TextPane then executes over REST. Mirrors
// Tapestry's whole-buffer diff (docs/ui-specs/textedit.md §6) but expressed as
// explicit operations against coloom's per-cursor thread + server-canonical store.
//
// Offset convention: all offsets here are JS string (UTF-16 code-unit) offsets
// into the buffer text — exactly what `textContent` / getSelection give us.
// Snippet splits need PYTHON CODE-POINT offsets server-side, so we convert at
// the edge (codePointOffset); token-node splits are token-INDEX based and must
// be token-aligned (a partially-kept token belongs to the replaced region).

import type { Creator, Token, WeaveNode } from './types'
import { nodeText } from './types'

// ---------------------------------------------------------------- buffer model

export interface BufNode {
  id: string
  creator: Creator
  kind: 'snippet' | 'tokens'
  text: string
  start: number // buffer char (UTF-16) offset of this node's first char
  end: number // exclusive
  bookmarked: boolean
  childCount: number // branches hanging off this node (coalesce guard)
  // for tokens nodes: buffer offset of each token start, plus a final sentinel
  // == end (so tokenStarts[i]..tokenStarts[i+1] is token i's range)
  tokenStarts: number[]
  tokens: Token[]
}

export interface EditBuffer {
  text: string
  nodes: BufNode[]
}

/** Build the flat editable buffer from the ordered thread nodes (root→cursor). */
export function buildBuffer(threadNodes: WeaveNode[]): EditBuffer {
  const nodes: BufNode[] = []
  let offset = 0
  for (const n of threadNodes) {
    const text = nodeText(n)
    const start = offset
    const tokenStarts: number[] = []
    let tokens: Token[] = []
    if (n.content.type === 'tokens') {
      tokens = n.content.tokens
      let acc = start
      for (const t of tokens) {
        tokenStarts.push(acc)
        acc += t.text.length
      }
      tokenStarts.push(acc) // sentinel == end
    }
    offset += text.length
    nodes.push({
      id: n.id,
      creator: n.creator,
      kind: n.content.type,
      text,
      start,
      end: offset,
      bookmarked: n.bookmarked,
      childCount: n.children.length,
      tokenStarts,
      tokens,
    })
  }
  return { text: nodes.map((n) => n.text).join(''), nodes }
}

// ---------------------------------------------------------------- diff core

export interface BufferDiff {
  prefix: number // length of the common prefix (buffer offset where the edit starts)
  oldEnd: number // buffer offset in OLD where the edit region ends (exclusive)
  inserted: string // the replacement text for [prefix, oldEnd)
}

/** Longest common prefix p and suffix s with p + s <= min(len), suffix not
 * overlapping the prefix. Edit region in old = [p, len(old)-s); replacement =
 * new[p : len(new)-s]. Returns null when the strings are identical. */
export function diffBuffers(oldText: string, newText: string): BufferDiff | null {
  if (oldText === newText) return null
  const minLen = Math.min(oldText.length, newText.length)
  let p = 0
  while (p < minLen && oldText[p] === newText[p]) p++
  // suffix must not overlap the prefix on either side
  let s = 0
  const maxS = minLen - p
  while (
    s < maxS &&
    oldText[oldText.length - 1 - s] === newText[newText.length - 1 - s]
  ) {
    s++
  }
  return {
    prefix: p,
    oldEnd: oldText.length - s,
    inserted: newText.slice(p, newText.length - s),
  }
}

export type EditKind = 'noop' | 'append' | 'tail-delete' | 'mid-edit'

export function classifyEdit(buf: EditBuffer, newText: string): EditKind {
  const d = diffBuffers(buf.text, newText)
  if (!d) return 'noop'
  // pure append: the edit region is empty and sits at the very end
  if (d.prefix === buf.text.length && d.oldEnd === buf.text.length) return 'append'
  // pure tail deletion: newText is a prefix of old (nothing inserted, region
  // runs to the end of the buffer)
  if (d.inserted === '' && d.oldEnd === buf.text.length) return 'tail-delete'
  return 'mid-edit'
}

// ---------------------------------------------------------------- offset helpers

/** UTF-16 offset → Python code-point offset within a string (snippet splits are
 * code-point based server-side; an emoji is 2 UTF-16 units but 1 code point). */
export function codePointOffset(text: string, utf16Offset: number): number {
  let cp = 0
  let i = 0
  while (i < utf16Offset && i < text.length) {
    const code = text.codePointAt(i)!
    i += code > 0xffff ? 2 : 1
    cp++
  }
  return cp
}

/** The BufNode containing buffer offset `off`. At an exact node boundary the
 * LATER node wins (so off == buffer end maps to the last node's end). */
export function nodeAtOffset(buf: EditBuffer, off: number): BufNode | null {
  if (buf.nodes.length === 0) return null
  for (const n of buf.nodes) {
    if (off < n.end) return n
    if (off === n.end && n === buf.nodes[buf.nodes.length - 1]) return n
  }
  return buf.nodes[buf.nodes.length - 1]
}

/** Round a buffer offset DOWN to the nearest token start within a tokens node
 * (a partially-kept token belongs to the replaced region). Returns the TOKEN
 * INDEX at/just-before `off`. */
export function tokenIndexFloor(node: BufNode, off: number): number {
  let idx = 0
  for (let i = 0; i < node.tokens.length; i++) {
    if (node.tokenStarts[i] <= off) idx = i
    else break
  }
  // if off is exactly at a token start, that token starts the replaced region
  if (node.tokenStarts[idx] < off) {
    // off falls strictly inside token `idx` -> keep idx (its start is the floor)
    return idx
  }
  return idx
}

// ---------------------------------------------------------------- edit plan
// An abstract, deterministic op list. TextPane executes these sequentially over
// REST. Kept here (pure) so the diff logic is testable without a browser.

// Split a node at a boundary. `at` is a TOKEN INDEX for tokens nodes, a CODE-POINT
// offset for snippets. The head keeps the node id; the tail is a new node.
export interface SplitOp {
  op: 'split'
  nodeId: string
  at: number
  kind: 'tokens' | 'snippet'
}

// Grow an existing snippet node in place (keystroke coalescing on pure append).
export interface UpdateTextOp {
  op: 'updateText'
  nodeId: string
  text: string
}

// Append one new snippet node under `parentRef` and move my cursor to it.
// parentRef is 'head' (the node split off in a preceding SplitOp) or a node id
// or null (new root). 'leaf' = the current thread leaf.
export interface AppendOp {
  op: 'append'
  text: string
  parent: ParentRef
  moveCursor: boolean
}

export type ParentRef =
  | { kind: 'id'; id: string }
  | { kind: 'splitHead'; opIndex: number } // head of the split at plan op `opIndex`
  | { kind: 'lastCreated' } // the node created by the previous create op
  | { kind: 'root' }

// Create the hybrid / replacement node carrying edited middle + preserved suffix.
export interface BuildEditedOp {
  op: 'buildEdited'
  parent: ParentRef
  content: { type: 'snippet'; text: string } | { type: 'tokens'; tokens: Token[] }
  creator: Creator
  metadata: Record<string, unknown>
  moveCursor: boolean
}

// Copy a downstream node verbatim onto the new branch (creator preserved, tokens
// flagged inexact, snippets as-is), recording provenance.
export interface CopyOp {
  op: 'copy'
  sourceId: string
  parent: ParentRef
  moveCursor: boolean
}

// Move my cursor to a node referenced by a preceding op (no new node).
export interface MoveCursorOp {
  op: 'moveCursor'
  target: ParentRef
}

export type EditOp =
  | SplitOp
  | UpdateTextOp
  | AppendOp
  | BuildEditedOp
  | CopyOp
  | MoveCursorOp

export interface EditPlan {
  kind: EditKind
  ops: EditOp[]
}

/** Build the edit plan from (buffer, newText, identity). Pure + deterministic.
 * `identity` is my cursor/participant name (used for human attribution + the
 * edited_by / copied_from provenance). */
export function planEdit(buf: EditBuffer, newText: string, identity: string): EditPlan {
  const kind = classifyEdit(buf, newText)
  if (kind === 'noop') return { kind, ops: [] }
  const d = diffBuffers(buf.text, newText)!

  if (kind === 'append') {
    return { kind, ops: planAppend(buf, d.inserted, identity) }
  }
  if (kind === 'tail-delete') {
    // the new doc end is where the common prefix stops (deletion runs to the
    // old buffer end), NOT d.oldEnd (== old buffer length here).
    return { kind, ops: planTailDelete(buf, d.prefix) }
  }
  return { kind, ops: planMidEdit(buf, d, identity) }
}

// -------------------------------------------------- append at the thread end

function planAppend(buf: EditBuffer, added: string, identity: string): EditOp[] {
  const leaf = buf.nodes[buf.nodes.length - 1]
  // coalesce only into MY own snippet leaf that isn't bookmarked AND has no
  // children: rewriting the text of a node with branches under it would
  // silently change every child's context (nothing-destroyed violation) —
  // with children present, the typed text becomes a NEW node under the leaf,
  // sibling to the existing branches
  const mine =
    leaf &&
    leaf.kind === 'snippet' &&
    leaf.creator.type === 'human' &&
    leaf.creator.label === identity &&
    !leaf.bookmarked &&
    leaf.childCount === 0
  if (mine) {
    return [{ op: 'updateText', nodeId: leaf.id, text: leaf.text + added }]
  }
  return [
    {
      op: 'append',
      text: added,
      parent: leaf ? { kind: 'id', id: leaf.id } : { kind: 'root' },
      moveCursor: true,
    },
  ]
}

// -------------------------------------------------- pure tail deletion
// Nothing destroyed: just move my cursor up. If the new end falls mid-node,
// split there and land on the head; downstream survives as a sibling branch.

function planTailDelete(buf: EditBuffer, newEnd: number): EditOp[] {
  if (newEnd <= 0) {
    // everything deleted: cursor to the first node's... there is no head; the
    // safest non-destructive move is to the root node (thread keeps existing
    // below as a branch). Split the root at 0 is illegal, so just move cursor
    // to the root — its full content stays but the cursor sits at its end.
    const root = buf.nodes[0]
    return root ? [{ op: 'moveCursor', target: { kind: 'id', id: root.id } }] : []
  }
  const node = nodeAtOffset(buf, newEnd)
  if (!node) return []
  // at a node boundary: move cursor to the last fully-kept node (the node whose
  // end == newEnd)
  if (newEnd === node.start) {
    // boundary at this node's start -> last kept node is the previous one
    const idx = buf.nodes.indexOf(node)
    const kept = buf.nodes[idx - 1]
    return kept ? [{ op: 'moveCursor', target: { kind: 'id', id: kept.id } }] : []
  }
  if (newEnd === node.end) {
    return [{ op: 'moveCursor', target: { kind: 'id', id: node.id } }]
  }
  // mid-node: split so the head holds the kept prefix; cursor lands on the head
  const within = newEnd - node.start
  const at =
    node.kind === 'tokens'
      ? tokenIndexFloor(node, newEnd)
      : codePointOffset(node.text, within)
  if (at <= 0) {
    // rounded down to the node start (token-aligned): keep the previous node
    const idx = buf.nodes.indexOf(node)
    const kept = buf.nodes[idx - 1]
    return kept
      ? [{ op: 'moveCursor', target: { kind: 'id', id: kept.id } }]
      : [{ op: 'moveCursor', target: { kind: 'id', id: node.id } }]
  }
  return [
    { op: 'split', nodeId: node.id, at, kind: node.kind },
    // head keeps the node id -> just move cursor to it
    { op: 'moveCursor', target: { kind: 'id', id: node.id } },
  ]
}

// -------------------------------------------------- mid-thread edit (the core)

function planMidEdit(buf: EditBuffer, d: BufferDiff, identity: string): EditOp[] {
  const ops: EditOp[] = []
  const prefixNode = nodeAtOffset(buf, d.prefix)
  const endNode = nodeAtOffset(buf, d.oldEnd)
  if (!prefixNode || !endNode) return []

  // ---- edit at an ANCESTOR (task #6): the edit touches the span of a
  // non-leaf thread node A while the cursor sits at a descendant. Instead of
  // splitting A and copying the downstream chain node-by-node (the old
  // behavior — it restructured the original branch), produce ONE new SIBLING
  // of A holding the full consolidated edited text of the A..leaf path. The
  // original branch stays COMPLETELY untouched — not even structurally.
  // (Leaf-only edits keep the split/hybrid path below: it preserves token
  // granularity, which consolidation deliberately gives up.)
  const lastIdx = buf.nodes.length - 1
  const startIdx = buf.nodes.indexOf(prefixNode)
  const endIdxEarly = buf.nodes.indexOf(endNode)
  if (!(startIdx === lastIdx && endIdxEarly === lastIdx)) {
    const a = buf.nodes[startIdx]
    const prev = buf.nodes[startIdx - 1]
    // consolidated text == the post-edit buffer from A's start onward
    const text =
      buf.text.slice(a.start, d.prefix) + d.inserted + buf.text.slice(d.oldEnd)
    return [
      {
        op: 'buildEdited',
        parent: prev ? { kind: 'id', id: prev.id } : { kind: 'root' },
        content: { type: 'snippet', text },
        creator: { type: 'human', label: identity },
        metadata: {
          edited_by: identity,
          edited_from: buf.nodes.slice(startIdx).map((n) => n.id),
        },
        moveCursor: true,
      },
    ]
  }

  // (1) split the prefix-end node at the prefix boundary so the head keeps the
  // shared prefix. If the prefix lands exactly on a node boundary, no split —
  // the head is the node ending at `prefix` (or the chain root for prefix 0).
  let headRef: ParentRef
  let prefixWithinNode = d.prefix - prefixNode.start
  // when prefix sits exactly on prefixNode.start, nodeAtOffset gave us the node
  // STARTING there (later-wins) -> the real "head" is the previous node.
  if (d.prefix === prefixNode.start) {
    const idx = buf.nodes.indexOf(prefixNode)
    const prev = buf.nodes[idx - 1]
    headRef = prev ? { kind: 'id', id: prev.id } : { kind: 'root' }
  } else if (d.prefix === prefixNode.end) {
    headRef = { kind: 'id', id: prefixNode.id }
  } else {
    // strictly inside prefixNode -> split it
    const at =
      prefixNode.kind === 'tokens'
        ? tokenIndexFloor(prefixNode, d.prefix)
        : codePointOffset(prefixNode.text, prefixWithinNode)
    if (at <= 0) {
      // token-aligned floor put us at the node start: head is the previous node
      const idx = buf.nodes.indexOf(prefixNode)
      const prev = buf.nodes[idx - 1]
      headRef = prev ? { kind: 'id', id: prev.id } : { kind: 'root' }
    } else {
      ops.push({ op: 'split', nodeId: prefixNode.id, at, kind: prefixNode.kind })
      headRef = { kind: 'id', id: prefixNode.id }
    }
  }

  // recompute prefixWithinNode against the (possibly token-floored) actual head
  // boundary so the preserved within-node suffix below is consistent.
  // For tokens nodes, the kept-prefix end is the floored token start.
  let keptPrefixEnd = d.prefix
  if (prefixNode.kind === 'tokens' && d.prefix > prefixNode.start && d.prefix < prefixNode.end) {
    const at = tokenIndexFloor(prefixNode, d.prefix)
    keptPrefixEnd = prefixNode.tokenStarts[at]
  }

  // (2) preserved within-node suffix of the endNode (the kept tail of the node
  // the edit ends inside) + downstream nodes.
  const endIdx = buf.nodes.indexOf(endNode)

  // does the edit start and end inside the SAME single node?
  const single = prefixNode === endNode

  if (single && prefixNode.kind === 'tokens') {
    // HYBRID: model attribution preserved. New node = edited middle (logprob
    // null) + preserved suffix tokens (each flagged inexact).
    const node = prefixNode
    const suffixTokens = tokensFromOffset(node, d.oldEnd).map((t) => ({
      ...t,
      inexact: true,
    }))
    const middleToken: Token = {
      text: d.inserted,
      logprob: null,
      token_id: null,
      entropy: null,
      top_logprobs: [],
      inexact: false,
    }
    const tokens: Token[] = []
    if (d.inserted !== '') tokens.push(middleToken)
    tokens.push(...suffixTokens)
    ops.push({
      op: 'buildEdited',
      parent: headRef,
      content: { type: 'tokens', tokens },
      creator: node.creator,
      metadata: { edited_by: identity, edited_from: node.id },
      moveCursor: buf.nodes.length === endIdx + 1, // cursor here iff no downstream
    })
  } else {
    // spans multiple nodes or human/snippet text -> attribute to ME (human).
    // The new snippet node holds: edited middle + the kept within-node suffix of
    // the END node (as plain text; we lose token granularity for cross-node /
    // human edits, by design).
    const keptSuffix = endNode.text.slice(d.oldEnd - endNode.start)
    const editedFrom = buf.nodes
      .slice(buf.nodes.indexOf(prefixNode), endIdx + 1)
      .map((n) => n.id)
    ops.push({
      op: 'buildEdited',
      parent: headRef,
      content: { type: 'snippet', text: d.inserted + keptSuffix },
      creator: { type: 'human', label: identity },
      metadata: { edited_by: identity, edited_from: editedFrom },
      moveCursor: buf.nodes.length === endIdx + 1,
    })
  }

  // (3) downstream nodes entirely after the edit -> copy verbatim onto the branch
  for (let i = endIdx + 1; i < buf.nodes.length; i++) {
    ops.push({
      op: 'copy',
      sourceId: buf.nodes[i].id,
      parent: { kind: 'lastCreated' },
      moveCursor: i === buf.nodes.length - 1, // cursor to the deepest new node
    })
  }
  return ops
}

/** Tokens of a tokens node at or after buffer offset `off`, splitting on the
 * floored token start (a partially-edited token is dropped from the suffix). */
function tokensFromOffset(node: BufNode, off: number): Token[] {
  // first token whose START is >= off; a token straddling `off` is consumed by
  // the edited middle (it is not preserved verbatim).
  let i = 0
  while (i < node.tokens.length && node.tokenStarts[i] < off) i++
  return node.tokens.slice(i)
}
