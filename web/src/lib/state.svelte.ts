// Shared editor state (audit §9 taxonomy):
//  - shared+persistent lives in `weave` (server-canonical snapshot, refetched on events)
//  - shared+temporary: hovered node, changed node, in-flight generations
//  - per-client: identity, collapse set, selected preset, toasts
// The WS feed drives a debounced full-weave refetch — simple and always consistent;
// incremental patching is a later optimization.

import { SvelteSet } from 'svelte/reactivity'
import { api, ApiError, CLIENT_ID } from './api'
import { getSetting, onProfileLogin, setSetting } from './profile.svelte'
import type {
  ActiveGen,
  PresetsResponse,
  SetupsResponse,
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

export interface Toast {
  id: number
  message: string
}

export const toasts = $state<{ items: Toast[] }>({ items: [] })
let nextToastId = 1

export function toast(message: string) {
  const id = nextToastId++
  toasts.items.push({ id, message })
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
  presets: PresetsResponse | null
  selectedPreset: string | null
  paramOverrides: Record<string, unknown>
  // two-layer inference setups (docs/setups-api.md); refreshed via refreshSetups()
  setups: SetupsResponse | null
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
  presets: null,
  selectedPreset: null,
  paramOverrides: {},
  setups: null,
})

// ------------------------------------------------------------ active generators
// A "generator" is anything you can weave with: a sampler setup or a YAML
// preset. SEVERAL can be active at once — generate fans out one request per
// active generator. A separate hidden set controls which chips are displayed.
// Per-client, localStorage-persisted (will move into profile settings).

export interface GeneratorRef {
  kind: 'sampler' | 'preset'
  id: string // sampler setup id, or preset name
}

export function generatorKey(ref: GeneratorRef): string {
  return `${ref.kind}:${ref.id}`
}

const GENERATORS_KEY = 'coloom.activeGenerators'
const HIDDEN_GENERATORS_KEY = 'coloom.hiddenGenerators'
const LEGACY_SAMPLERS_KEY = 'coloom.activeSamplers'

function loadGeneratorList(key: string): GeneratorRef[] {
  try {
    const v = JSON.parse(localStorage.getItem(key) ?? '[]')
    if (!Array.isArray(v)) return []
    return v.filter(
      (x) =>
        x &&
        (x.kind === 'sampler' || x.kind === 'preset') &&
        typeof x.id === 'string',
    )
  } catch {
    return []
  }
}

function loadActiveGenerators(): GeneratorRef[] {
  const refs = loadGeneratorList(GENERATORS_KEY)
  // migrate the pre-unification sampler-id list
  try {
    const legacy = JSON.parse(localStorage.getItem(LEGACY_SAMPLERS_KEY) ?? '[]')
    if (Array.isArray(legacy)) {
      for (const id of legacy) {
        if (typeof id === 'string' && !refs.some((r) => r.kind === 'sampler' && r.id === id)) {
          refs.push({ kind: 'sampler', id })
        }
      }
    }
    localStorage.removeItem(LEGACY_SAMPLERS_KEY)
  } catch {
    // ignore unparseable legacy state
  }
  return refs
}

export const activeGenerators = $state<{ list: GeneratorRef[] }>({
  list: loadActiveGenerators(),
})
export const hiddenGenerators = $state<{ keys: string[] }>({
  keys: loadGeneratorList(HIDDEN_GENERATORS_KEY).map(generatorKey),
})

function persistGenerators() {
  localStorage.setItem(GENERATORS_KEY, JSON.stringify(activeGenerators.list))
  localStorage.setItem(
    HIDDEN_GENERATORS_KEY,
    JSON.stringify(
      hiddenGenerators.keys.map((k) => {
        const [kind, ...rest] = k.split(':')
        return { kind, id: rest.join(':') }
      }),
    ),
  )
  setSetting('activeGenerators', $state.snapshot(activeGenerators.list))
  setSetting('hiddenGenerators', [...hiddenGenerators.keys])
}

