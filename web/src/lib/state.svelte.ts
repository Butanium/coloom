// Shared editor state (audit §9 taxonomy):
//  - shared+persistent lives in `weave` (server-canonical snapshot, refetched on events)
//  - shared+temporary: hovered node, changed node, in-flight generations
//  - per-client: identity, collapse set, selected preset, toasts
// The WS feed drives a debounced full-weave refetch — simple and always consistent;
// incremental patching is a later optimization.

import { SvelteSet } from 'svelte/reactivity'
import { api, ApiError, CLIENT_ID } from './api'
import { askConfirm } from './confirm.svelte'
import { onProfileLogin, profile, setSetting } from './profile.svelte'
import { pushUndo, undoEntry, undoLast, undoStack } from './undo.svelte'
import type {
  ActiveGen,
  Generator,
  ResolvedGenerator,
  Template,
  Tokens,
  TopLogprob,
  Weave,
  WeaveEvent,
} from './types'

// ---------------------------------------------------------------- identity
// The identity IS the profile name once logged in (cursor names, creator
// attribution). The localStorage key remains the pre-login fallback so tests
// and old sessions keep working.

const IDENTITY_KEY = 'coloom.identity'

export const identity = $state({
  name: localStorage.getItem(IDENTITY_KEY) ?? 'human',
})

export function setIdentity(name: string) {
  const trimmed = name.trim()
  if (!trimmed) return
  identity.name = trimmed
  localStorage.setItem(IDENTITY_KEY, trimmed)
}

// ---------------------------------------------------------------- toasts

export interface ToastAction {
  label: string
  run: () => void
}

export interface Toast {
  id: number
  message: string
  action?: ToastAction // e.g. the post-delete "undo" button
  kind: 'danger' | 'info' // danger = errors (default); info = neutral notices
}

export const toasts = $state<{ items: Toast[] }>({ items: [] })
let nextToastId = 1

export function toast(
  message: string,
  action?: ToastAction,
  kind: 'danger' | 'info' = 'danger',
) {
  const id = nextToastId++
  toasts.items.push({ id, message, action, kind })
  setTimeout(() => dismissToast(id), 8000)
}

export function dismissToast(id: number) {
  toasts.items = toasts.items.filter((t) => t.id !== id)
}

/** Run an async action, surfacing failures as a toast (never swallowed). */
export async function withToast<T>(action: () => Promise<T>): Promise<T | undefined> {
  try {
    return await action()
  } catch (e) {
    toast(e instanceof ApiError ? e.message : `${e}`)
    return undefined
  }
}

// ---------------------------------------------------------------- editor session

export const session = $state<{
  weave: Weave | null
  loading: boolean
  loadError: string | null
  connection: 'connecting' | 'live' | 'reconnecting'
  hoveredNodeId: string | null
  // last node someone added/changed — views focus-follow it (pointer-guarded)
  changedNodeId: string | null
  changedAt: number
  inflight: number
  // generations in flight server-side, by everyone (from gen_started/finished)
  activeGens: ActiveGen[]
  // rolling event buffer for the activity feed (backfilled on open, capped)
  events: WeaveEvent[]
  // templates + my profile's generators (docs/generators-api.md); null until
  // the first refreshGenerators() after login
  templates: Template[] | null
  generators: Generator[] | null
}>({
  weave: null,
  loading: false,
  loadError: null,
  connection: 'connecting',
  hoveredNodeId: null,
  changedNodeId: null,
  changedAt: 0,
  inflight: 0,
  activeGens: [],
  events: [],
  templates: null,
  generators: null,
})

// ------------------------------------------------------------ generators
// A GENERATOR (docs/generators-api.md) is the per-profile activatable thing,
// inheriting from a template or another generator. SEVERAL can be active at
// once — generate fans out one request per active generator. A separate
// hidden set controls which chips are displayed, and exactly ONE generator
// has FOCUS: the quick param row + drag-to-adjust edit the focused one.
// Active/hidden persist per profile (server-side settings); focus is local.

export const generatorUi = $state<{
  active: string[] // generator ids, persisted in profile settings
  hidden: string[] // generator ids, persisted in profile settings
  focusedId: string | null // last body-clicked chip; resolution in focusedGeneratorId()
  // chips whose ANCESTOR (template / parent generator) was changed by someone
  // else — tinted until focused (id → who/what for the tooltip)
  stale: Record<string, { by: string; kind: string; name: string }>
}>({ active: [], hidden: [], focusedId: null, stale: {} })

function persistGenerators() {
  setSetting('activeGenerators', [...generatorUi.active])
  setSetting('hiddenGenerators', [...generatorUi.hidden])
}

export function isGeneratorActive(id: string): boolean {
  return generatorUi.active.includes(id)
}

export function toggleActiveGenerator(id: string) {
  const activating = !isGeneratorActive(id)
  generatorUi.active = activating
    ? [...generatorUi.active, id]
    : generatorUi.active.filter((x) => x !== id)
  // an active-but-hidden chip would be invisible — activating implies shown
  if (activating) {
    generatorUi.hidden = generatorUi.hidden.filter((x) => x !== id)
  }
  persistGenerators()
}

export function isGeneratorHidden(id: string): boolean {
  return generatorUi.hidden.includes(id)
}

export function toggleHiddenGenerator(id: string) {
  generatorUi.hidden = generatorUi.hidden.includes(id)
    ? generatorUi.hidden.filter((x) => x !== id)
    : [...generatorUi.hidden, id]
  persistGenerators()
}

