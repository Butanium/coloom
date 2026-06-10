// Per-client canvas multi-select: a plain reactive set of node ids.
// Local-only — never synced to other clients, never persisted in the profile.
// Stale ids (nodes deleted since selection) are harmless: every consumer reads
// through validSelection(), which filters against the live weave.

import { SvelteSet } from 'svelte/reactivity'
import { session } from './state.svelte'

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
