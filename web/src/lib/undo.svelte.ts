// Undo stack: destructive actions push an INVERSE operation (a closure that
// calls the server's restore endpoint) instead of asking for confirmation
// up front. Ctrl+Z (the rebindable 'undo' action) and the post-delete toast's
// "undo" button both run entries from here.
//
// Currently covers node delete + bulk delete; in-editor TEXT undo stays
// deferred (the keyboard handler never intercepts Ctrl+Z while typing).
//
// Deliberately import-free: state.svelte.ts builds the inverse closures (it
// owns the api client + refetch machinery) and pushes them through pushUndo.

export interface UndoEntry {
  id: number
  /** Human description of the undoable action, e.g. 'deleted 3 nodes'. */
  label: string
  /** Weave the action happened in; Ctrl+Z only pops entries for the open weave. */
  weaveId: string | null
  /** The inverse op. Throws on failure — callers surface it, never swallow. */
  run: () => Promise<void>
}

const STACK_CAP = 50

export const undoStack = $state<{ items: UndoEntry[] }>({ items: [] })
let nextId = 1

export function pushUndo(
  label: string,
  weaveId: string | null,
  run: () => Promise<void>,
): UndoEntry {
  const entry: UndoEntry = { id: nextId++, label, weaveId, run }
  undoStack.items.push(entry)
  if (undoStack.items.length > STACK_CAP) {
    undoStack.items = undoStack.items.slice(-STACK_CAP)
  }
  return entry
}

/** Undo a SPECIFIC entry (the toast's "undo" button targets the deletion that
 * spawned it, not whatever is on top). No-op false if already undone. */
export async function undoEntry(entry: UndoEntry): Promise<boolean> {
  const idx = undoStack.items.findIndex((e) => e.id === entry.id)
  if (idx === -1) return false
  undoStack.items.splice(idx, 1)
  await entry.run()
  return true
}

/** Pop and run the most recent entry for `weaveId` (Ctrl+Z). Returns the
 * entry when one ran, null when the stack had nothing for this weave. */
export async function undoLast(weaveId: string | null): Promise<UndoEntry | null> {
  for (let i = undoStack.items.length - 1; i >= 0; i--) {
    if (undoStack.items[i].weaveId !== weaveId) continue
    const [entry] = undoStack.items.splice(i, 1)
    await entry.run()
    return entry
  }
  return null
}