/** Chip display list: my generators in server order, hidden filtered out.
 * Shared by GenControls and the digit-key toggles. */
export function visibleGenerators(): Generator[] {
  return (session.generators ?? []).filter((g) => !isGeneratorHidden(g.id))
}

/** Active generators that actually exist right now (stale ids filtered). */
export function validActiveGenerators(): Generator[] {
  return (session.generators ?? []).filter((g) => isGeneratorActive(g.id))
}

/** Exactly one generator holds focus: the last body-clicked chip, defaulting
 * to the first active visible chip, else the first visible chip. */
export function focusedGenerator(): Generator | null {
  const vis = visibleGenerators()
  if (generatorUi.focusedId !== null) {
    const g = vis.find((x) => x.id === generatorUi.focusedId)
    if (g) return g
  }
  return vis.find((g) => isGeneratorActive(g.id)) ?? vis[0] ?? null
}

/** Body-click focus: binds the quick row to this generator + clears its
 * stale-ancestor badge ("I've seen it"). */
export function focusGenerator(id: string) {
  generatorUi.focusedId = id
  if (generatorUi.stale[id]) {
    delete generatorUi.stale[id]
  }
}

export function generatorById(id: string | null | undefined): Generator | null {
  return (session.generators ?? []).find((g) => g.id === id) ?? null
}

export function templateById(id: string | null | undefined): Template | null {
  return (session.templates ?? []).find((t) => t.id === id) ?? null
}

/** The parent chain of a generator, leaf-most parent first, ending at a
 * template when the chain reaches one. Cycle-safe (server rejects cycles, but
 * never trust data not to be mid-transition). */
export function ancestorsOf(gen: Generator): ({ kind: 'template'; t: Template } | { kind: 'generator'; g: Generator })[] {
  const out: ({ kind: 'template'; t: Template } | { kind: 'generator'; g: Generator })[] = []
  const seen = new Set<string>([gen.id])
  let parent = gen.parent
  while (parent) {
    if (parent.kind === 'template') {
      const t = templateById(parent.id)
      if (t) out.push({ kind: 'template', t })
      break
    }
    const g = generatorById(parent.id)
    if (!g || seen.has(g.id)) break
    seen.add(g.id)
    out.push({ kind: 'generator', g })
    parent = g.parent
  }
  return out
}

/** What `gen` INHERITS from its parent chain (its own overrides excluded) —
 * the placeholder values shown under un-overridden fields. */
export function inheritedView(gen: Generator): ResolvedGenerator {
  return mergeChain(ancestorsOf(gen))
}

/** Same, but from an arbitrary parent ref (the drawer form previews
 * placeholders for the parent currently SELECTED, not the one saved). */
export function resolveParentChain(parent: Generator['parent']): ResolvedGenerator {
  if (parent === null) return mergeChain([])
  if (parent.kind === 'template') {
    const t = templateById(parent.id)
    return mergeChain(t ? [{ kind: 'template', t }] : [])
  }
  const g = generatorById(parent.id)
  if (!g) return mergeChain([])
  // the parent's own fields count too: chain = parent + its ancestors
  return mergeChain([{ kind: 'generator', g }, ...ancestorsOf(g)])
}

function mergeChain(chain: ReturnType<typeof ancestorsOf>): ResolvedGenerator {
  const out: ResolvedGenerator = {
    base_url: null,
    model: null,
    api_key: null,
    api_key_env: null,
    params: {},
  }
  // scalars: nearest-set wins (walk leafward = chain order)
  for (const a of chain) {
    const src = a.kind === 'template' ? a.t : a.g
    out.base_url ??= src.base_url ?? null
    out.model ??= src.model ?? null
    out.api_key ??= src.api_key ?? null
    out.api_key_env ??= src.api_key_env ?? null
  }
  // params: merge root → leaf, leaf wins
  for (const a of [...chain].reverse()) {
    const src = a.kind === 'template' ? a.t : a.g
    out.params = { ...out.params, ...src.params }
  }
  return out
}

/** Generators that would be FLATTENED by deleting `kind`/`id` (direct
 * children) — surfaced in the delete confirms. */
export function directChildrenOf(kind: 'template' | 'generator', id: string): Generator[] {
  return (session.generators ?? []).filter(
    (g) => g.parent !== null && g.parent.kind === kind && g.parent.id === id,
  )
}

/** Delete a generator behind an IN-APP confirm that warns children get
 * FLATTENED (resolved fields materialized into them). Shared by the chips
 * menu and the drawer. skipConfirm (shift+click) deletes immediately.
 * Returns true when the delete ran. */
export async function confirmDeleteGenerator(
  g: Generator,
  opts: { skipConfirm?: boolean } = {},
): Promise<boolean> {
  if (!opts.skipConfirm) {
    const kids = directChildrenOf('generator', g.id)
    const warn =
      kids.length > 0
        ? `${kids.length} generator${kids.length > 1 ? 's' : ''} inherit from it (${kids
            .map((k) => k.name)
            .join(', ')}) — they will be FLATTENED: the inherited values get materialized into them.`
        : ''
    const ok = await askConfirm({
      title: `Delete generator "${g.name}"?`,
      body: warn,
      confirmLabel: 'delete',
      danger: true,
    })
    if (!ok) return false
  }
  await withToast(async () => {
    await api.deleteGenerator(g.id)
    await refreshGenerators()
  })
  return true
}

