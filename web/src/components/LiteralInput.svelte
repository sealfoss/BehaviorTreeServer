<script lang="ts">
  // A Python literal typed as text ([1, 2.5, 'a'], {'k': 3}, {1, 2}); checked by the gateway
  // (ask parse_literal, design 9.3) before it is sent. The page never rebuilds the value.
  import { session } from '../lib/stores/session.svelte';

  let {
    value,
    type = null,
    disabled = false,
    onchange,
    testid,
    placeholder = '',
  }: {
    value: unknown;
    type?: string | null;
    disabled?: boolean;
    onchange: (wire: string) => void;
    testid?: string;
    placeholder?: string;
  } = $props();

  let error = $state('');
  const shown = $derived(value === null || value === undefined ? '' : String(value));

  async function commit(event: Event): Promise<void> {
    const input = event.currentTarget as HTMLInputElement;
    const text = input.value;
    if (text === shown) return;
    const reply = await session.ask('parse_literal', { type, text });
    if (!reply) {
      error = 'offline';
      return;
    }
    if (reply.t === 'no') {
      error = reply.reason ?? 'not a Python literal';
      return;
    }
    error = '';
    const wire = typeof reply.value === 'string' ? reply.value : text;
    input.value = wire;
    onchange(wire);
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
  class="literal"
  class:invalid={!!error}
  title={error || 'A Python literal, such as [1, 2.5, \'text\'] or {\'key\': 3}'}
  value={shown}
  {placeholder}
  {disabled}
  onchange={commit}
  onkeydown={key}
  data-testid={testid}
/>
