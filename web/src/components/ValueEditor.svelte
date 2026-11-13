<script lang="ts">
  // An editor for a value of a blackboard entry type (the widget's editors per type).
  import { parseIntText } from '../lib/wire';
  import DoubleInput from './DoubleInput.svelte';
  import LiteralInput from './LiteralInput.svelte';

  let {
    type,
    value,
    disabled = false,
    onchange,
    testid,
  }: { type: string; value: unknown; disabled?: boolean; onchange: (wire: unknown) => void; testid?: string } = $props();

  let error = $state('');

  function commitInt(event: Event): void {
    const input = event.currentTarget as HTMLInputElement;
    try {
      const number = parseIntText(input.value);
      error = '';
      if (number !== value) onchange(number);
    } catch (problem) {
      error = (problem as Error).message;
    }
  }

  function commitText(event: Event): void {
    const text = (event.currentTarget as HTMLInputElement).value;
    if (text !== value) onchange(text);
  }

  function enterBlurs(event: KeyboardEvent): void {
    if (event.key === 'Enter') (event.currentTarget as HTMLInputElement).blur();
  }
</script>

{#if type === 'Integer'}
  <input type="number" step="1" class="int" class:invalid={!!error} title={error} value={value ?? 0} {disabled} onchange={commitInt} onkeydown={enterBlurs} data-testid={testid} />
{:else if type === 'Double'}
  <DoubleInput {value} {disabled} onchange={(wire) => onchange(wire)} {testid} />
{:else if type === 'String'}
  <input type="text" value={value ?? ''} {disabled} onchange={commitText} onkeydown={enterBlurs} data-testid={testid} />
{:else if type === 'Bool'}
  <input type="checkbox" checked={value === true} {disabled} onchange={(e) => onchange((e.currentTarget as HTMLInputElement).checked)} data-testid={testid} />
{:else if type === 'List' || type === 'Dictionary' || type === 'Set'}
  <LiteralInput {value} {type} {disabled} onchange={(wire) => onchange(wire)} {testid} />
{:else}
  <input type="text" readonly value={value === null || value === undefined ? '' : String(value)} title="Read-only" data-testid={testid} />
{/if}