/** Ids of `gen` + everything below it — excluded from its parent picker
 * (assigning one would create a cycle). */
export function descendantIdsOf(genId: string): Set<string> {
  const out = new Set<string>([genId])
  let grew = true
  while (grew) {
    grew = false
    for (const g of session.generators ?? []) {
      if (g.parent?.kind === 'generator' && out.has(g.parent.id) && !out.has(g.id)) {
        out.add(g.id)
        grew = true
      }
    }
  }
  return out
}

export async function refreshGenerators() {
  if (profile.name === null) return
  const name = profile.name
  try {
    const [templates, generators] = await Promise.all([
      api.listTemplates(),
      api.listGenerators(name),
    ])
    if (profile.name !== name) return // logged out / switched mid-fetch
    session.templates = templates
    session.generators = generators
    // drop active/hidden ids that no longer exist (deleted generators)
    const known = new Set(generators.map((g) => g.id))
    const active = generatorUi.active.filter((id) => known.has(id))
    const hidden = generatorUi.hidden.filter((id) => known.has(id))
    if (
      active.length !== generatorUi.active.length ||
      hidden.length !== generatorUi.hidden.length
    ) {
      generatorUi.active = active
      generatorUi.hidden = hidden
      persistGenerators()
    }
  } catch (e) {
    toast(`failed to load generators: ${e}`)
  }
}

// ---- login: apply profile settings ------------------------------------------
// activeGenerators / hiddenGenerators are string[] of generator ids. Anything
// else (the pre-redesign {kind,id} refs, ids of deleted generators) is simply
// DISCARDED — dev mode, no legacy migration (Clément 2026-06-10). Discarding
// only ever touches these two keys: setSetting merges into the loaded
// settings blob, so every other profile key survives untouched.

async function applyGeneratorSettings(settings: Record<string, unknown>) {
  await refreshGenerators()
  // fetch failed → don't interpret (and NEVER persist) against an empty list;
  // the raw settings stay untouched server-side for the next login
  if (session.generators === null) return
  const generators = session.generators
  const validIds = (value: unknown): string[] =>
    Array.isArray(value)
      ? value.filter(
          (v): v is string =>
            typeof v === 'string' && generators.some((g) => g.id === v),
        )
      : []
  const act = validIds(settings.activeGenerators)
  const hid = validIds(settings.hiddenGenerators)
  generatorUi.active = act
  generatorUi.hidden = hid
  generatorUi.focusedId = null
  generatorUi.stale = {}
  const changed = (value: unknown, ids: string[]) =>
    Array.isArray(value) ? value.length !== ids.length : value !== undefined
  if (changed(settings.activeGenerators, act) || changed(settings.hiddenGenerators, hid)) {
    persistGenerators() // write back the cleaned shape
  }
}

onProfileLogin((settings) => {
  const u = settings.ui as Partial<typeof ui> | undefined
  if (u) Object.assign(ui, u)
  else persistUi()
  void applyGeneratorSettings(settings)
})

const EVENT_BUFFER_CAP = 500

// ---------------------------------------------------------------- UI state
// per-client pane/view state, persisted to localStorage

const UI_KEY = 'coloom.ui'

export type SidebarTab = 'tree' | 'children' | 'bookmarks' | 'activity' | 'info'
export type CenterTab = 'canvas' | 'graph'

function loadUi() {
  try {
    return JSON.parse(localStorage.getItem(UI_KEY) ?? '{}')
  } catch {
    return {}
  }
}

export const ui = $state<{
  sidebarTab: SidebarTab
  centerTab: CenterTab
  sidebarWidth: number
  textWidth: number
  searchQuery: string
}>({
  sidebarTab: 'tree',
  centerTab: 'canvas',
  sidebarWidth: 300,
  textWidth: 480,
  searchQuery: '',
  ...loadUi(),
})

export function persistUi() {
  const { sidebarTab, centerTab, sidebarWidth, textWidth } = ui
  const snapshot = { sidebarTab, centerTab, sidebarWidth, textWidth }
  localStorage.setItem(UI_KEY, JSON.stringify(snapshot)) // pre-login fallback
  setSetting('ui', snapshot)
}

// view command bus: panes watch `seq` and execute the latest command
export const viewCommand = $state<{
  kind: 'fit-cursor' | 'fit-weave' | 'center-node' | null
  nodeId: string | null
  seq: number
}>({ kind: null, nodeId: null, seq: 0 })

export function sendViewCommand(
  kind: 'fit-cursor' | 'fit-weave' | 'center-node',
  nodeId: string | null = null,
) {
  viewCommand.kind = kind
  viewCommand.nodeId = nodeId
  viewCommand.seq++
}

// context menu: one global menu, opened by any view on a node
export const contextMenu = $state<{
  open: boolean
  x: number
  y: number
  nodeId: string | null
}>({ open: false, x: 0, y: 0, nodeId: null })

export function openContextMenu(nodeId: string, x: number, y: number) {
  contextMenu.open = true
  contextMenu.nodeId = nodeId
  contextMenu.x = x
  contextMenu.y = y
}

export function closeContextMenu() {
  contextMenu.open = false
  contextMenu.nodeId = null
}

export const collapsed = new SvelteSet<string>()

export function toggleCollapsed(nodeId: string) {
  if (collapsed.has(nodeId)) collapsed.delete(nodeId)
  else collapsed.add(nodeId)
}

