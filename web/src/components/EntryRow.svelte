<script lang="ts">
  // One row of the Blackboard tab. A fed entry's value belongs to its topic (read-only), and it
  // is greyed out while it has no data or is stale (design 7.4).
  import { session } from '../lib/stores/session.svelte';
  import type { Entry } from '../lib/types';
  import { containerItems } from '../lib/wire';
  import ValueEditor from './ValueEditor.svelte';

  let {
    entry,
    checked,
    first,
    last,
    ontoggle,
    onmove,
  }: { entry: Entry; checked: boolean; first: boolean; last: boolean; ontoggle: (on: boolean) => void; onmove: (offset: number) => void } = $props();

  const fed = $derived(!!entry.source);
  const stale = $derived(entry.freshness === 'nodata' || entry.freshness === 'stale');
  const online = $derived(session.executors.online);
  const readOnly = $derived(fed || entry.type === 'Object' || !online);
  const items = $derived(['List', 'Dictionary', 'Set'].includes(entry.type) && typeof entry.value === 'string' && !entry.truncated ? containerItems(entry.value) : []);
  const ageText = $derived.by(() => {
    if (entry.freshness === 'nodata') return 'no data';
    if (entry.age === null || entry.age === undefined) return '';
    const seconds = entry.age < 10 ? entry.age.toFixed(1) : String(Math.round(entry.age));
    return `${seconds} s${entry.freshness === 'stale' ? ' (stale)' : ''}`;
  });

  function rename(event: Event): void {
    const input = event.currentTarget as HTMLInputElement;
    const name = input.value;
    if (name === entry.name) return;
    if (!name || name !== name.trim() || /[./]/.test(name)) {
      session.ui.notify('warning', 'An entry name must not be empty, start or end with spaces, or contain "." or "/".');
      input.value = entry.name;
      return;
    }
    void session.bbOp({ op: 'bb.rename', id: entry.id, name });
  }

  function openDialog(kind: 'source' | 'entry'): void {
    session.ui.dialogEntry = entry.id;
    session.ui.dialog = kind;
  }
</script>

<div class="bb-row" class:stale class:fed data-testid="bb-row-{entry.name}" data-entry-id={entry.id}>
  <input type="checkbox" {checked} onchange={(e) => ontoggle((e.currentTarget as HTMLInputElement).checked)} title="Select" data-testid="bb-select-{entry.name}" />
  <span class="bb-col-type">{entry.type}</span>
  <input
    class="bb-col-name"
    type="text"
    value={entry.name}
    disabled={!online}
    onchange={rename}
    onkeydown={(e) => e.key === 'Enter' && (e.currentTarget as HTMLInputElement).blur()}
    data-testid="bb-name-{entry.name}"
  />
  <span class="bb-col-value">
    {#if entry.truncated}
      <button type="button" class="link show-all" onclick={() => openDialog('entry')} title="The value is long; show all of it" data-testid="bb-value-{entry.name}">
        <span class="show-all-text">{String(entry.value).slice(0, 60)}</span><span class="show-all-more">… (show all)</span>
      </button>
    {:else}
      <ValueEditor
        type={entry.type}
        value={entry.value}
        disabled={readOnly}
        onchange={(value) => void session.bbOp({ op: 'bb.set_value', id: entry.id, value })}
        testid="bb-value-{entry.name}"
      />
      {#if items.length}
        <select class="bb-items" title="The items of the value" data-testid="bb-items-{entry.name}">
          {#each items as item, index (index)}<option>{item}</option>{/each}
        </select>
      {/if}
    {/if}
  </span>
  <input
    class="bb-col-reset"
    type="checkbox"
    checked={entry.reset}
    disabled={fed || !online}
    onchange={(e) => void session.bbOp({ op: 'bb.set_reset', id: entry.id, reset: (e.currentTarget as HTMLInputElement).checked })}
    title="Resets: at the end of every execution loop, this entry goes back to the value it had when the loop started"
    data-testid="bb-reset-{entry.name}"
  />
  <span class="bb-col-source">
    <button type="button" onclick={() => openDialog('source')} disabled={entry.type === 'Object' || !online} data-testid="bb-source-{entry.name}">Source…</button>
    {#if entry.problem}<span class="bb-problem" title={entry.problem} data-testid="bb-problem-{entry.name}">⚠ {entry.problem}</span>{/if}
    {#if fed}<span class="bb-topic" title="{entry.source?.type} {entry.source?.field ?? ''}">{entry.source?.topic}</span>{/if}
    {#if fed && ageText}<span class="bb-age" data-testid="bb-age-{entry.name}">{ageText}</span>{/if}
  </span>
  <span class="bb-col-order">
    <button type="button" class="arrow" disabled={first || !online} onclick={() => onmove(-1)} title="Move this entry up in the list" data-testid="bb-up-{entry.name}">▲</button>
    <button type="button" class="arrow" disabled={last || !online} onclick={() => onmove(1)} title="Move this entry down in the list" data-testid="bb-down-{entry.name}">▼</button>
  </span>
</div>
