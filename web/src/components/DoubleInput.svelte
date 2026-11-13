<script lang="ts">
  // A Double's editor (design 11.4, P-10): a text box that accepts numbers, nan, inf and -inf,
  // and ',' as the decimal point. The browser's number box accepts none of the three words.
  import { normaliseDoubleText } from '../lib/wire';

  let {
    value,
    disabled = false,
    onchange,
    testid,
  }: { value: unknown; disabled?: boolean; onchange: (wire: string) => void; testid?: string } = $props();

  let error = $state('');
  const shown = $derived(value === null || value === undefined ? '' : String(value));

  function commit(event: Event): void {
    const input = event.currentTarget as HTMLInputElement;
    try {
      const wire = normaliseDoubleText(input.value);
      error = '';
      input.value = wire;
      if (wire !== shown) onchange(wire);
    } catch (problem) {
      error = (problem as Error).message;
    }
  }

  function key(event: KeyboardEvent): void {
    const input = event.currentTarget as HTMLInputElement;
    if (event.key === 'Enter') input.blur();
    if (event.key === 'Escape') {
      input.value = shown;
      error = '';
    }
  }
</script>

<input
  type="text"
  inputmode="decimal"
  class="double"
  class:invalid={!!error}
  title={error || 'A number (nan, inf and -inf too; "," is accepted as the decimal point)'}
  value={shown}
  {disabled}
  onchange={commit}
  onkeydown={key}
  data-testid={testid}
/>
