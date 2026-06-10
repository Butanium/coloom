// Typed REST client for the coloom server (proxied in dev, same-origin in prod).

import type {
  Creator,
  Cursor,
  Generator,
  NodeContent,
  ParentRef,
  ProbeResult,
  Template,
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

// The logged-in profile name, sent on every request as X-Coloom-Profile so the
// server can stamp the actor (`by`) into template/generator events. Set by
// profile.svelte.ts on login/logout (setter avoids an import cycle: profile
// imports api). Percent-encoded — header values must be ISO-8859-1 and
// profile names may not be ("clément"); the server unquotes.
let profileHeader: string | null = null

export function setApiProfile(name: string | null) {
  profileHeader = name === null ? null : encodeURIComponent(name)
}

async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method,
    headers: {
      'X-Coloom-Client': CLIENT_ID,
      ...(profileHeader !== null ? { 'X-Coloom-Profile': profileHeader } : {}),
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
  /** Soft-delete: deleted_node_ids = the whole cascaded subtree (restorable).
   * moved_cursors = cursors that were stranded in the subtree and relocated
   * to the parent (`to: null` = removed, the victim was a root); `from` lets
   * undo put them back. */
  removeNode: (weaveId: string, nodeId: string) =>
    request<{
      deleted_node_ids?: string[]
      moved_cursors?: { name: string; from: string; to: string | null }[]
      removed?: string[] // legacy alias of deleted_node_ids (old servers)
    }>('DELETE', `/weaves/${weaveId}/nodes/${nodeId}`),
  /** Undo a (soft) deletion: restores the node's deletion op (its whole
   * cascaded subtree) plus any deleted-ancestor ops needed for reachability.
   * 409 if the node isn't deleted. */
  restoreNode: (weaveId: string, nodeId: string) =>
    request<{ restored_node_ids: string[] }>(
      'POST',
      `/weaves/${weaveId}/nodes/${nodeId}/restore`,
    ),
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
      generatorId: string // docs/generators-api.md — replaces sampler_id/preset
      params?: Record<string, unknown> // per-request overrides (CLI parity)
      moveCursor?: boolean
    },
  ) =>
    request<WeaveNode[]>('POST', `/weaves/${weaveId}/gen`, {
      node_id: opts.nodeId ?? null,
      cursor: opts.cursor ?? null,
      generator_id: opts.generatorId,
      params: opts.params ?? {},
      move_cursor: opts.moveCursor ?? false,
    }),

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

  // templates + generators CRUD (docs/generators-api.md)
  listTemplates: () => request<Template[]>('GET', '/templates'),
  createTemplate: (fields: Omit<Template, 'id' | 'builtin'>) =>
    request<Template>('POST', '/templates', fields),
  /** Promote: materialize a generator's RESOLVED fields into a new template. */
  promoteGenerator: (generatorId: string) =>
    request<Template>('POST', '/templates', { from_generator: generatorId }),
  updateTemplate: (
    id: string,
    fields: Partial<Omit<Template, 'id' | 'builtin'>>, // params = whole replace
  ) => request<Template>('PATCH', `/templates/${id}`, fields),
  deleteTemplate: (id: string) => request<void>('DELETE', `/templates/${id}`),

  listGenerators: (profile: string) =>
    request<Generator[]>('GET', `/generators?profile=${encodeURIComponent(profile)}`),
  createGenerator: (fields: {
    profile: string
    name: string
    parent?: ParentRef | null
    base_url?: string | null
    model?: string | null
    api_key?: string | null
    api_key_env?: string | null
    params?: Record<string, unknown>
  }) => request<Generator>('POST', '/generators', fields),
  /** mode 'inherit' → parent=from, empty overrides; 'duplicate' → literal copy
   * (template source: parent=null, all fields copied). */
  createGeneratorFrom: (
    from: ParentRef,
    mode: 'inherit' | 'duplicate',
    profile: string,
    name?: string,
  ) =>
    request<Generator>('POST', '/generators', {
      from,
      mode,
      profile,
      ...(name !== undefined ? { name } : {}),
    }),
  /** Partial update; explicit null clears a field back to inherited; a params
   * key set to null removes that single override (params merge per-key). */
  updateGenerator: (
    id: string,
    fields: Partial<{
      name: string
      parent: ParentRef | null
      base_url: string | null
      model: string | null
      api_key: string | null
      api_key_env: string | null
      params: Record<string, unknown | null>
    }>,
  ) => request<Generator>('PATCH', `/generators/${id}`, fields),
  deleteGenerator: (id: string) => request<void>('DELETE', `/generators/${id}`),

  /** Reachability + model suggestions for an endpoint (server-side probe of
   * {base_url}/models — the browser can't and must not call third-party
   * endpoints directly). Two modes: literal {base_url, api_key?|api_key_env?},
   * or by id {template_id|generator_id, base_url?} where the server resolves
   * the stored/inherited credentials (an explicit base_url wins over the
   * stored one — for probing while the URL field is being edited). */
  probeEndpoint: (
    body:
      | { base_url: string; api_key?: string; api_key_env?: string }
      | { template_id: string; base_url?: string }
      | { generator_id: string; base_url?: string },
  ) => request<ProbeResult>('POST', '/probe-endpoint', body),
  getEvents: (since: number, weaveId?: string) =>
    request<{ events: WeaveEvent[]; cursor: number }>(
      'GET',
      `/events?since=${since}${weaveId ? `&weave_id=${weaveId}` : ''}`,
    ),
}
