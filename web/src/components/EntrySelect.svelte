<script lang="ts">
  // A blackboard-aware combo box: lists the entries (optionally of one type); a chosen entry
  // that does not exist stays chosen and is shown in red as "(missing)", as in the widget.
  import { session } from '../lib/stores/session.svelte';

  let {
    value,
    type = null,
    literal = null,
    disabled = false,
    onchange,
    testid,
    title = '',
  }: {
    value: string | null;
    type?: string | null;
    literal?: string | null;
    disabled?: boolean;
    onchange: (name: string | null) => void;
    testid?: string;
    title?: string;
  } = $props();

  const names = $derived(session.blackboard.entries.filter((entry) => !type || entry.type === type).map((entry) => entry.name));
  const missing = $derived(value !== null && value !== '' && !session.blackboard.byName(value));
  const LITERAL = '\u0000literal';
</script>

<select
  class:missing
  {title}
  value={value === null || value === '' ? (literal !== null ? LITERAL : '') : value}
  {disabled}
  onchange={(e) => {
    const chosen = (e.currentTarget as HTMLSelectElement).value;
    onchange(chosen === LITERAL || chosen === '' ? (literal !== null ? null : '') : chosen);
  }}
  data-testid={testid}
>
  {#if literal !== null}
    <option value={LITERAL}>{literal}</option>
  {:else}
    <option value="" disabled>(choose an entry)</option>
  {/if}
  {#each names as name (name)}
    <option value={name}>{name}</option>
  {/each}
  {#if missing && value}
    <option value={value} class="missing">{value} (missing)</option>
  {/if}
</select>
