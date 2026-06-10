<script lang="ts" module>
  // Key/value param rows à la Tapestry (render_config_map: Vec<(String, String)>):
  // the UI never asks for a JSON object — you add fields with preset/custom
  // names and fill their values. Values parse as JSON when they parse (numbers,
  // booleans, arrays, objects, quoted strings) and fall back to plain strings.
  export interface ParamRow {
    name: string
    value: string
  }

  export function rowsFromParams(params: Record<string, unknown>): ParamRow[] {
    return Object.entries(params).map(([name, v]) => ({
      name,
      value: typeof v === 'string' ? v : JSON.stringify(v),
    }))
  }

  /** Rows → params object. Throws on nameless values / duplicates so callers
   * surface the problem (never swallow). Fully empty rows are ignored. */
  export function paramsFromRows(rows: ParamRow[]): Record<string, unknown> {
    const out: Record<string, unknown> = {}
    for (const row of rows) {
      const name = row.name.trim()
      const raw = row.value.trim()
      if (!name && !raw) continue
      if (!name) throw new Error('a param row has a value but no name')
      if (name in out) throw new Error(`duplicate param name: ${name}`)
      if (!raw) throw new Error(`param "${name}" has no value`)
      try {
        out[name] = JSON.parse(raw)
      } catch {
        out[name] = raw // plain string (e.g. a stop sequence typed bare)
      }
    }
    return out
  }

  /** One-line display summary of a params object (chip titles, list rows). */
  export function paramsSummary(params: Record<string, unknown>): string {
    return Object.entries(params)
      .map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`)
      .join('  ')
  }

  // Common /v1/completions flags offered as suggestions; any custom name works.
  export const PRESET_PARAM_NAMES = [
    'temperature',
    'max_tokens',
    'top_p',
    'n',
    'stop',
    'seed',
    'logprobs',
    'presence_penalty',
    'frequency_penalty',
    'logit_bias',
    'best_of',
    'suffix',
    'echo',
  ]
</script>

<script lang="ts">
  let {
    rows = $bindable(),
    testid = 'params',
  }: { rows: ParamRow[]; testid?: string } = $props()

  const listId = $derived(`${testid}-preset-names`)

  function addRow() {
    rows.push({ name: '', value: '' })
  }

  function removeRow(i: number) {
    rows.splice(i, 1)
  }
</script>

<div class="params" data-testid={testid}>
  <datalist id={listId}>
    {#each PRESET_PARAM_NAMES as n (n)}
      <option value={n}></option>
    {/each}
  </datalist>
  {#each rows as row, i (i)}
    <div class="row">
      <input
        class="name"
        placeholder="param"
        list={listId}
        bind:value={row.name}
        data-testid={`${testid}-name-${i}`}
      />
      <input
        class="value"
        placeholder="value"
        bind:value={row.value}
        data-testid={`${testid}-value-${i}`}
      />
      <button
        class="rm"
        onclick={() => removeRow(i)}
        aria-label="remove param"
        data-testid={`${testid}-remove-${i}`}>×</button
      >
    </div>
  {/each}
  <button class="add" onclick={addRow} data-testid={`${testid}-add`}>+ param</button>
</div>

<style>
  .params {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
  }
  .row {
    display: flex;
    gap: 0.3rem;
    align-items: center;
  }
  .name {
    flex: 0 0 45%;
    font-family: var(--mono, monospace);
  }
  .value {
    flex: 1;
    min-width: 0;
    font-family: var(--mono, monospace);
  }
  .row input {
    font-size: var(--fs-ui);
    padding: 0.25rem 0.45rem;
    box-sizing: border-box;
  }
  .rm {
    flex-shrink: 0;
    padding: 0.1rem 0.45rem;
    font-size: var(--fs-ui);
    background: none;
    border: 1px solid transparent;
    color: var(--text-dim);
  }
  .rm:hover {
    color: var(--danger);
    border-color: var(--border);
  }
  .add {
    align-self: flex-start;
    font-size: var(--fs-small);
    padding: 0.15rem 0.5rem;
    color: var(--text-dim);
  }
  .add:hover {
    color: var(--text);
  }
</style>
