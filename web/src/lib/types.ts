// TS mirrors of the pydantic weave model (src/coloom/models.py).

export interface TopLogprob {
  text: string
  logprob: number
  token_id?: number | null
}

export interface Token {
  text: string
  logprob?: number | null
  token_id?: number | null
  entropy?: number | null
  top_logprobs: TopLogprob[]
  // logprob carried over from a different prefix context (preserved tail of an
  // edited thread): still shades, but views must mark it as not exact
  inexact?: boolean
}

export interface Snippet {
  type: 'snippet'
  text: string
}

export interface Tokens {
  type: 'tokens'
  tokens: Token[]
}

export type NodeContent = Snippet | Tokens

export interface HumanCreator {
  type: 'human'
  label: string
  color?: string | null
  id?: string | null
}

export interface ModelCreator {
  type: 'model'
  label: string
  color?: string | null
  id?: string | null
  seed?: number | null
  raw_request?: Record<string, unknown> | null
  raw_response?: Record<string, unknown> | null
}

export interface UnknownCreator {
  type: 'unknown'
}

export type Creator = HumanCreator | ModelCreator | UnknownCreator

export interface WeaveNode {
  id: string
  parents: string[]
  children: string[]
  content: NodeContent
  creator: Creator
  created: string
  modified: string
  bookmarked: boolean
  metadata: Record<string, unknown>
}

export interface Cursor {
  name: string
  node_id: string
  updated: string
  moved_by?: string | null
}

export interface WeaveInfo {
  id: string
  title: string
  description: string
  created: string
  metadata: Record<string, unknown>
}

export interface Weave {
  id: string
  title: string
  description: string
  created: string
  nodes: Record<string, WeaveNode>
  roots: string[]
  cursors: Record<string, Cursor>
  bookmarks: string[]
  metadata: Record<string, unknown>
}

export interface ThreadResponse {
  path: string[]
  content: string
  nodes: WeaveNode[]
}

// ------------------------------------------------------- templates + generators
// Two nouns (docs/generators-api.md): a TEMPLATE is a complete generator
// definition on the shelf, server-global; a GENERATOR is the concrete,
// activatable thing, per-profile, inheriting from a parent (template or
// another generator of the same profile) with nullable-field overrides.

export interface Template {
  id: string
  name: string
  builtin: boolean // imported from coloom.yaml presets; read-only via the API
  base_url: string
  model: string
  api_key?: string | null // redacted to "***" by the server when set
  api_key_env?: string | null
  params: Record<string, unknown>
}

export interface ParentRef {
  kind: 'template' | 'generator'
  id: string
}

/** The fields a generator inherits/overrides, fully resolved leaf→root. */
export interface ResolvedGenerator {
  base_url: string | null
  model: string | null
  api_key?: string | null // redacted to "***" when set anywhere in the chain
  api_key_env?: string | null
  params: Record<string, unknown> // merged root → leaf, leaf wins
}

export interface Generator {
  id: string
  profile: string
  name: string
  parent: ParentRef | null
  // null = inherited from the parent chain
  base_url?: string | null
  model?: string | null
  api_key?: string | null
  api_key_env?: string | null
  params: Record<string, unknown> // own overridden keys only
  resolved: ResolvedGenerator
  usable: boolean // false until base_url + model resolve non-empty
}

export interface ProbeResult {
  ok: boolean
  error: string | null
  models: string[]
}

export interface WeaveEvent {
  seq: number
  weave_id: string
  type:
    | 'weave_created'
    | 'weave_updated'
    | 'weave_deleted'
    | 'node_added'
    | 'node_removed'
    | 'node_restored' // undo of a (soft) deletion — task #3 restore endpoint
    | 'node_updated'
    | 'node_split'
    | 'cursor_moved'
    | 'cursor_removed'
    | 'gen_started'
    | 'gen_finished'
    // global (not weave-scoped) — payload {id, name, profile?, by, origin}
    | 'template_created'
    | 'template_updated'
    | 'template_deleted'
    | 'generator_created'
    | 'generator_updated'
    | 'generator_deleted'
  payload: Record<string, unknown>
  created: string
}

/** An in-flight generation, tracked from gen_started/gen_finished events. */
export interface ActiveGen {
  gen_id: string
  requester: string | null
  node_id: string
  generator: string | null // generator name
  started: string
}

export function nodeText(node: WeaveNode): string {
  return node.content.type === 'snippet'
    ? node.content.text
    : node.content.tokens.map((t) => t.text).join('')
}

const GEN_PARAM_KEYS = ['model', 'temperature', 'max_tokens', 'n', 'logprobs', 'seed', 'top_p']

/** The sampling config a model node was generated with (from the stored raw
 * request) — render this the same way in every view's tooltip/inspector. */
export function genParams(node: WeaveNode): Record<string, unknown> | null {
  if (node.creator.type !== 'model' || !node.creator.raw_request) return null
  const req = node.creator.raw_request
  const out: Record<string, unknown> = {}
  for (const k of GEN_PARAM_KEYS) {
    if (req[k] !== undefined && req[k] !== null) out[k] = req[k]
  }
  if (node.metadata.finish_reason) out.finish = node.metadata.finish_reason
  return out
}