export function isGeneratorActive(ref: GeneratorRef): boolean {
  return activeGenerators.list.some((r) => generatorKey(r) === generatorKey(ref))
}

export function toggleActiveGenerator(ref: GeneratorRef) {
  activeGenerators.list = isGeneratorActive(ref)
    ? activeGenerators.list.filter((r) => generatorKey(r) !== generatorKey(ref))
    : [...activeGenerators.list, ref]
  persistGenerators()
}

export function isGeneratorHidden(ref: GeneratorRef): boolean {
  return hiddenGenerators.keys.includes(generatorKey(ref))
}

export function toggleHiddenGenerator(ref: GeneratorRef) {
  const key = generatorKey(ref)
  hiddenGenerators.keys = hiddenGenerators.keys.includes(key)
    ? hiddenGenerators.keys.filter((k) => k !== key)
    : [...hiddenGenerators.keys, key]
  persistGenerators()
}

// On profile login: profile settings override the local fallbacks; a profile
// that never stored a key gets seeded from the current local state (so the
// pre-profiles localStorage world migrates in on first login).
onProfileLogin((settings) => {
  const u = settings.ui as Partial<typeof ui> | undefined
  if (u) Object.assign(ui, u)
  else persistUi()
  const act = settings.activeGenerators as GeneratorRef[] | undefined
  if (act) activeGenerators.list = act
  const hid = settings.hiddenGenerators as string[] | undefined
  if (hid) hiddenGenerators.keys = hid
  if (!act || !hid) persistGenerators()
})

/** Display list for generator chips: samplers first, then presets, hidden
 * filtered out. Shared by GenControls and the digit-key toggles. */
export function visibleGenerators(): { ref: GeneratorRef; label: string }[] {
  const out: { ref: GeneratorRef; label: string }[] = []
  for (const s of session.setups?.samplers ?? []) {
    const ref: GeneratorRef = { kind: 'sampler', id: s.id }
    if (!isGeneratorHidden(ref)) out.push({ ref, label: s.name })
  }
  for (const name of Object.keys(session.presets?.presets ?? {})) {
    const ref: GeneratorRef = { kind: 'preset', id: name }
    if (!isGeneratorHidden(ref)) out.push({ ref, label: name })
  }
  return out
}

/** Active generators that actually exist right now (stale refs filtered). */
export function validActiveGenerators(): GeneratorRef[] {
  return activeGenerators.list.filter((g) =>
    g.kind === 'sampler'
      ? (session.setups?.samplers ?? []).some((s) => s.id === g.id)
      : session.presets?.presets[g.id] !== undefined,
  )
}

export async function refreshSetups() {
  try {
    session.setups = await api.listSetups()
    // drop active sampler refs that no longer exist
    const known = new Set(session.setups.samplers.map((s) => s.id))
    const kept = activeGenerators.list.filter(
      (r) => r.kind !== 'sampler' || known.has(r.id),
    )
    if (kept.length !== activeGenerators.list.length) {
      activeGenerators.list = kept
      persistGenerators()
    }
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) {
      // server without the setups API — behave as "no setups defined"
      session.setups = { models: [], samplers: [] }
      return
    }
    toast(`failed to load setups: ${e}`)
  }
}

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

// ---------------------------------------------------------------- weave sync

let activeWeaveId: string | null = null
let ws: WebSocket | null = null
let reconnectDelay = 500
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let refetchQueued = false

async function refetchWeave() {
  if (activeWeaveId === null || refetchQueued) return
  refetchQueued = true
  await Promise.resolve() // coalesce bursts of events into one fetch
  refetchQueued = false
  const id = activeWeaveId
  if (id === null) return
  try {
    const weave = await api.getWeave(id)
    if (activeWeaveId === id) session.weave = weave
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) {
      toast('this weave was deleted')
      session.weave = null
      session.loadError = 'weave deleted'
    } else {
      toast(`weave refetch failed: ${e}`)
    }
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
      preset: (event.payload.preset as string | null) ?? null,
      started: event.created,
    })
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

