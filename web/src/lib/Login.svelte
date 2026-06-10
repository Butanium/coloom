<script lang="ts">
  // Profile-select gate: the app opens here. A profile carries all per-person
  // client state (identity, ui prefs, generators, keybindings) server-side.
  import { api } from './api'
  import { loginProfile } from './profile.svelte'
  import { toast, withToast } from './state.svelte'

  let profiles = $state<{ name: string; updated: string }[]>([])
  let loaded = $state(false)
  let newName = $state('')

  async function refresh() {
    await withToast(async () => {
      profiles = await api.listProfiles()
      loaded = true
    })
  }

  $effect(() => {
    void refresh()
  })

  async function pick(name: string) {
    await withToast(() => loginProfile(name))
  }

  async function create() {
    const name = newName.trim()
    if (!name) {
      toast('profile name required')
      return
    }
    await withToast(() => loginProfile(name))
  }

  async function remove(name: string, e: Event) {
    e.stopPropagation()
    if (
      !confirm(
        `remove profile "${name}" from this list? its settings are kept — ` +
          `logging in with the same name brings everything back`,
      )
    )
      return
    await withToast(async () => {
      await api.deleteProfile(name)
      await refresh()
    })
  }
</script>

<div class="login">
  <div class="box">
    <h1>coloom</h1>
    <p class="sub">who's weaving?</p>
    {#if loaded}
      <ul class="profiles" data-testid="profile-list">
        {#each profiles as p (p.name)}
          <li>
            <button class="profile" onclick={() => pick(p.name)} data-testid={`profile-${p.name}`}>
              <span class="dot" style:background={`hsl(${[...p.name].reduce((h, c) => (h * 31 + c.charCodeAt(0)) | 0, 0) % 360}, 65%, 60%)`}
              ></span>
              {p.name}
            </button>
            <button
              class="rm"
              title="delete profile"
              onclick={(e) => remove(p.name, e)}
              data-testid={`profile-delete-${p.name}`}>✕</button
            >
          </li>
        {:else}
          <li class="empty">no profiles yet — create one below</li>
        {/each}
      </ul>
    {:else}
      <p class="empty">loading profiles…</p>
    {/if}
    <form
      class="new"
      onsubmit={(e) => {
        e.preventDefault()
        void create()
      }}
    >
      <input
        placeholder="new profile name"
        bind:value={newName}
        data-testid="new-profile-name"
      />
      <button class="primary" type="submit" data-testid="new-profile-create">enter</button>
    </form>
  </div>
</div>

<style>
  .login {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .box {
    width: min(26rem, 92vw);
    display: flex;
    flex-direction: column;
    gap: 0.8rem;
  }
  h1 {
    margin: 0;
    font-size: 1.6rem;
  }
  .sub {
    margin: 0;
    color: var(--text-dim);
    font-size: var(--fs-ui);
  }
  .profiles {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }
  .profiles li {
    display: flex;
    gap: 0.3rem;
    align-items: center;
  }
  .profile {
    flex: 1;
    display: flex;
    align-items: center;
    gap: 0.6rem;
    text-align: left;
    font-size: var(--fs-ui);
    padding: 0.5rem 0.8rem;
  }
  .profile:hover {
    border-color: var(--accent);
  }
  .dot {
    width: 0.7rem;
    height: 0.7rem;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .rm {
    color: var(--text-dim);
    background: none;
    border-color: transparent;
    padding: 0.4rem 0.5rem;
  }
  .rm:hover {
    color: var(--danger);
    border-color: var(--border);
  }
  .empty {
    color: var(--text-dim);
    font-size: var(--fs-small);
    text-align: center;
    padding: 0.6rem;
  }
  .new {
    display: flex;
    gap: 0.4rem;
  }
  .new input {
    flex: 1;
    font-size: var(--fs-ui);
  }
</style>
