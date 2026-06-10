// Profiles: the app opens on a profile-select page; ALL per-person client
// state (identity, ui prefs, active generators, keybindings…) lives in the
// profile's settings blob, stored SERVER-side (PUT /profiles/{name}) so a
// profile roams across browsers/windows. localStorage only caches the last
// profile name for auto-login.
//
// Import direction (no cycles): state.svelte.ts and keybindings.svelte.ts
// import {setSetting,getSetting} from here; this module imports nothing from
// them. On login, App.svelte calls each store's applyProfileSettings().

import { api, ApiError } from './api'

const PROFILE_CACHE_KEY = 'coloom.profile'

export const profile = $state<{
  name: string | null
  settings: Record<string, unknown>
  pending: boolean // auto-login in flight (App shows a loading state)
  saveError: string | null
}>({
  name: null,
  settings: {},
  pending: localStorage.getItem(PROFILE_CACHE_KEY) !== null,
  saveError: null,
})

// stores register an applier; loginProfile runs them after settings load
type Applier = (settings: Record<string, unknown>) => void
const appliers: Applier[] = []
export function onProfileLogin(applier: Applier) {
  appliers.push(applier)
}

export function getSetting<T>(key: string, fallback: T): T {
  const v = profile.settings[key]
  return v === undefined ? fallback : (v as T)
}

let saveTimer: ReturnType<typeof setTimeout> | null = null

export function setSetting(key: string, value: unknown) {
  profile.settings[key] = value
  if (profile.name === null) return // pre-login (e.g. module init) — nothing to sync
  if (saveTimer !== null) clearTimeout(saveTimer)
  saveTimer = setTimeout(() => {
    saveTimer = null
    const name = profile.name
    if (name === null) return
    api
      .putProfile(name, $state.snapshot(profile.settings))
      .then(() => {
        profile.saveError = null
      })
      .catch((e) => {
        // surfaced in the header sync dot — never silently lose settings
        profile.saveError = `${e}`
      })
  }, 800)
}

/** Flush a pending debounced save immediately (logout / beforeunload). */
export async function flushProfileSave() {
  if (saveTimer === null || profile.name === null) return
  clearTimeout(saveTimer)
  saveTimer = null
  try {
    await api.putProfile(profile.name, $state.snapshot(profile.settings))
    profile.saveError = null
  } catch (e) {
    profile.saveError = `${e}`
  }
}

export async function loginProfile(name: string): Promise<void> {
  const trimmed = name.trim()
  if (!trimmed) throw new Error('profile name required')
  profile.pending = true
  try {
    let settings: Record<string, unknown>
    try {
      settings = (await api.getProfile(trimmed)).settings
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 404)) throw e
      // first login = create the profile
      settings = (await api.putProfile(trimmed, {})).settings
    }
    profile.name = trimmed
    profile.settings = settings
    localStorage.setItem(PROFILE_CACHE_KEY, trimmed)
    for (const apply of appliers) apply(settings)
  } finally {
    profile.pending = false
  }
}

export async function logoutProfile() {
  await flushProfileSave()
  profile.name = null
  profile.settings = {}
  localStorage.removeItem(PROFILE_CACHE_KEY)
  location.hash = '' // back to the picker route under the login gate
}

/** Auto-login from the cached profile name; called once from App. */
export async function autoLogin(): Promise<void> {
  const cached = localStorage.getItem(PROFILE_CACHE_KEY)
  if (cached === null) {
    profile.pending = false
    return
  }
  try {
    await loginProfile(cached)
  } catch {
    // cached name unusable (server reset etc) → show the login page
    localStorage.removeItem(PROFILE_CACHE_KEY)
    profile.pending = false
  }
}
