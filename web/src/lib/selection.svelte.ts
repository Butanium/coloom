// Per-client canvas multi-select: a plain reactive set of node ids.
// Local-only — never synced to other clients, never persisted in the profile.
// Stale ids (nodes deleted since selection) are harmless: every consumer reads
// through validSelection(), which filters against the live weave.

import { SvelteSet } from 'svelte/reactivity'
import { deleteNodes, session } from './state.svelte'
import type { Weave } from './types'

export const selected = new SvelteSet<string>()

export function toggleSelected(id: string) {
  if (selected.has(id)) selected.delete(id)
  else selected.add(id)
}

export function clearSelection() {
  selected.clear()
}

export function selectMany(ids: Iterable<string>) {
  for (const id of ids) selected.add(id)
}

/** Selected ids that still exist in the live weave. */
export function validSelection(): string[] {
  const weave = session.weave
  if (!weave) return []
  return [...selected].filter((id) => id in weave.nodes)
}

/** Topmost selected ids only: a selected descendant of another selected node
 * is already covered by the ancestor's delete cascade. */
function topmostOnly(weave: Weave, sel: string[]): string[] {
  const set = new Set(sel)
  return sel.filter((id) => {
    let cur = weave.nodes[id]?.parents[0]
    while (cur !== undefined) {
      if (set.has(cur)) return false
      cur = weave.nodes[cur]?.parents[0]
    }
    return true
  })
}

/** Bulk-delete the whole selection: ONE undo batch + "undo" toast
 * (deleteNodes), no confirmation, selection cleared after. Shared by the
 * SelectionBar "delete all" button and the Delete key (item 7).
 * Returns false when there is nothing selected to delete. */
export async function deleteSelection(): Promise<boolean> {
  const weave = session.weave
  const ids = validSelection()
  if (!weave || ids.length === 0) return false
  await deleteNodes(topmostOnly(weave, ids))
  clearSelection()
  return true
}