/** Root→node path for a node id (tree-shaped weaves: first parent wins). */
export function threadPath(weave: Weave, nodeId: string): string[] {
  const path: string[] = []
  let cur: string | undefined = nodeId
  const seen = new Set<string>()
  while (cur !== undefined && weave.nodes[cur] && !seen.has(cur)) {
    seen.add(cur)
    path.push(cur)
    cur = weave.nodes[cur].parents[0]
  }
  return path.reverse()
}

export function myCursorNodeId(): string | null {
  return session.weave?.cursors[identity.name]?.node_id ?? null
}

// ---- in-flight generation placeholders (task #24) ---------------------------

export const GEN_PENDING_PREFIX = 'gen-pending:'

export function isGenPendingId(id: string): boolean {
  return id.startsWith(GEN_PENDING_PREFIX)
}

/** session.weave augmented with PHANTOM children for every in-flight
 * generation: one skeleton per expected completion (gen_started carries `n`),
 * attached under the generation's target node. Tree + canvas render these as
 * pending placeholders; they vanish when gen_finished lands (the real nodes
 * arrive via the node_added refetch; a failure already surfaces as a toast).
 * Call inside $derived — reads session.weave and session.activeGens. */
export function weaveWithPlaceholders(): Weave | null {
  const w = session.weave
  if (!w) return null
  if (session.activeGens.length === 0) return w
  const nodes = { ...w.nodes }
  for (const g of session.activeGens) {
    const parent = nodes[g.node_id]
    if (!parent) continue // target not in the snapshot (yet) — no phantom
    const ids: string[] = []
    const label = g.retry
      ? `retrying ${g.retry.attempt}/${g.retry.max}${g.retry.error ? ` — ${g.retry.error}` : ''}`
      : 'generating…'
    for (let i = 0; i < g.n; i++) {
      const id = `${GEN_PENDING_PREFIX}${g.gen_id}:${i}`
      ids.push(id)
      nodes[id] = {
        id,
        parents: [g.node_id],
        children: [],
        content: { type: 'snippet', text: label },
        creator: { type: 'model', label: g.generator ?? 'model' },
        created: g.started,
        modified: g.started,
        bookmarked: false,
        metadata: { gen_pending: true, requester: g.requester },
      }
    }
    nodes[g.node_id] = { ...parent, children: [...parent.children, ...ids] }
  }
  return { ...w, nodes }
}

// Dev/test introspection: lets playwright tests / debugging read client-side
// state (e.g. "where does THIS tab think my cursor is") without scraping the
// DOM. Dev builds only; assertions still go through REST per test conventions.
if (import.meta.env.DEV) {
  ;(window as unknown as Record<string, unknown>).__coloom = {
    session,
    identity,
    myCursorNodeId,
  }
}

// ---------------------------------------------------------------- weave sync

let activeWeaveId: string | null = null
let ws: WebSocket | null = null
let reconnectDelay = 500
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let refetchQueued = false
// Stale-snapshot guard: a snapshot fetch can resolve AFTER events that are
// NEWER than it were patched locally (e.g. delete → refetch starts → a
// cursor_moved lands and patches → stale snapshot assigns and silently
// undoes the patch, with no later event to correct it). Cure: remember
// events patched while any fetch is in flight and RE-APPLY them on top of
// the assigned snapshot (patches are idempotent), and let only the
// latest-initiated fetch assign (overlapping fetches resolve out of order).
let refetchSeq = 0
let refetchesInFlight = 0
let patchedDuringRefetch: WeaveEvent[] = []

// Pending optimistic cursor moves (incident 5): name -> the move we POSTed
// whose value a SNAPSHOT hasn't authoritatively carried yet. A snapshot
// fetched before our POST but assigned after it silently claws the cursor
// back — and the own-origin echo is absorbed (leg 1), so patch replay (leg 2)
// alone can't restore it (replaying an absorbed echo is still a no-op). Cure:
// re-assert pending moves over every assigned snapshot, until a snapshot
// whose fetch STARTED after the POST settled is assigned — that snapshot is
// authoritative for the move (it carries it, or something legitimately newer
// like a summon) and retires the pending entry either way. NOTE the echo must
// NOT retire the entry: it can arrive before the stale snapshot does.
const pendingCursorMoves = new Map<
  string,
  { nodeId: string; movedBy: string | null; settledAt: number | null }
>()

/** Stamp a pending move as POST-settled (confirmation is snapshot-time-based). */
function settlePendingCursor(name: string, nodeId: string) {
  const pending = pendingCursorMoves.get(name)
  if (pending && pending.nodeId === nodeId) pending.settledAt = performance.now()
}

function reassertPendingCursors(weave: Weave, fetchStartedAt: number) {
  for (const [name, pending] of [...pendingCursorMoves]) {
    if (pending.settledAt !== null && pending.settledAt < fetchStartedAt) {
      // fetched after the server accepted the move: authoritative — retire
      // the entry and let the snapshot stand (ours, or legitimately newer)
      pendingCursorMoves.delete(name)
      continue
    }
    if (weave.cursors[name]?.node_id === pending.nodeId) continue // already carried
    if (!weave.nodes[pending.nodeId]) continue // target gone; let truth stand
    weave.cursors[name] = {
      name,
      node_id: pending.nodeId,
      updated: new Date().toISOString(),
      moved_by: pending.movedBy,
    }
  }
}

