<script lang="ts">
  import Editor from './lib/Editor.svelte'
  import Login from './lib/Login.svelte'
  import Picker from './lib/Picker.svelte'
  import Toasts from './lib/Toasts.svelte'
  import { autoLogin, profile } from './lib/profile.svelte'
  import { setIdentity } from './lib/state.svelte'

  // hash routing: '' → picker, '#/w/<id>' → editor — all behind the profile gate
  let hash = $state(location.hash)
  $effect(() => {
    const onHash = () => (hash = location.hash)
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  })

  const weaveId = $derived(hash.match(/^#\/w\/([0-9a-f]+)$/)?.[1] ?? null)

  // auto-login from the cached profile name, once
  $effect(() => {
    void autoLogin()
  })

  // the identity (cursor name, creator attribution) IS the profile name
  $effect(() => {
    if (profile.name) setIdentity(profile.name)
  })
</script>

{#if profile.pending}
  <div class="center-msg"><p>loading profile…</p></div>
{:else if profile.name === null}
  <Login />
{:else if weaveId}
  <Editor {weaveId} />
{:else}
  <Picker />
{/if}
<Toasts />

<style>
  .center-msg {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-dim);
  }
</style>
