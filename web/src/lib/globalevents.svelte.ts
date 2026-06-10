// Global activity: events across ALL weaves, for the activity pane's
// "all weaves" scope. Separate from session.events (the per-weave buffer in
// state.svelte.ts) — this opens its OWN WebSocket without a weave_id filter
// (the EventHub treats that as "everything") plus a GET /events backfill,
// and only runs while a pane actually wants it (refcounted start/stop).

import { api } from './api'
import type { WeaveEvent } from './types'

const CAP = 500

export const globalFeed = $state<{
  events: WeaveEvent[]
  titles: Record<string, string> // weave_id → title, labels foreign-weave entries
  status: 'idle' | 'connecting' | 'live' | 'error'
  error: string | null
}>({ events: [], titles: {}, status: 'idle', error: null })

let ws: WebSocket | null = null
let users = 0
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let reconnectDelay = 500

function push(e: WeaveEvent) {
  // dedupe against the backfill racing the live socket
  if (globalFeed.events.some((x) => x.seq === e.seq)) return
  globalFeed.events.push(e)
  if (globalFeed.events.length > CAP) {
    globalFeed.events = globalFeed.events.slice(-CAP)
  }
}

async function backfill() {
  try {
    // the unfiltered log paginates (limit 1000) from the START — page through
    // with the since cursor to reach the tail, keeping only the last CAP
    let tail: WeaveEvent[] = []
    let since = 0
    for (;;) {
      const { events, cursor } = await api.getEvents(since)
      tail = [...tail, ...events].slice(-CAP)
      if (events.length === 0 || cursor === since) break
      since = cursor
    }
    const weaves = await api.listWeaves()
    globalFeed.titles = Object.fromEntries(weaves.map((w) => [w.id, w.title]))
    // merge with anything the live socket already delivered, newest tail wins
    const seen = new Set(tail.map((e) => e.seq))
    const extra = globalFeed.events.filter((e) => !seen.has(e.seq))
    globalFeed.events = [...tail, ...extra].sort((a, b) => a.seq - b.seq).slice(-CAP)
    globalFeed.error = null
  } catch (e) {
    globalFeed.status = 'error'
    globalFeed.error = `${e}` // surfaced in the pane — never swallowed
  }
}

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  globalFeed.status = 'connecting'
  ws = new WebSocket(`${proto}://${location.host}/ws`) // no weave_id = all events
  ws.onopen = () => {
    globalFeed.status = 'live'
    reconnectDelay = 500
    void backfill() // resync anything missed while down
  }
  ws.onmessage = (msg) => {
    const event = JSON.parse(msg.data) as WeaveEvent
    push(event)
    // a brand-new weave won't be in the title map yet
    if (event.type === 'weave_created') {
      globalFeed.titles[event.weave_id] = (event.payload.title as string) ?? ''
    }
  }
  ws.onclose = () => {
    ws = null
    if (users === 0) return
    globalFeed.status = 'connecting'
    reconnectTimer = setTimeout(connect, reconnectDelay)
    reconnectDelay = Math.min(reconnectDelay * 2, 10000)
  }
}

/** A pane wants the global feed (call from an $effect; pair with stop). */
export function startGlobalFeed() {
  users++
  if (users === 1 && ws === null) connect()
}

export function stopGlobalFeed() {
  users = Math.max(0, users - 1)
  if (users === 0) {
    if (reconnectTimer !== null) clearTimeout(reconnectTimer)
    reconnectTimer = null
    ws?.close()
    ws = null
    globalFeed.status = 'idle'
  }
}
