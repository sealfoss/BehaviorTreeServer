<script lang="ts">
  // Add Entry (the widget's dialog, user guide "The blackboard"): name, type and initial value.
  import { newId } from '../lib/ids';
  import { session } from '../lib/stores/session.svelte';
  import { CONTAINER_TYPES, ENTRY_TYPES } from '../lib/types';
  import { defaultWire, normaliseDoubleText, parseIntText, type Wire } from '../lib/wire';

  let { initialType, onclose }: { initialType: string; onclose: () => void } = $props();

  let name = $state('');
  // svelte-ignore state_referenced_locally
  let type = $state(initialType);
  let text = $state('');
  let checked = $state(false);
  let error = $state('');
  let busy = $state(false);

  const placeholder = $derived(String(defaultWire(type) ?? ''));

  /** The initial value in wire form, or an error text. */
  async function value(): Promise<Wire | { error: string }> {
    const typed = text.trim();
    try {
      if (type === 'Bool') return checked;
      if (type === 'String') return text;
      if (!typed) return defaultWire(type);
      if (type === 'Integer') return parseIntText(typed);
      if (type === 'Double') return normaliseDoubleText(typed);
    } catch (problem) {
      return { error: (problem as Error).message };
    }
    const reply = await session.ask('parse_literal', { type, text: typed });
    if (!reply) return { error: 'offline' };
    if (reply.t === 'no') return { error: reply.reason ?? 'not a Python literal' };
    return typeof reply.value === 'string' ? reply.value : typed;
  }

  async function ok(): Promise<void> {
    error = '';
    if (!name || name !== name.trim() || /[./]/.test(name)) {
      error = 'An entry name must not be empty, start or end with spaces, or contain "." or "/".';
      return;
    }
    if (session.blackboard.byName(name)) {
      error = `An entry named ${name} exists.`;
      return;
    }
    busy = true;
    const result = await value();
    busy = false;
    if (result !== null && typeof result === 'object') {
      error = result.error;
      return;
    }
    if (await session.bbOp({ op: 'bb.add', entry: { id: newId(), name, type, value: result, reset: false } })) onclose();
  }
</script>

<div class="modal-backdrop">
  <div class="dialog" role="dialog" aria-modal="true" aria-label="Add Entry" data-testid="dialog-add-entry">
    <h2>Add Entry</h2>
    <div class="form">
      <label for="add-entry-name">Name:</label>
      <!-- svelte-ignore a11y_autofocus -->
      <input id="add-entry-name" type="text" bind:value={name} autofocus data-testid="add-entry-name" onkeydown={(e) => e.key === 'Enter' && void ok()} />
      <label for="add-entry-type">Type:</label>
      <select id="add-entry-type" bind:value={type} data-testid="add-entry-type">
        {#each ENTRY_TYPES as item (item)}<option value={item}>{item}</option>{/each}
      </select>
      <label for="add-entry-value">Value:</label>
      {#if type === 'Bool'}
        <input id="add-entry-value" type="checkbox" bind:checked data-testid="add-entry-value" />
      {:else}
        <input
          id="add-entry-value"
          type="text"
          bind:value={text}
          placeholder={type === 'String' ? '' : placeholder}
          title={CONTAINER_TYPES.has(type) ? "A Python literal, such as [1, 2.5, 'text'] or {'key': 3}" : type === 'Double' ? 'A number; nan, inf and -inf too; "," is accepted as the decimal point' : ''}
          data-testid="add-entry-value"
          onkeydown={(e) => e.key === 'Enter' && void ok()}
        />
      {/if}
    </div>
    {#if error}<p class="error" data-testid="add-entry-error">{error}</p>{/if}
    <div class="dialog-buttons">
      <button type="button" class="primary" disabled={busy} onclick={ok} data-testid="btn-add-entry-ok">Add</button>
      <button type="button" onclick={onclose} data-testid="btn-add-entry-cancel">Cancel</button>
    </div>
  </div>
</div>
