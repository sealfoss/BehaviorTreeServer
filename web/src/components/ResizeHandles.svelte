<script lang="ts">
  // Invisible strips along the edges and squares at the corners of a note or group; dragging
  // one resizes it (the canvas does the resizing).
  let { id }: { id: string } = $props();
  const EDGES = ['l', 'r', 't', 'b', 'tl', 'tr', 'bl', 'br'];
</script>

{#each EDGES as edge (edge)}
  <div class="resize resize-{edge}" data-resize={edge} data-node-id={id} data-testid="resize-{edge}-{id}"></div>
{/each}
