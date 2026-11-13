<script lang="ts">
  // Notices from the gateway and from refused requests.
  import { session } from '../lib/stores/session.svelte';
</script>

<div class="notices" aria-live="polite">
  {#each session.ui.notices as notice (notice.id)}
    <div class="notice notice-{notice.level}" data-testid="notice">
      <span>{notice.text}</span>
      <button type="button" class="notice-close" aria-label="Dismiss" onclick={() => session.ui.dismiss(notice.id)}>×</button>
    </div>
  {/each}
</div>
