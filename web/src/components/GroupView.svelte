<script lang="ts">
  // A group (the widget's Group node): a frame whose title bar moves what lies completely
  // inside it. Only the title bar and the edges take clicks; the inside lets them through.
  import { canvasState } from '../lib/canvasState.svelte';
  import { measured } from '../lib/measure';
  import type { NodeSize } from '../lib/stores/view.svelte';
  import { session } from '../lib/stores/session.svelte';
  import type { TreeDocument, WireNode } from '../lib/types';
  import ResizeHandles from './ResizeHandles.svelte';

  let { node, doc, readOnly }: { node: WireNode; doc: TreeDocument; readOnly: boolean } = $props();

  const width = $derived(node.width ?? 360);
  const height = $derived(node.height ?? 240);
  const color = $derived(node.color ?? '#3d7cc9');
  const selected = $derived(session.view.selection.has(node.id));
  const editing = $derived(canvasState.editingGroup === node.id && !readOnly);

  function rgba(hex: string, alpha: number): string {
    const value = /^#?([0-9a-f]{6})$/i.exec(hex)?.[1] ?? '3d7cc9';
    const n = parseInt(value, 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
  }

  function sink(id: string, size: NodeSize): void {
    const old = session.view.sizes[id];
    if (!old || old.w !== size.w || old.h !== size.h) session.view.sizes[id] = size;
  }

  function finish(input: HTMLInputElement, keep: boolean): void {
    // Esc ends the editing first; the blur that follows when the editor goes must not commit.
    if (canvasState.editingGroup !== node.id) return;
    const title = input.value.trim();
    canvasState.editingGroup = null;
    if (keep && title && title !== node.title) void session.sendOp(doc.key, { op: 'node.set_title', id: node.id, title });
  }

  function focusEditor(element: HTMLInputElement): void {
    element.focus();
    element.select();
  }
</script>

<div
  class="group"
  class:selected
  data-node-id={node.id}
  data-group
  data-testid="node-{node.id}"
  style="transform: translate({node.x}px, {node.y}px); width: {width}px; height: {height}px; border-color: {color}; background: {rgba(color, 22 / 255)}"
  use:measured={{ id: node.id, sink }}
>
  <div class="group-title" data-group-title data-node-id={node.id} data-testid="group-title-{node.id}" style="background: {rgba(color, 85 / 255)}">
    {#if editing}
      <input
        class="group-name-editor"
        value={node.title}
        style="border-color: {color}"
        use:focusEditor
        onkeydown={(e) => {
          e.stopPropagation();
          if (e.key === 'Enter') finish(e.currentTarget as HTMLInputElement, true);
          if (e.key === 'Escape') finish(e.currentTarget as HTMLInputElement, false);
        }}
        onblur={(e) => finish(e.currentTarget as HTMLInputElement, true)}
        data-testid="group-name-editor-{node.id}"
      />
    {:else}
      <span class="group-name" data-group-name style="border-color: {color}" data-testid="group-name-{node.id}">{node.title}</span>
    {/if}
  </div>
  {#if !readOnly}<ResizeHandles id={node.id} />{/if}
</div>
