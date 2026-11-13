<script lang="ts">
  // The Blackboard tab (design 11.5): the widget's table (type, name, value, Resets, up and
  // down arrows, Add Entry, Remove Entries, Select All), a Source… button per row and, for fed
  // entries, the topic and the age.
  import { session } from '../lib/stores/session.svelte';
  import { ENTRY_TYPES } from '../lib/types';
  import AddEntryDialog from './AddEntryDialog.svelte';
  import EntryRow from './EntryRow.svelte';

  const blackboard = session.blackboard;
  let checked = $state<Set<string>>(new Set());
  let newType = $state<string>('Integer');
  let adding = $state(false);
  const online = $derived(session.executors.online);

  const allChecked = $derived(blackboard.entries.length > 0 && blackboard.entries.every((entry) => checked.has(entry.id)));

  function toggleAll(event: Event): void {
    checked = (event.currentTarget as HTMLInputElement).checked ? new Set(blackboard.entries.map((entry) => entry.id)) : new Set();
  }

  function toggle(id: string, on: boolean): void {
    const next = new Set(checked);
    if (on) next.add(id);
    else next.delete(id);
    checked = next;
  }

  function removeEntries(): void {
    const ids = blackboard.entries.filter((entry) => checked.has(entry.id)).map((entry) => entry.id);
    if (ids.length) void session.bbOp({ op: 'bb.remove', ids });
    checked = new Set();
  }

  function move(index: number, offset: number): void {
    const ids = blackboard.entries.map((entry) => entry.id);
    const target = index + offset;
    if (target < 0 || target >= ids.length) return;
    [ids[index], ids[target]] = [ids[target], ids[index]];
    void session.bbOp({ op: 'bb.move', order: ids });
  }
</script>

<section class="blackboard" data-testid="blackboard">
  <div class="bb-select-all">
    <label class="check" title="Select every entry"><input type="checkbox" checked={allChecked} onchange={toggleAll} data-testid="bb-select-all" /> Select All</label>
  </div>
  <div class="bb-header">
    <span></span>
    <span class="bb-col-type">Type</span>
    <span class="bb-col-name">Entry Name</span>
    <span class="bb-col-value">Entry Value</span>
    <span class="bb-col-reset" title="Entries with Resets checked go back to their loop-start value at the end of every execution loop">Resets</span>
    <span class="bb-col-source">Source</span>
    <span class="bb-col-order" title="Move an entry up or down in the list with its arrows; the order is saved with the blackboard">Order</span>
  </div>
  <div class="bb-rows">
    {#key session.bbVersion}
      {#each blackboard.entries as entry, index (entry.id)}
        <EntryRow
          {entry}
          checked={checked.has(entry.id)}
          first={index === 0}
          last={index === blackboard.entries.length - 1}
          ontoggle={(on) => toggle(entry.id, on)}
          onmove={(offset) => move(index, offset)}
        />
      {:else}
        <p class="muted bb-empty">The blackboard is empty.</p>
      {/each}
    {/key}
  </div>
  <div class="bb-controls">
    <select bind:value={newType} data-testid="bb-new-type">
      {#each ENTRY_TYPES as type (type)}<option value={type}>{type}</option>{/each}
    </select>
    <button type="button" onclick={() => (adding = true)} disabled={!online} data-testid="bb-add">Add Entry</button>
    <button type="button" onclick={removeEntries} disabled={!checked.size || !online} data-testid="bb-remove">Remove Entries</button>
  </div>
</section>
{#if adding}<AddEntryDialog initialType={newType} onclose={() => (adding = false)} />{/if}
