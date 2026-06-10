// User-configurable keybindings: every global shortcut is a named action with a
// rebindable combo. keyboard.svelte.ts dispatches through this table; the
// KeybindingsDialog edits it. Escape is hardwired in keyboard.svelte.ts
// (close menu / cancel capture) and deliberately not an action here.

import { onProfileLogin, setSetting } from './profile.svelte'

export interface Binding {
  key: string // KeyboardEvent.key as delivered (Shift+g arrives as 'G')
  ctrl: boolean
  alt: boolean
  shift: boolean
  meta: boolean
}

export type ActionId =
  | 'move_to_parent'
  | 'move_to_child'
  | 'move_to_prev_sibling'
  | 'move_to_next_sibling'
  | 'generate_at_cursor'
  | 'generate_at_cursor_move'
  | 'toggle_bookmark'
  | 'toggle_collapsed'
  | 'delete_current'
  | 'undo'
  | 'fit_to_cursor'
  | 'fit_to_weave'
  | 'focus_search'
  | `generator_${1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9}`

export interface Action {
  id: ActionId
  label: string
  category: 'navigation' | 'weaving' | 'node' | 'view' | 'generators'
}

export const ACTIONS: Action[] = [
  { id: 'move_to_parent', label: 'move cursor to parent', category: 'navigation' },
  { id: 'move_to_child', label: 'move cursor to first child', category: 'navigation' },
  { id: 'move_to_prev_sibling', label: 'move cursor to previous sibling', category: 'navigation' },
  { id: 'move_to_next_sibling', label: 'move cursor to next sibling', category: 'navigation' },
  { id: 'generate_at_cursor', label: 'generate at cursor', category: 'weaving' },
  { id: 'generate_at_cursor_move', label: 'generate and follow', category: 'weaving' },
  { id: 'toggle_bookmark', label: 'toggle bookmark', category: 'node' },
  { id: 'toggle_collapsed', label: 'collapse / expand subtree', category: 'node' },
  { id: 'delete_current', label: 'delete node (and subtree)', category: 'node' },
  { id: 'undo', label: 'undo (restore deleted nodes)', category: 'node' },
  { id: 'fit_to_cursor', label: 'center view on my cursor', category: 'view' },
  { id: 'fit_to_weave', label: 'fit whole weave', category: 'view' },
  { id: 'focus_search', label: 'focus tree search', category: 'view' },
  ...Array.from({ length: 9 }, (_, i) => ({
    id: `generator_${i + 1}` as ActionId,
    label: `toggle generator ${i + 1}`,
    category: 'generators' as const,
  })),
]

function bind(key: string, mods: Partial<Omit<Binding, 'key'>> = {}): Binding {
  return { key, ctrl: false, alt: false, shift: false, meta: false, ...mods }
}

export const DEFAULT_BINDINGS: Record<ActionId, Binding> = {
  move_to_parent: bind('ArrowLeft'),
  move_to_child: bind('ArrowRight'),
  move_to_prev_sibling: bind('ArrowUp'),
  move_to_next_sibling: bind('ArrowDown'),
  generate_at_cursor: bind('Enter', { ctrl: true }),
  generate_at_cursor_move: bind('Enter', { ctrl: true, shift: true }),
  toggle_bookmark: bind('b'),
  toggle_collapsed: bind('c'),
  delete_current: bind('Delete'),
  undo: bind('z', { ctrl: true }),
  fit_to_cursor: bind('9', { ctrl: true }),
  fit_to_weave: bind('0', { ctrl: true }),
  focus_search: bind('/'),
  generator_1: bind('1'),
  generator_2: bind('2'),
  generator_3: bind('3'),
  generator_4: bind('4'),
  generator_5: bind('5'),
  generator_6: bind('6'),
  generator_7: bind('7'),
  generator_8: bind('8'),
  generator_9: bind('9'),
}

