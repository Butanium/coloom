// Global keyboard shortcut suite (spec: docs/ui-specs/shared-state.md §3, adapted
// to coloom's named-cursor model: navigation moves MY cursor, never an "active path").
// Keys are user-configurable: dispatch goes through the keybindings table
// (keybindings.svelte.ts); only Escape is hardwired. Mounted once from
// Editor.svelte via $effect(() => initKeyboard()).

import {
  ACTIONS,
  bindingFor,
  capture,
  matchesBinding,
  type ActionId,
} from './keybindings.svelte'
import { clearSelection } from './selection.svelte'
import {
  closeContextMenu,
  collapsed,
  contextMenu,
  deleteNode,
  generateAt,
  identity,
  moveMyCursor,
  myCursorNodeId,
  sendViewCommand,
  session,
  toggleActiveGenerator,
  toggleBookmark,
  toggleCollapsed,
  visibleGenerators,
} from './state.svelte'

function isEditableTarget(t: EventTarget | null): boolean {
  if (!(t instanceof HTMLElement)) return false
  if (t.isContentEditable) return true
  const tag = t.tagName
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT'
}

// Sticky child navigation: going back toward the leaves returns to the child
// you LAST visited, not children[0]. Per-window memory, ids validated against
// the live children list (stale entries from deleted nodes just fall through).
const lastVisitedChild = new Map<string, string>()

/** Resolve cursor navigation from my cursor node; null = no move (no wrap). */
function navigationTarget(dir: 'parent' | 'child' | 'prev' | 'next'): string | null {
  const weave = session.weave
  if (!weave) return null
  const cur = myCursorNodeId()
  if (!cur) return null
  const node = weave.nodes[cur]
  if (!node) return null
  if (dir === 'parent') {
    const parent = node.parents[0] ?? null
    if (parent !== null) lastVisitedChild.set(parent, cur) // remember the way back
    return parent
  }
  if (dir === 'child') {
    const remembered = lastVisitedChild.get(cur)
    const child =
      remembered !== undefined && node.children.includes(remembered)
        ? remembered
        : node.children[0]
    if (child === undefined) return null
    collapsed.delete(cur) // auto-expand so the move is visible
    return child
  }
  // siblings: children of my parent, or the roots if I'm a root
  const parentId = node.parents[0]
  const siblings =
    parentId !== undefined ? (weave.nodes[parentId]?.children ?? []) : weave.roots
  const idx = siblings.indexOf(cur)
  if (idx === -1) return null
  const next = dir === 'prev' ? idx - 1 : idx + 1
  if (next < 0 || next >= siblings.length) return null // clamp, no wrap
  if (parentId !== undefined) lastVisitedChild.set(parentId, siblings[next])
  return siblings[next]
}

/** Delete my cursor node (cascades its subtree); cursor → parent FIRST so the
 * server never sees my cursor inside the deleted subtree. */
async function deleteCursorNode() {
  const weave = session.weave
  if (!weave) return
  const cur = myCursorNodeId()
  if (!cur) return
  const node = weave.nodes[cur]
  if (!node) return
  const subtree = node.children.length > 0 ? ' and its entire subtree' : ''
  if (!confirm(`Delete this node${subtree}? (${identity.name}'s cursor moves to the parent)`)) return
  const parent = node.parents[0]
  if (parent !== undefined) await moveMyCursor(parent)
  await deleteNode(cur)
}

/** Move my cursor along the tree; false = clamped (let the browser have the key). */
function navigate(dir: 'parent' | 'child' | 'prev' | 'next'): boolean {
  const target = navigationTarget(dir)
  if (target === null) return false
  void moveMyCursor(target)
  sendViewCommand('center-node', target)
  return true
}

function generate(opts: { moveCursor?: boolean } = {}): boolean {
  const cur = myCursorNodeId()
  if (cur === null) return false
  void generateAt(cur, opts)
  return true
}

/** Toggle the k-th visible generator chip (sampler or preset) on/off. */
function toggleGenerator(k: number): boolean {
  const gen = visibleGenerators()[k - 1]
  if (gen === undefined) return false
  toggleActiveGenerator(gen.ref)
  return true
}

// Each handler returns true when it acted (→ preventDefault); false lets the
// browser keep the key (e.g. arrow at a clamped edge, digit with no chip).
const handlers: Record<ActionId, () => boolean> = {
  move_to_parent: () => navigate('parent'),
  move_to_child: () => navigate('child'),
  move_to_prev_sibling: () => navigate('prev'),
  move_to_next_sibling: () => navigate('next'),
  generate_at_cursor: () => generate(),
  generate_at_cursor_move: () => generate({ moveCursor: true }),
  toggle_bookmark: () => {
    const cur = myCursorNodeId()
    if (cur === null) return false
    void toggleBookmark(cur)
    return true
  },
  toggle_collapsed: () => {
    const cur = myCursorNodeId()
    if (cur === null) return false
    toggleCollapsed(cur)
    return true
  },
  delete_current: () => {
    void deleteCursorNode()
    return true
  },
  fit_to_cursor: () => {
    sendViewCommand('fit-cursor')
    return true
  },
  fit_to_weave: () => {
    sendViewCommand('fit-weave')
    return true
  },
  focus_search: () => {
    const input = document.querySelector('[data-search-input]')
    if (!(input instanceof HTMLElement)) return false
    input.focus()
    return true
  },
  generator_1: () => toggleGenerator(1),
  generator_2: () => toggleGenerator(2),
  generator_3: () => toggleGenerator(3),
  generator_4: () => toggleGenerator(4),
  generator_5: () => toggleGenerator(5),
  generator_6: () => toggleGenerator(6),
  generator_7: () => toggleGenerator(7),
  generator_8: () => toggleGenerator(8),
  generator_9: () => toggleGenerator(9),
}

const ALT_NAV: Record<string, 'parent' | 'child' | 'prev' | 'next'> = {
  ArrowLeft: 'parent',
  ArrowRight: 'child',
  ArrowUp: 'prev',
  ArrowDown: 'next',
}

function handleKeydown(e: KeyboardEvent) {
  // the keybindings dialog is capturing a combo: it owns every key, Escape included
  if (capture.suppressed) return
  // hardwired Alt+Arrow nav aliases: modified combos aren't text entry, so these
  // work even while focus is in the doc / an input (plain arrows stay caret moves)
  if (e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey && e.key in ALT_NAV) {
    if (session.weave === null || contextMenu.open) return
    if (navigate(ALT_NAV[e.key])) e.preventDefault()
    return
  }
  // never steal keys from text entry
  if (isEditableTarget(e.target)) return

  if (e.key === 'Escape') {
    closeContextMenu()
    clearSelection()
    session.hoveredNodeId = null
    e.preventDefault()
    return
  }

  if (session.weave === null) return
  // context menu open = modal layer: suppress everything but Escape (handled above)
  if (contextMenu.open) return

  // binding-table dispatch, exact modifier match; unmatched combos (incl. all
  // ctrl/cmd/alt chords the user hasn't bound) stay with the browser
  for (const action of ACTIONS) {
    const b = bindingFor(action.id)
    if (b === null || !matchesBinding(e, b)) continue
    if (handlers[action.id]()) e.preventDefault()
    return // first match wins
  }
}

/** Attach the global keydown listener; returns its cleanup (for an Editor $effect). */
export function initKeyboard(): () => void {
  window.addEventListener('keydown', handleKeydown)
  return () => window.removeEventListener('keydown', handleKeydown)
}
