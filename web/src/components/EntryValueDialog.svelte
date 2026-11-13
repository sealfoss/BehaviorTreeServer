<script lang="ts">
  // The whole value of an entry sent shortened (longer than 64 KB, contract 5.1): fetched with
  // ask entry_value; containers can be edited as a Python literal here.
  import { session } from '../lib/stores/session.svelte';

  let { onclose }: { onclose: () => void } = $props();

  const entry = session.blackboard.byId(session.ui.dialogEntry ?? '');
  let text = $state('Loading…');
  let loaded = $state(false);
  const editable = !!entry && ['List', 'Dictionary', 'Set', 'String'].includes(entry.type) && !entry.source;

  $effect(() => {
    if (!entry) return;
    void session.ask('entry_value', { id: entry.id }).then((reply) => {
      if (reply?.t === 'ok') {
        text = String(reply.value ?? '');
        loaded = true;
      } else {
        text = reply?.reason ?? 'The value could not be fetched.';
      }
    });
  });

  async function save(): Promise<void> {
    if (!entry) return;
    if (await session.bbOp({ op: 'bb.set_value', id: entry.id, value: text })) onclose();
  }
</script>

<div class="modal-backdrop">
  <div class="dialog wide-dialog" role="dialog" aria-modal="true" aria-label="Entry value" data-testid="dialog-entry-value">
    <h2>{entry?.name ?? ''} ({entry?.type ?? ''})</h2>
    <textarea class="value-text" bind:value={text} readonly={!editable || !loaded} data-testid="entry-value-text"></textarea>
    <div class="dialog-buttons">
      {#if editable}<button type="button" class="primary" disabled={!loaded} onclick={save} data-testid="btn-entry-value-save">Save</button>{/if}
      <button type="button" onclick={onclose} data-testid="btn-entry-value-close">Close</button>
    </div>
  </div>
</div>
