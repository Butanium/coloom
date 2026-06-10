// Typed REST client for the coloom server (proxied in dev, same-origin in prod).

import type {
  Creator,
  Cursor,
  ModelSetup,
  NodeContent,
  PresetsResponse,
  SamplerSetup,
  SetupsResponse,
  ThreadResponse,
  Weave,
  WeaveEvent,
  WeaveInfo,
  WeaveNode,
} from './types'

export class ApiError extends Error {
  status: number
  constructor(status: number, detail: string) {
    super(detail)
    this.status = status
  }
}

// Per-tab client id, sent on every request: the server stamps it into event
// payloads as `origin`, so this tab can tell its own mutations' echoes from
// remote changes (state.svelte.ts isOwnEvent).
export const CLIENT_ID = crypto.randomUUID()

async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method,
    headers: {
      'X-Coloom-Client': CLIENT_ID,
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) {
    let detail = `${method} ${url} failed: ${res.status}`
    try {
      const data = await res.json()
      if (data.detail) detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)
    } catch {
      // non-JSON error body; keep the status-line message
    }
    throw new ApiError(res.status, detail)
  }
  if (res.status === 204) return undefined as T
  return res.json()
}

export const api = {
  listWeaves: () => request<WeaveInfo[]>('GET', '/weaves'),
  createWeave: (title: string, description = '', metadata?: Record<string, unknown>) =>
    request<WeaveInfo>('POST', '/weaves', {
      title,
      description,
      ...(metadata ? { metadata } : {}),
    }),
  getWeave: (id: string) => request<Weave>('GET', `/weaves/${id}`),
  deleteWeave: (id: string) => request<void>('DELETE', `/weaves/${id}`),

  updateWeave: (
    weaveId: string,
    fields: { title?: string; description?: string; metadata?: Record<string, unknown> },
  ) => request<WeaveInfo>('PATCH', `/weaves/${weaveId}`, fields),

  addNode: (
    weaveId: string,
    text: string,
    opts: {
      parentId?: string | null
      creator?: Creator
      moveCursor?: string
      content?: NodeContent // typed content (e.g. Tokens for counterfactual branches)
      metadata?: Record<string, unknown>
    } = {},
  ) =>
    request<WeaveNode>('POST', `/weaves/${weaveId}/nodes`, {
      text,
      ...(opts.content ? { content: opts.content } : {}),
      parent_id: opts.parentId ?? null,
      ...(opts.creator ? { creator: opts.creator } : {}),
      move_cursor: opts.moveCursor ?? null,
      ...(opts.metadata ? { metadata: opts.metadata } : {}),
    }),
  removeNode: (weaveId: string, nodeId: string) =>
    request<{ removed: number }>('DELETE', `/weaves/${weaveId}/nodes/${nodeId}`),
  splitNode: (weaveId: string, nodeId: string, at: number) =>
    request<{ head: WeaveNode; tail: WeaveNode }>(
      'POST',
      `/weaves/${weaveId}/nodes/${nodeId}/split`,
      { at },
    ),
  /** Replace a node's content wholesale (free-form editing / keystroke
   * coalescing). `metadata`, when given, replaces the node's metadata. */
  updateNode: (
    weaveId: string,
    nodeId: string,
    content: NodeContent,
    metadata?: Record<string, unknown>,
  ) =>
    request<WeaveNode>('PATCH', `/weaves/${weaveId}/nodes/${nodeId}`, {
      content,
      ...(metadata !== undefined ? { metadata } : {}),
    }),
  setBookmark: (weaveId: string, nodeId: string, bookmarked: boolean) =>
    request<{ node_id: string; bookmarked: boolean }>(
      'PUT',
      `/weaves/${weaveId}/nodes/${nodeId}/bookmark`,
      { bookmarked },
    ),

  listCursors: (weaveId: string) =>
    request<Record<string, Cursor>>('GET', `/weaves/${weaveId}/cursors`),
  setCursor: (weaveId: string, name: string, nodeId: string, movedBy?: string) =>
    request<Cursor>('PUT', `/weaves/${weaveId}/cursors/${encodeURIComponent(name)}`, {
      node_id: nodeId,
      moved_by: movedBy ?? name,
    }),
  getThread: (weaveId: string, cursorName: string) =>
    request<ThreadResponse>(
      'GET',
      `/weaves/${weaveId}/cursors/${encodeURIComponent(cursorName)}/thread`,
    ),

  generate: (
    weaveId: string,
    opts: {
      nodeId?: string
      cursor?: string
      preset?: string | null
      samplerId?: string | null // wins over preset (docs/setups-api.md)
      params?: Record<string, unknown>
      moveCursor?: boolean
    },
  ) =>
    request<WeaveNode[]>('POST', `/weaves/${weaveId}/gen`, {
      node_id: opts.nodeId ?? null,
      cursor: opts.cursor ?? null,
      preset: opts.preset ?? null,
      sampler_id: opts.samplerId ?? null,
      params: opts.params ?? {},
      move_cursor: opts.moveCursor ?? false,
    }),

  listPresets: () => request<PresetsResponse>('GET', '/presets'),

  // profiles: server-stored per-person client settings (roam across browsers)
  listProfiles: () => request<{ name: string; updated: string }[]>('GET', '/profiles'),
  getProfile: (name: string) =>
    request<{ name: string; settings: Record<string, unknown>; active?: boolean }>(
      'GET',
      `/profiles/${encodeURIComponent(name)}`,
    ),
  putProfile: (name: string, settings: Record<string, unknown>) =>
    request<{ name: string; settings: Record<string, unknown> }>(
      'PUT',
      `/profiles/${encodeURIComponent(name)}`,
      { settings },
    ),
  deleteProfile: (name: string) =>
    request<void>('DELETE', `/profiles/${encodeURIComponent(name)}`),

  // setups CRUD (docs/setups-api.md)
  listSetups: () => request<SetupsResponse>('GET', '/setups'),
  createModelSetup: (fields: Omit<ModelSetup, 'id'>) =>
    request<ModelSetup>('POST', '/setups/models', fields),
  updateModelSetup: (id: string, fields: Partial<Omit<ModelSetup, 'id'>>) =>
    request<ModelSetup>('PATCH', `/setups/models/${id}`, fields),
  deleteModelSetup: (id: string) => request<void>('DELETE', `/setups/models/${id}`),
  createSamplerSetup: (fields: Omit<SamplerSetup, 'id'>) =>
    request<SamplerSetup>('POST', '/setups/samplers', fields),
  updateSamplerSetup: (id: string, fields: Partial<Omit<SamplerSetup, 'id'>>) =>
    request<SamplerSetup>('PATCH', `/setups/samplers/${id}`, fields),
  deleteSamplerSetup: (id: string) =>
    request<void>('DELETE', `/setups/samplers/${id}`),
  getEvents: (since: number, weaveId?: string) =>
    request<{ events: WeaveEvent[]; cursor: number }>(
      'GET',
      `/events?since=${since}${weaveId ? `&weave_id=${weaveId}` : ''}`,
    ),
}
