// Left-to-right tidy tree layout with fixed-width, variable-height cards.
// Tree-only for now (first parent wins); swap for dagre/elk when DAG merges land.

import type { Weave } from './types'
import { nodeText } from './types'

export const CARD_W = 250
// Spec unit `pad` (canvas spec §0, M ≈ monospace row height). Everything in the
// strip right of a card is sized off it: the button sits at x ∈ [pad, 4*pad]
// past the card edge, the invisible hover strip extends to 5*pad (spec §1.6/§4).
export const PAD = 12
export const STRIP_W = PAD * 5
export const BTN = 24 // spec: M * 1.75 (M grew with the 15px body text)
// Column gap = strip width, so each node's hover strip exactly fills the gap
// to the next column.
export const CARD_GAP_X = STRIP_W
// Row gap must clear the hover toolbar that hangs below a card (4 + TOOL_H = 28px)
// AND the cursor pills that float above the next card (~24px) — otherwise the
// next card's foreignObject text intercepts toolbar clicks (playwright-caught).
export const CARD_GAP_Y = 34
export const ROOT_GAP_Y = 62
// Card body text is 15px monospace, line-height 20px (NodeCard .text). These
// MUST track that CSS or text overflows / gets clipped: LINE_H = the line box,
// CHARS_PER_LINE = how many 15px-mono glyphs fit in (CARD_W − 2*TEXT_PAD).
// 15px mono advance ≈ 9px → (250 − 16)/9 ≈ 26; round DOWN to 25 so the
// estimate over-counts lines (taller card) rather than clipping.
const TEXT_PAD = 8
const LINE_H = 20
const CHARS_PER_LINE = 25
const MAX_LINES = 7

export interface CardBox {
  x: number
  y: number
  w: number
  h: number
}

export interface WeaveLayout {
  boxes: Map<string, CardBox>
  width: number
  height: number
}

export function cardHeight(text: string): number {
  let lines = 0
  for (const seg of text.split('\n')) {
    lines += Math.max(1, Math.ceil(seg.length / CHARS_PER_LINE))
  }
  return TEXT_PAD * 2 + LINE_H * Math.min(Math.max(lines, 1), MAX_LINES)
}

export function layoutWeave(weave: Weave, collapsed: ReadonlySet<string>): WeaveLayout {
  const boxes = new Map<string, CardBox>()
  let maxX = 0

  // Returns the subtree's vertical extent [top, bottom]; cursor `nextY` advances
  // as leaves are placed.
  let nextY = 0
  function place(id: string, depth: number): [number, number] {
    const node = weave.nodes[id]
    const h = cardHeight(nodeText(node))
    const x = depth * (CARD_W + CARD_GAP_X)
    maxX = Math.max(maxX, x + CARD_W)
    const children = collapsed.has(id) ? [] : node.children.filter((c) => weave.nodes[c])
    if (children.length === 0) {
      const y = nextY
      nextY = y + h + CARD_GAP_Y
      boxes.set(id, { x, y, w: CARD_W, h })
      return [y, y + h]
    }
    let top = Infinity
    let bottom = -Infinity
    let firstMid = 0
    let lastMid = 0
    children.forEach((child, i) => {
      const [t, b] = place(child, depth + 1)
      top = Math.min(top, t)
      bottom = Math.max(bottom, b)
      const mid = (t + b) / 2
      if (i === 0) firstMid = mid
      lastMid = mid
    })
    // center on the span of child midpoints (nicer than the raw extent midpoint)
    let y = (firstMid + lastMid) / 2 - h / 2
    y = Math.max(y, top) // don't float above the first child
    boxes.set(id, { x, y, w: CARD_W, h })
    return [Math.min(top, y), Math.max(bottom, y + h)]
  }

  for (const root of weave.roots) {
    if (!weave.nodes[root]) continue
    place(root, 0)
    nextY += ROOT_GAP_Y - CARD_GAP_Y
  }

  return { boxes, width: maxX, height: Math.max(nextY - ROOT_GAP_Y, 0) }
}

// Spec §1.7 wire handles extend horizontally by WIRE_FRAME = 36; with our column
// gap the endpoints count as "close", so we soften the handle to half the span
// (in lieu of Tapestry's full wire_bezier_5 close-case battery — the tidy tree
// layout never produces backward edges, so the loop-around cases can't occur).
export const WIRE_FRAME = 36

export function edgePath(parent: CardBox, child: CardBox): string {
  const x0 = parent.x + parent.w
  const y0 = parent.y + parent.h / 2
  const x1 = child.x
  const y1 = child.y + child.h / 2
  const f = Math.max(Math.min(WIRE_FRAME, (x1 - x0) / 2), 20)
  return `M ${x0} ${y0} C ${x0 + f} ${y0}, ${x1 - f} ${y1}, ${x1} ${y1}`
}