async function refetchWeave() {
  if (activeWeaveId === null || refetchQueued) return
  refetchQueued = true
  await Promise.resolve() // coalesce bursts of events into one fetch
  refetchQueued = false
  const id = activeWeaveId
  if (id === null) return
  const mySeq = ++refetchSeq
  const fetchStartedAt = performance.now()
  refetchesInFlight++
  try {
    const weave = await api.getWeave(id)
    if (activeWeaveId === id && mySeq === refetchSeq) {
      session.weave = weave
      for (const ev of patchedDuringRefetch) patchEventLocally(ev)
      patchedDuringRefetch = []
      reassertPendingCursors(weave, fetchStartedAt)
    }
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) {
      toast('this weave was deleted')
      session.weave = null
      session.loadError = 'weave deleted'
    } else {
      toast(`weave refetch failed: ${e}`)
    }
  } finally {
    refetchesInFlight--
    if (refetchesInFlight === 0) patchedDuringRefetch = []
  }
}

function trackEvent(event: WeaveEvent) {
  session.events.push(event)
  if (session.events.length > EVENT_BUFFER_CAP) {
    session.events = session.events.slice(-EVENT_BUFFER_CAP)
  }
  if (event.type === 'gen_started') {
    session.activeGens.push({
      gen_id: event.payload.gen_id as string,
      requester: (event.payload.requester as string | null) ?? null,
      node_id: event.payload.node_id as string,
      generator: (event.payload.generator as string | null) ?? null,
      n: typeof event.payload.n === 'number' && event.payload.n > 0 ? event.payload.n : 1,
      started: event.created,
      retry: null,
    })
  } else if (event.type === 'gen_retrying') {
    // transient failure, the server is retrying: surface it on the placeholder
    const gen = session.activeGens.find((g) => g.gen_id === event.payload.gen_id)
    if (gen) {
      gen.retry = {
        attempt: typeof event.payload.attempt === 'number' ? event.payload.attempt : 0,
        max: typeof event.payload.max === 'number' ? event.payload.max : 0,
        error: typeof event.payload.error === 'string' ? event.payload.error : '',
      }
    }
  } else if (event.type === 'gen_finished') {
    session.activeGens = session.activeGens.filter(
      (g) => g.gen_id !== event.payload.gen_id,
    )
    if (event.payload.error) {
      const who = event.payload.requester ?? 'someone'
      toast(`generation by ${who} failed: ${event.payload.error}`)
    }
  }
}

/** This event is the server echo of a mutation THIS TAB sent (the per-tab
 * CLIENT_ID round-trips through the X-Coloom-Client header into the event
 * payload's `origin`). */
function isOwnEvent(event: WeaveEvent): boolean {
  return event.payload.origin === CLIENT_ID
}

// Patch cheap, frequent events (cursor moves, bookmarks) directly into the
// local weave instead of a full refetch — navigation must not feel like a
// network round trip. Returns true when the event was fully absorbed.
function patchEventLocally(event: WeaveEvent): boolean {
  const w = session.weave
  if (!w) return false
  if (event.type === 'cursor_moved') {
    const name = event.payload.name as string
    const nodeId = event.payload.node_id as string
    // own echo: every cursor move from this tab is either optimistic (already
    // applied locally) or bundled with a node mutation whose own event triggers
    // the refetch — patching the late echo would bounce fast navigation back
    // then forward (the flicker bug). Other tabs' moves always patch/refetch.
    if (isOwnEvent(event)) return true
    // node not in our snapshot yet (event raced ahead of a node_added
    // refetch) → fall back to the full refetch
    if (!w.nodes[nodeId]) return false
    w.cursors[name] = {
      name,
      node_id: nodeId,
      updated: event.created,
      moved_by: (event.payload.moved_by as string | null) ?? null,
    }
    return true
  }
  if (event.type === 'cursor_removed') {
    delete w.cursors[event.payload.name as string]
    return true
  }
  if (event.type === 'node_updated' && typeof event.payload.bookmarked === 'boolean') {
    const nodeId = event.payload.node_id as string
    const node = w.nodes[nodeId]
    if (!node) return false
    node.bookmarked = event.payload.bookmarked
    // keep the weave-level bookmark id list in sync (BookmarksPane reads it)
    if (event.payload.bookmarked && !w.bookmarks.includes(nodeId)) {
      w.bookmarks.push(nodeId)
    } else if (!event.payload.bookmarked) {
      w.bookmarks = w.bookmarks.filter((id) => id !== nodeId)
    }
    return true
  }
  return false
}

// ---- global (non-weave-scoped) template/generator change events ------------

const GLOBAL_EVENT_TYPES = new Set([
  'template_created',
  'template_updated',
  'template_deleted',
  'generator_created',
  'generator_updated',
  'generator_deleted',
])

/** Is the changed thing (a template or a generator) an ancestor of `gen`? */
function hasAncestor(gen: Generator, kind: 'template' | 'generator', id: string): boolean {
  return ancestorsOf(gen).some((a) =>
    a.kind === 'template' ? kind === 'template' && a.t.id === id : kind === 'generator' && a.g.id === id,
  )
}

/** Template/generator mutations broadcast to every client. Skip own echoes
 * (this tab already refreshed after its mutation); for remote changes, badge
 * any of MY chips whose ancestor changed, then refetch the lists. */
