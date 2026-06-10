// Creator → color: human-vs-agent attribution is coloom's default lens.
// Explicit creator.color wins; otherwise humans share a warm hue, models get
// stable cool hues hashed from their label.

import type { Creator, Token } from './types'

export const HUMAN_COLOR = '#e8a33d'
const MODEL_HUES = [210, 265, 175, 320, 145, 235, 25] // cool-ish, distinct

function hashLabel(label: string): number {
  let h = 0
  for (let i = 0; i < label.length; i++) h = (h * 31 + label.charCodeAt(i)) | 0
  return Math.abs(h)
}

export function creatorColor(creator: Creator): string {
  if (creator.type !== 'unknown' && creator.color) return creator.color
  if (creator.type === 'human') return HUMAN_COLOR
  if (creator.type === 'model') {
    const hue = MODEL_HUES[hashLabel(creator.label) % MODEL_HUES.length]
    return `hsl(${hue}, 62%, 62%)`
  }
  return '#8a8a93'
}

export function creatorLabel(creator: Creator): string {
  return creator.type === 'unknown' ? 'unknown' : creator.label
}

// Text fill for node/token TEXT. The saturated creatorColor() at 62% lightness,
// further dimmed by tokenOpacity over the dark background, was near-invisible
// (Tapestry renders text in the theme's light default and uses color only as
// an accent). So: text gets the creator hue mixed into the light theme text
// color — readable at any opacity — while borders/pills/accents keep the
// saturated creatorColor(). The mix is kept low (28%) so the result stays close
// to the light --text and never darkens below it: enough hue to tell human (warm)
// from model (cool) apart, never enough to sink into the dark background.
export function creatorTextColor(creator: Creator): string {
  return `color-mix(in oklab, ${creatorColor(creator)} 28%, var(--text))`
}

export function cursorColor(name: string): string {
  const hue = (hashLabel(name) * 7) % 360
  return `hsl(${hue}, 70%, 60%)`
}

// Bookmark stroke colors (Tapestry dark theme: selection.bg_fill / selection.stroke).
// Bookmark color BEATS thread color on card borders; the pale variant marks
// bookmarked nodes that are also on my cursor thread (canvas spec §3.2).
export const BOOKMARK_COLOR = '#005c80'
export const BOOKMARK_ON_THREAD_COLOR = '#c0deff'

// Tapestry's token-opacity curve (canvas spec §3.3, shared.rs:1016-1047):
//   prob_term = 1 − ln(1/clamp(p, ε, 1)) / 10     p=1 → 1.0, p=e⁻¹ → 0.9, p≈4.5e−5 → 0
//   conf_term = confidence / (ln(k) + 2)          only when a confidence metric exists
//   opacity   = clamp(min(prob_term, conf_term), 0.65, 1.0)
// coloom delta: our tokens carry `entropy` (in nats, over the sampled distribution)
// instead of Tapestry's confidence/confidence_k pair. We use
//   conf_term = 1 − entropy / (ln(k) + 2),  k = top_logprobs.length (k ≥ 2)
// so entropy 0 (certain) → 1.0 and entropy at ln(k)+2 nats → 0, mirroring the
// shape of Tapestry's term; same [0.65, 1] clamp.
export function tokenOpacity(token: Token, minOpacity = 0.65): number {
  if (token.logprob == null) return 1
  const p = Math.exp(token.logprob)
  let term = 1 - Math.log(1 / Math.max(p, 1e-10)) / 10
  const k = token.top_logprobs.length
  if (token.entropy != null && k >= 2) {
    term = Math.min(term, 1 - token.entropy / (Math.log(k) + 2))
  }
  return Math.max(minOpacity, Math.min(term, 1))
}