function handleEvent(event: WeaveEvent) {
  if (event.weave_id !== activeWeaveId) return
  trackEvent(event)
  if (event.type === 'node_added' || event.type === 'node_split') {
    const id = (event.payload.new_node_id ?? event.payload.node_id) as string
    session.changedNodeId = id
    session.changedAt = Date.now()
  }
  if (event.type === 'gen_started' || event.type === 'gen_finished') return // no weave mutation
  if (patchEventLocally(event)) return
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
  if (session.presets === null) {
    try {
      session.presets = await api.listPresets()
      session.selectedPreset =
        session.presets.default_preset ?? Object.keys(session.presets.presets)[0] ?? null
    } catch (e) {
      toast(`failed to load presets: ${e}`)
    }
  }
  if (session.setups === null) void refreshSetups()
}

export function closeWeave() {
  activeWeaveId = null
  if (reconnectTimer !== null) clearTimeout(reconnectTimer)
  reconnectTimer = null
  ws?.close()
  ws = null
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

/** Generate at a node. With active generators (samplers and/or presets), fans
 * out ONE request per generator (children from each model/temp appear side by
 * side); otherwise falls back to the selected preset. Only the first request
 * may move the cursor. */
export async function generateAt(nodeId: string, opts: { moveCursor?: boolean } = {}) {
  const weave = session.weave
  if (!weave) return
  const base = {
    nodeId,
    cursor: identity.name,
    params: session.paramOverrides,
  }
  const gens = validActiveGenerators()
  const requests =
    gens.length > 0
      ? gens.map((g, i) => ({
          ...base,
          ...(g.kind === 'sampler' ? { samplerId: g.id } : { preset: g.id }),
          moveCursor: (opts.moveCursor ?? false) && i === 0,
        }))
      : [{ ...base, preset: session.selectedPreset, moveCursor: opts.moveCursor ?? false }]
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
 * cursor_moved re-patches the same state; a failed POST refetches to undo). */
function applyCursorLocally(name: string, nodeId: string, movedBy: string | null) {
  const w = session.weave
  if (!w || !w.nodes[nodeId]) return
  w.cursors[name] = {
    name,
    node_id: nodeId,
    updated: new Date().toISOString(),
    moved_by: movedBy,
  }
}

export async function moveMyCursor(nodeId: string) {
  const weave = session.weave
  if (!weave) return
  applyCursorLocally(identity.name, nodeId, null)
  await withToast(async () => {
    try {
      await api.setCursor(weave.id, identity.name, nodeId)
    } catch (e) {
      void refetchWeave() // roll back the optimistic move
      throw e
    }
  })
}

export async function moveCursorOf(name: string, nodeId: string) {
  const weave = session.weave
  if (!weave) return
  applyCursorLocally(name, nodeId, identity.name)
  await withToast(async () => {
    try {
      await api.setCursor(weave.id, name, nodeId, identity.name)
    } catch (e) {
      void refetchWeave()
      throw e
    }
  })
}

export async function appendText(text: string) {
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

/** Delete all children of a node (each cascades through its own subtree). */
export async function deleteChildren(nodeId: string) {
  const weave = session.weave
  if (!weave) return
  const node = weave.nodes[nodeId]
  if (!node) return
  await withToast(async () => {
    for (const child of node.children) {
      await api.removeNode(weave.id, child)
    }
  })
}

export async function toggleBookmark(nodeId: string) {
  const weave = session.weave
  if (!weave) return
  const node = weave.nodes[nodeId]
  if (!node) return
  await withToast(() => api.setBookmark(weave.id, nodeId, !node.bookmarked))
}

export async function deleteNode(nodeId: string) {
  const weave = session.weave
  if (!weave) return
  await withToast(() => api.removeNode(weave.id, nodeId))
}