function handleGlobalEvent(event: WeaveEvent) {
  trackEvent(event) // activity feed: "clément edited template gpt4-base"
  if (isOwnEvent(event)) return
  const by = (event.payload.by as string | null) ?? null
  const id = event.payload.id as string
  const name = (event.payload.name as string) ?? '?'
  const kind = event.type.startsWith('template_') ? 'template' : 'generator'
  // Badge BEFORE refetching: a deleted template flattens its generators, so
  // the ancestry is only visible in the pre-refresh snapshot. Remote edits can
  // only move a chip's inherited fields — overrides are untouchable by others.
  if (by !== null && by !== profile.name) {
    for (const g of session.generators ?? []) {
      if (hasAncestor(g, kind, id)) {
        generatorUi.stale[g.id] = { by, kind, name }
      }
    }
  }
  void refreshGenerators()
}

function handleEvent(event: WeaveEvent) {
  if (GLOBAL_EVENT_TYPES.has(event.type)) {
    handleGlobalEvent(event)
    return
  }
  if (event.weave_id !== activeWeaveId) return
  trackEvent(event)
  if (event.type === 'node_added' || event.type === 'node_split') {
    const id = (event.payload.new_node_id ?? event.payload.node_id) as string
    session.changedNodeId = id
    session.changedAt = Date.now()
  }
  if (
    event.type === 'gen_started' ||
    event.type === 'gen_retrying' ||
    event.type === 'gen_finished'
  ) {
    return // no weave mutation (trackEvent maintains activeGens)
  }
  if (patchEventLocally(event)) {
    // remember patches an in-flight snapshot might be staler than
    if (refetchesInFlight > 0) patchedDuringRefetch.push(event)
    return
  }
  void refetchWeave()
}

function connectWs(weaveId: string) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  ws = new WebSocket(`${proto}://${location.host}/ws?weave_id=${weaveId}`)
  ws.onopen = () => {
    session.connection = 'live'
    reconnectDelay = 500
    void refetchWeave() // resync anything missed while disconnected
  }
  ws.onmessage = (msg) => handleEvent(JSON.parse(msg.data))
  ws.onclose = () => {
    ws = null
    if (activeWeaveId !== weaveId) return
    session.connection = 'reconnecting'
    reconnectTimer = setTimeout(() => connectWs(weaveId), reconnectDelay)
    reconnectDelay = Math.min(reconnectDelay * 2, 10000)
  }
}

export async function openWeave(weaveId: string) {
  await flushPendingEdits() // a held edit in the weave being left must land first
  closeWeave()
  activeWeaveId = weaveId
  session.loading = true
  session.loadError = null
  session.connection = 'connecting'
  try {
    session.weave = await api.getWeave(weaveId)
  } catch (e) {
    session.loadError = e instanceof ApiError ? e.message : `${e}`
    session.loading = false
    return
  }
  session.loading = false
  // backfill the activity feed (and any gens already in flight)
  try {
    const { events } = await api.getEvents(0, weaveId)
    session.events = []
    session.activeGens = []
    for (const e of events.slice(-EVENT_BUFFER_CAP)) trackEvent(e)
  } catch (e) {
    toast(`failed to backfill events: ${e}`)
  }
  connectWs(weaveId)
  // generators load on profile login; cover direct deep-links that race it
  if (session.generators === null) void refreshGenerators()
}

export function closeWeave() {
  activeWeaveId = null
  if (reconnectTimer !== null) clearTimeout(reconnectTimer)
  reconnectTimer = null
  ws?.close()
  ws = null
  pendingCursorMoves.clear()
  session.weave = null
  session.loadError = null
  session.hoveredNodeId = null
  session.changedNodeId = null
  session.events = []
  session.activeGens = []
  closeContextMenu()
  collapsed.clear()
}

// ---------------------------------------------------------------- actions

// ---- boundary-flush of free-form edits --------------------------------------
// Free-form doc edits stay LOCAL until a boundary action (Clément 2026-06-10):
// generate, any cursor move, node ops, undo, weave switch, doc blur, tab hide.
// TextPane registers its flusher here; every weave-mutating action below
// awaits flushPendingEdits() FIRST, so the held edit lands on the server (and
// settles) before the action runs — "send the node update, then run the next
// one". The flusher resolves immediately when there's nothing to flush, and
// no-ops re-entrantly while its own edit plan is executing (the plan's cursor
// moves use the NoFlush variant).

export interface EditFlusher {
  flush(): Promise<void>
  dirty(): boolean
}

let editFlusher: EditFlusher | null = null

export function registerEditFlusher(f: EditFlusher | null) {
  editFlusher = f
}

/** Apply any locally-held free-form edit and wait for it to settle. Safe to
 * call from anywhere; resolves immediately when there is nothing pending. */
export async function flushPendingEdits(): Promise<void> {
  if (editFlusher) await editFlusher.flush()
}

/** Is there typed text no edit plan has covered yet? (e.g. keyboard generate
 * fires even when the cursor only comes to exist via the flush) */
export function hasPendingEdits(): boolean {
  return editFlusher !== null && editFlusher.dirty()
}

/** Generate at a node: fans out ONE request per ACTIVE generator (children
 * from each model/temp appear side by side). With nothing active, falls back
 * to the FOCUSED generator so a fresh profile's weave button still works.
 * Only the first request may move the cursor. */