/** EXACT modifier match: Ctrl+S must not fire on Ctrl+Shift+S. */
export function matchesBinding(e: KeyboardEvent, b: Binding): boolean {
  return (
    e.key === b.key &&
    e.ctrlKey === b.ctrl &&
    e.altKey === b.alt &&
    e.shiftKey === b.shift &&
    e.metaKey === b.meta
  )
}

export function bindingFromEvent(e: KeyboardEvent): Binding {
  return { key: e.key, ctrl: e.ctrlKey, alt: e.altKey, shift: e.shiftKey, meta: e.metaKey }
}

function displayKey(key: string): string {
  if (key === ' ') return 'Space'
  if (key.startsWith('Arrow')) return key.slice('Arrow'.length)
  return key.length === 1 ? key.toUpperCase() : key
}

export function formatBinding(b: Binding): string {
  const parts: string[] = []
  if (b.ctrl) parts.push('Ctrl')
  if (b.alt) parts.push('Alt')
  if (b.shift) parts.push('Shift')
  if (b.meta) parts.push('Meta')
  parts.push(displayKey(b.key))
  return parts.join('+')
}

/** Identity key for conflict detection (display strings can collide, e.g. g vs Shift+G can't). */
export function bindingId(b: Binding): string {
  return [b.ctrl, b.alt, b.shift, b.meta, b.key].join('|')
}

// ---------------------------------------------------------------- persistence
// Overrides live in PROFILE settings (roaming, server-side) with localStorage
// as the pre-login fallback. Only overrides are stored; null = explicitly
// unbound.

const STORAGE_KEY = 'coloom.keybindings'

function isBinding(v: unknown): v is Binding {
  if (typeof v !== 'object' || v === null) return false
  const b = v as Binding
  return (
    typeof b.key === 'string' &&
    typeof b.ctrl === 'boolean' &&
    typeof b.alt === 'boolean' &&
    typeof b.shift === 'boolean' &&
    typeof b.meta === 'boolean'
  )
}

function sanitizeOverrides(raw: unknown): Partial<Record<ActionId, Binding | null>> {
  const out: Partial<Record<ActionId, Binding | null>> = {}
  if (raw && typeof raw === 'object') {
    for (const [id, v] of Object.entries(raw)) {
      if (id in DEFAULT_BINDINGS && (v === null || isBinding(v))) {
        out[id as ActionId] = v as Binding | null
      }
    }
  }
  return out
}

function loadOverrides(): Partial<Record<ActionId, Binding | null>> {
  try {
    return sanitizeOverrides(JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}'))
  } catch {
    return {} // unparseable stored state = fresh defaults
  }
}

function saveOverrides() {
  // localStorage = pre-login fallback; profile settings = roaming truth
  localStorage.setItem(STORAGE_KEY, JSON.stringify(bindings.overrides))
  setSetting('keybindings', $state.snapshot(bindings.overrides))
}

export const bindings = $state<{ overrides: Partial<Record<ActionId, Binding | null>> }>({
  overrides: loadOverrides(),
})

// profile settings override the local fallback on login; profiles that never
// stored keybindings get seeded from the current local state
onProfileLogin((settings) => {
  if (settings.keybindings !== undefined) {
    bindings.overrides = sanitizeOverrides(settings.keybindings)
  } else if (Object.keys(bindings.overrides).length > 0) {
    saveOverrides()
  }
})

/** Effective binding for an action; null = unbound. */
export function bindingFor(id: ActionId): Binding | null {
  const override = bindings.overrides[id]
  return override === undefined ? DEFAULT_BINDINGS[id] : override
}

export function setBinding(id: ActionId, b: Binding | null) {
  bindings.overrides[id] = b
  saveOverrides()
}

export function resetBindings() {
  bindings.overrides = {}
  saveOverrides()
}

// While true (the dialog is capturing a combo) keyboard.svelte.ts dispatches NOTHING.
export const capture = $state({ suppressed: false })
