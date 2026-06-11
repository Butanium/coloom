/** Svelte action: Tapestry/egui-style DragValue on a numeric input.
 *
 * Press + drag horizontally (or vertically) to adjust the value: right/up
 * increases, left/down decreases (delta = dx − dy, like egui's DragValue).
 * A plain click (<4px movement) falls through to normal focus + typing.
 * Convention: no pointer capture before the 4px drag threshold.
 */

export interface DragnumParams {
  /** value change per pixel of drag */
  speed: number
  min?: number
  max?: number
  /** round to this many decimals (0 = integers) */
  decimals: number
  /** current value; null = no override yet (drag seeds from `seed()`) */
  get: () => number | null
  /** called every frame during the drag — LOCAL visual update only; persist
   * in `dragEnd` (commit-on-release, docs/optimistic-state.md leg-3 corollary) */
  set: (v: number) => void
  /** starting value when get() is null (e.g. the generator's own param) */
  seed: () => number
  /** the 4px threshold was crossed: a real drag began (gesture start) */
  dragStart?: () => void
  /** a real drag ended (pointer released after dragging): commit here */
  dragEnd?: () => void
}

const THRESHOLD_PX = 4

export function dragnum(node: HTMLElement, params: DragnumParams) {
  let p = params
  let startX = 0
  let startY = 0
  let startValue = 0
  let pointerId: number | null = null
  let dragging = false

  function clampRound(v: number): number {
    if (p.min !== undefined) v = Math.max(p.min, v)
    if (p.max !== undefined) v = Math.min(p.max, v)
    const f = 10 ** p.decimals
    return Math.round(v * f) / f
  }

  function onPointerDown(e: PointerEvent) {
    if (e.button !== 0) return
    pointerId = e.pointerId
    startX = e.clientX
    startY = e.clientY
    startValue = p.get() ?? p.seed()
    dragging = false
    // capture immediately (deviation from the canvas 4px-first rule: the
    // target is the input itself, so there is no child gesture to hijack,
    // and a fast drag would otherwise escape before its first pointermove)
    node.setPointerCapture(pointerId)
  }

  function onPointerMove(e: PointerEvent) {
    if (pointerId === null || e.pointerId !== pointerId) return
    const dx = e.clientX - startX
    const dy = e.clientY - startY
    if (!dragging) {
      if (Math.hypot(dx, dy) < THRESHOLD_PX) return
      dragging = true
      node.classList.add('dragnum-dragging')
      // dragStart BEFORE blur: the blur ends a focus gesture, and overlapping
      // gesture depths must never hit zero mid-drag (authority would lapse)
      p.dragStart?.()
      if (node instanceof HTMLInputElement) node.blur()
    }
    e.preventDefault()
    p.set(clampRound(startValue + (dx - dy) * p.speed))
  }

  function onPointerEnd(e: PointerEvent) {
    if (pointerId === null || e.pointerId !== pointerId) return
    if (dragging) {
      node.releasePointerCapture(pointerId)
      node.classList.remove('dragnum-dragging')
      p.dragEnd?.() // commit-on-release
    }
    pointerId = null
    dragging = false
  }

  function onClick(e: MouseEvent) {
    // swallow the click that ends a drag so it doesn't refocus the input
    const dx = e.clientX - startX
    const dy = e.clientY - startY
    if (Math.hypot(dx, dy) >= THRESHOLD_PX) {
      e.preventDefault()
      e.stopPropagation()
    }
  }

  node.addEventListener('pointerdown', onPointerDown)
  node.addEventListener('pointermove', onPointerMove)
  node.addEventListener('pointerup', onPointerEnd)
  node.addEventListener('pointercancel', onPointerEnd)
  node.addEventListener('click', onClick, true)
  node.classList.add('dragnum')

  return {
    update(next: DragnumParams) {
      p = next
    },
    destroy() {
      node.removeEventListener('pointerdown', onPointerDown)
      node.removeEventListener('pointermove', onPointerMove)
      node.removeEventListener('pointerup', onPointerEnd)
      node.removeEventListener('pointercancel', onPointerEnd)
      node.removeEventListener('click', onClick, true)
    },
  }
}
