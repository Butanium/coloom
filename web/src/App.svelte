<script lang="ts">
  interface WeaveSummary {
    id: string
    title: string
    description: string
    created: string
  }

  async function fetchWeaves(): Promise<WeaveSummary[]> {
    const res = await fetch('/weaves')
    if (!res.ok) throw new Error(`GET /weaves failed: ${res.status}`)
    return res.json()
  }
</script>

<main>
  <h1>coloom</h1>
  <p>a loom for human + AI co-weaving</p>
  {#await fetchWeaves()}
    <p>loading weaves…</p>
  {:then weaves}
    {#if weaves.length === 0}
      <p>no weaves yet</p>
    {:else}
      <ul>
        {#each weaves as w (w.id)}
          <li><code>{w.id}</code> {w.title}</li>
        {/each}
      </ul>
    {/if}
  {:catch err}
    <p class="error">server unreachable: {err.message}</p>
  {/await}
</main>

<style>
  main {
    max-width: 48rem;
    margin: 2rem auto;
    padding: 0 1rem;
  }
  .error {
    color: #c0392b;
  }
</style>