export async function generateAt(nodeId: string, opts: { moveCursor?: boolean } = {}) {
  await flushPendingEdits()
  const weave = session.weave
  if (!weave) return
  let gens = validActiveGenerators()
  if (gens.length === 0) {
    const focused = focusedGenerator()
    if (focused === null) {
      toast('no generator available — create one in the generators drawer')
      return
    }
    gens = [focused]
  }
  const requests = gens.map((g, i) => ({
    nodeId,
    cursor: identity.name,
    generatorId: g.id,
    moveCursor: (opts.moveCursor ?? false) && i === 0,
  }))
  session.inflight += requests.length
  await Promise.all(
    requests.map((req) =>
      withToast(() => api.generate(weave.id, req)).finally(() => {
        session.inflight--
      }),
    ),
  )
}

/** Apply a cursor move locally right away (optimistic — the server echo via
 * cursor_moved re-patches the same state; a failed POST refetches to undo).
 * Also registers the move as PENDING so a stale snapshot assigned mid-flight
 * can't claw it back (incident 5 — see reassertPendingCursors). */
function applyCursorLocally(name: string, nodeId: string, movedBy: string | null) {
  const w = session.weave
  if (!w || !w.nodes[nodeId]) return
  pendingCursorMoves.set(name, { nodeId, movedBy, settledAt: null })
  w.cursors[name] = {
    name,
    node_id: nodeId,
    updated: new Date().toISOString(),
    moved_by: movedBy,
  }
}

export async function moveMyCursor(nodeId: string) {
  await flushPendingEdits()
  await moveMyCursorNoFlush(nodeId)
}

/** moveMyCursor WITHOUT the edit flush — for the edit plan executor itself
 * (TextPane executePlan), whose cursor moves are part of the flush. */
export async function moveMyCursorNoFlush(nodeId: string) {
  const weave = session.weave
  if (!weave) return
  applyCursorLocally(identity.name, nodeId, null)
  await withToast(async () => {
    try {
      await api.setCursor(weave.id, identity.name, nodeId)
      settlePendingCursor(identity.name, nodeId)
    } catch (e) {
      pendingCursorMoves.delete(identity.name) // the move is dead, don't re-assert it
      void refetchWeave() // roll back the optimistic move
      throw e
    }
  })
}

export async function moveCursorOf(name: string, nodeId: string) {
  await flushPendingEdits()
  const weave = session.weave
  if (!weave) return
  applyCursorLocally(name, nodeId, identity.name)
  await withToast(async () => {
    try {
      await api.setCursor(weave.id, name, nodeId, identity.name)
      settlePendingCursor(name, nodeId)
    } catch (e) {
      pendingCursorMoves.delete(name)
      void refetchWeave()
      throw e
    }
  })
}

export async function appendText(text: string) {
  await flushPendingEdits()
  const weave = session.weave
  if (!weave) return
  await withToast(() =>
    api.addNode(weave.id, text, {
      parentId: myCursorNodeId(), // null = new root on an empty thread
      creator: { type: 'human', label: identity.name },
      moveCursor: identity.name,
    }),
  )
}

/** Create an empty/typed child or sibling as me, optionally moving my cursor. */
export async function createNode(
  text: string,
  opts: { parentId: string | null; moveCursor?: boolean },
) {
  await flushPendingEdits()
  const weave = session.weave
  if (!weave) return
  return withToast(() =>
    api.addNode(weave.id, text, {
      parentId: opts.parentId,
      creator: { type: 'human', label: identity.name },
      moveCursor: opts.moveCursor ? identity.name : undefined,
    }),
  )
}

/** The counterfactual gesture: branch from `nodeId` at `tokenIndex`, taking the
 * alternative `alt` instead of the actually-sampled token. Splits the node so
 * the prefix is shared, then adds a sibling Tokens branch starting with `alt`
 * (attribution + the counterfactual list preserved), and moves my cursor there. */
export async function branchAtToken(
  nodeId: string,
  tokenIndex: number,
  alt: TopLogprob,
) {
  await flushPendingEdits()
  const weave = session.weave
  if (!weave) return
  const node = weave.nodes[nodeId]
  if (!node || node.content.type !== 'tokens') return
  const original = node.content.tokens[tokenIndex]
  if (!original) return
  await withToast(async () => {
    let parentId: string | null
    if (tokenIndex > 0) {
      const { head } = await api.splitNode(weave.id, nodeId, tokenIndex)
      parentId = head.id
    } else {
      parentId = node.parents[0] ?? null
    }
    const content: Tokens = {
      type: 'tokens',
      tokens: [
        {
          text: alt.text,
          logprob: alt.logprob,
          token_id: alt.token_id ?? null,
          top_logprobs: original.top_logprobs,
        },
      ],
    }
    await api.addNode(weave.id, '', {
      content,
      parentId,
      creator: node.creator,
      moveCursor: identity.name,
      metadata: { counterfactual_of: nodeId, token_index: tokenIndex },
    })
  })
}

// ---- node deletion + undo ---------------------------------------------------
// Deletions ask NO confirmation: the server soft-deletes, an inverse op lands
// on the undo stack (undo.svelte.ts), and a toast offers an "undo" button.
// Ctrl+Z (rebindable 'undo' action) pops the stack for the open weave.

interface MovedCursor {
  name: string
  from: string
  to: string | null // null = the cursor was removed (its node was a root)
}

/** After a restore, put back the cursors the deletion had relocated — but
 * only those still parked where the delete left them (never yank a cursor
 * its owner has since moved deliberately). */
