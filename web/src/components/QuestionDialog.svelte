<script lang="ts">
  // A question to the user (contract 5.4: unsaved, replace, choose_file; prompts for names).
  import { session } from '../lib/stores/session.svelte';

  const ui = session.ui;
  let input = $state('');

  $effect(() => {
    input = ui.question?.input?.value ?? '';
  });

  function answer(button: string): void {
    ui.answer({ button, input });
  }
</script>

{#if ui.question}
  {@const q = ui.question}
  <div class="modal-backdrop">
    <div class="dialog" role="dialog" aria-modal="true" aria-label={q.title} data-testid={q.testid ?? 'dialog-question'}>
      <h2>{q.title}</h2>
      <p class="dialog-text">{q.text}</p>
      {#if q.input}
        <label class="dialog-field">
          <span>{q.input.label}</span>
          <!-- svelte-ignore a11y_autofocus -->
          <input
            type="text"
            bind:value={input}
            autofocus
            data-testid="dialog-input"
            onkeydown={(e) => {
              if (e.key === 'Enter') answer(q.buttons.find((b) => b.primary)?.value ?? q.buttons[0].value);
            }}
          />
        </label>
      {/if}
      {#if q.choices?.length}
        <ul class="dialog-choices" data-testid="dialog-choices">
          {#each q.choices as choice (choice)}
            <li>
              <button type="button" class:chosen={choice === input} onclick={() => (input = choice)} ondblclick={() => answer(q.buttons.find((b) => b.primary)?.value ?? 'ok')}>{choice}</button>
            </li>
          {/each}
        </ul>
      {/if}
      <div class="dialog-buttons">
        {#each q.buttons as button (button.value)}
          <button type="button" class:primary={button.primary} onclick={() => answer(button.value)} data-testid="dialog-btn-{button.value}">{button.label}</button>
        {/each}
      </div>
    </div>
  </div>
{/if}
