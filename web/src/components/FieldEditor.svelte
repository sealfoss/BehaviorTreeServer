<script lang="ts">
  // The editor of one declared field (design 6.4): int, float, str, bool, choice, or a
  // Python literal for any other value.
  import { session } from '../lib/stores/session.svelte';
  import type { CatalogField, WireNode } from '../lib/types';
  import { parseIntText } from '../lib/wire';
  import DoubleInput from './DoubleInput.svelte';
  import EntrySelect from './EntrySelect.svelte';
  import LiteralInput from './LiteralInput.svelte';

  let { node, field, docKey, readOnly }: { node: WireNode; field: CatalogField; docKey: string; readOnly: boolean } = $props();

  const value = $derived(node.fields?.[field.name]);
  const testid = $derived(`field-${node.id}-${field.name}`);
  let error = $state('');

  function set(wire: unknown): void {
    void session.sendOp(docKey, { op: 'node.set_field', id: node.id, field: field.name, value: wire });
  }

  function commitInt(event: Event): void {
    const input = event.currentTarget as HTMLInputElement;
    try {
      const number = parseIntText(input.value);
      error = '';
      if (number !== value) set(number);
    } catch (problem) {
      error = (problem as Error).message;
    }
  }

  function enterBlurs(event: KeyboardEvent): void {
    if (event.key === 'Enter') (event.currentTarget as HTMLInputElement).blur();
  }
</script>

{#if field.kind === 'int'}
  <input type="number" step="1" class:invalid={!!error} title={error || field.doc} value={value ?? 0} disabled={readOnly} onchange={commitInt} onkeydown={enterBlurs} data-testid={testid} />
{:else if field.kind === 'float'}
  <DoubleInput {value} disabled={readOnly} onchange={set} {testid} />
{:else if field.kind === 'str'}
  <input
    type="text"
    title={field.doc}
    value={value ?? ''}
    disabled={readOnly}
    onchange={(e) => set((e.currentTarget as HTMLInputElement).value)}
    onkeydown={enterBlurs}
    data-testid={testid}
  />
{:else if field.kind === 'bool'}
  <input type="checkbox" title={field.doc} checked={value === true} disabled={readOnly} onchange={(e) => set((e.currentTarget as HTMLInputElement).checked)} data-testid={testid} />
{:else if field.kind === 'choice'}
  {@const options = Array.isArray(value) ? (value as string[]) : []}
  <select
    title={field.doc}
    value={String(node.field_selections?.[field.name] ?? 0)}
    disabled={readOnly}
    onchange={(e) => void session.sendOp(docKey, { op: 'node.set_selection', id: node.id, field: field.name, index: Number((e.currentTarget as HTMLSelectElement).value) })}
    data-testid={testid}
  >
    {#each options as option, index (index)}
      <option value={String(index)}>{option}</option>
    {/each}
  </select>
{:else if field.kind === 'entry'}
  <EntrySelect value={typeof value === 'string' && value ? value : null} literal="(none)" disabled={readOnly} onchange={(name) => set(name || null)} {testid} title={field.doc} />
{:else}
  <LiteralInput {value} disabled={readOnly} onchange={set} {testid} placeholder="None" />
{/if}