async function moveCursorsBack(weaveId: string, moved: MovedCursor[]) {
  for (const m of moved) {
    const cur = session.weave?.cursors[m.name]
    const undisturbed = cur === undefined ? m.to === null : cur.node_id === m.to
    if (!undisturbed) continue
    await api.setCursor(weaveId, m.name, m.from, identity.name)
  }
}

/** Register the inverse of a deletion: an undo-stack entry that calls the
 * restore endpoint (once per deletion-op root) and re-parks relocated
 * cursors, plus the post-delete toast with its "undo" button. `rootIds` =
 * the ids DELETE was called on; the server restores each root's whole
 * cascaded subtree. */
function registerDeletionUndo(
  weaveId: string,
  rootIds: string[],
  removed: number,
  movedCursors: MovedCursor[],
) {
  const what = removed === 1 ? 'node deleted' : `${removed} nodes deleted`
  const entry = pushUndo(what, weaveId, async () => {
    let restored = 0
    for (const id of rootIds) {
      try {
        restored += (await api.restoreNode(weaveId, id)).restored_node_ids.length
      } catch (e) {
        // 409 = this root is no longer deleted (a sibling restore already
        // brought it back as a deleted-ancestor op, or another client undid
        // it) — that's the desired end state, keep restoring the rest
        if (!(e instanceof ApiError && e.status === 409)) throw e
      }
    }
    await moveCursorsBack(weaveId, movedCursors)
    toast(restored === 1 ? 'node restored' : `${restored} nodes restored`, undefined, 'info')
  })
  toast(what, { label: 'undo', run: () => void withToast(() => undoEntry(entry)) }, 'info')
}

/** Ctrl+Z entry point. Returns true when the key was consumed (an undo ran,
 * or there was nothing to undo and we said so). */
export function undoLastAction(): boolean {
  const weave = session.weave
  if (!weave) return false
  if (!undoStack.items.some((e) => e.weaveId === weave.id)) {
    toast('nothing to undo', undefined, 'info')
    return true
  }
  void withToast(async () => {
    await flushPendingEdits()
    await undoLast(weave.id)
  })
  return true
}

/** Delete all children of a node (each cascades through its own subtree).
 * The whole batch becomes ONE undo entry. */
export async function deleteChildren(nodeId: string) {
  const weave = session.weave
  if (!weave) return
  const node = weave.nodes[nodeId]
  if (!node) return
  await deleteNodes([...node.children])
}

/** Bulk delete (multi-select bar, delete-children): one undo entry + one
 * toast for the batch. Cascades handle descendants server-side. */
export async function deleteNodes(ids: string[]) {
  await flushPendingEdits()
  const weave = session.weave
  if (!weave || ids.length === 0) return
  await withToast(async () => {
    let removed = 0
    const movedCursors: MovedCursor[] = []
    for (const id of ids) {
      const res = await api.removeNode(weave.id, id)
      removed += (res.deleted_node_ids ?? res.removed ?? [id]).length
      movedCursors.push(...(res.moved_cursors ?? []))
    }
    if (removed > 0) registerDeletionUndo(weave.id, ids, removed, movedCursors)
  })
}

export async function toggleBookmark(nodeId: string) {
  await flushPendingEdits()
  const weave = session.weave
  if (!weave) return
  const node = weave.nodes[nodeId]
  if (!node) return
  await withToast(() => api.setBookmark(weave.id, nodeId, !node.bookmarked))
}

/** Merge a node into its parent (server-side semantics — a NEW merged node,
 * absorbed originals soft-deleted). Undo follows the BINDING rules of
 * docs/events-api.md "Merge with parent": restore the deletion op + re-park
 * cursors; child migration is an edge edit that restore does NOT reverse, so
 * the merged node is deleted only when it took no children — deleting it
 * otherwise would cascade onto the migrated children, which must stay. */
export async function mergeWithParent(nodeId: string) {
  await flushPendingEdits()
  const weave = session.weave
  if (!weave) return
  await withToast(async () => {
    const res = await api.mergeWithParent(weave.id, nodeId)
    const tookChildren = res.merged_node.children.length > 0
    const entry = pushUndo('merge with parent', weave.id, async () => {
      try {
        await api.restoreNode(weave.id, res.deleted_node_ids[0])
      } catch (e) {
        // 409 = already restored (another client undid it) — desired end state
        if (!(e instanceof ApiError && e.status === 409)) throw e
      }
      await moveCursorsBack(weave.id, res.moved_cursors)
      if (!tookChildren) {
        // leaf merge: removing the merged copy completes a perfect undo
        // (direct removeNode — the undo of an undo is not itself undoable)
        await api.removeNode(weave.id, res.merged_node_id)
        toast('merge undone', undefined, 'info')
      } else {
        toast(
          'merge undone — merged copy kept (it carries the migrated children)',
          undefined,
          'info',
        )
      }
    })
    toast(
      'merged with parent',
      { label: 'undo', run: () => void withToast(() => undoEntry(entry)) },
      'info',
    )
  })
}

export async function deleteNode(nodeId: string) {
  await flushPendingEdits()
  const weave = session.weave
  if (!weave) return
  await withToast(async () => {
    const res = await api.removeNode(weave.id, nodeId)
    // ?? fallbacks: a pre-soft-delete server (deploy window) lacks the new
    // fields; deletion still works, undo will surface its 404 honestly
    registerDeletionUndo(
      weave.id,
      [nodeId],
      (res.deleted_node_ids ?? res.removed ?? [nodeId]).length,
      res.moved_cursors ?? [],
    )
  })
}
