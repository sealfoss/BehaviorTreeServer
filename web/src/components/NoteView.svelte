<script lang="ts">
  // A note (the widget's Notes node): a box of text drawn with the largest font at which all of
  // it fits, words never split (design 11.4, P-08). Click the text to edit; drag an edge or a
  // corner to resize.
  import { canvasState } from '../lib/canvasState.svelte';
  import { measured } from '../lib/measure';
  import { domMeasure, fittedPixelSize, TEXT_MARGIN, type Measure } from '../lib/notefit';
  import type { NodeSize } from '../lib/stores/view.svelte';
  import { session } from '../lib/stores/session.svelte';
  import type { TreeDocument, WireNode } from '../lib/types';
  import ResizeHandles from './ResizeHandles.svelte';

  let { node, doc, readOnly }: { node: WireNode; doc: TreeDocument; readOnly: boolean } = $props();

  const PLACEHOLDER = 'Click to write a note';
  const width = $derived(node.width ?? 220);
  const height = $derived(node.height ?? 120);
  const text = $derived(node.text ?? '');
  const editing = $derived(canvasState.editingNote === node.id && !readOnly);
  const selected = $derived(session.view.selection.has(node.id));

  let measure: Measure | null = null;
  function probe(): Measure {
    if (!measure) {
      const element = document.createElement('div');
      element.className = 'note-probe';
      document.body.appendChild(element);
      measure = domMeasure(element);
    }
    return measure;
  }
  const pixels = $derived(fittedPixelSize(text || PLACEHOLDER, width - 2 * TEXT_MARGIN, height - 2 * TEXT_MARGIN, probe()));

  let timer: ReturnType<typeof setTimeout> | null = null;
  let draft: string | null = null;
  function typed(event: Event): void {
    draft = (event.currentTarget as HTMLTextAreaElement).value;
    if (timer) clearTimeout(timer);
    timer = setTimeout(flush, 250);
  }
  function flush(): void {
    if (timer) clearTimeout(timer);
    timer = null;
    if (draft !== null && draft !== text) void session.sendOp(doc.key, { op: 'node.set_text', id: node.id, text: draft });
    draft = null;
  }
  function finish(): void {
    flush();
    if (canvasState.editingNote === node.id) canvasState.editingNote = null;
  }

  function sink(id: string, size: NodeSize): void {
    const old = session.view.sizes[id];
    if (!old || old.w !== size.w || old.h !== size.h) session.view.sizes[id] = size;
  }

  function focusEditor(element: HTMLTextAreaElement): void {
    element.value = text;
    element.focus();
  }
</script>

<div
  class="note"
  class:selected
  data-node-id={node.id}
  data-testid="node-{node.id}"
  style="transform: translate({node.x}px, {node.y}px); width: {width}px; height: {height}px"
  use:measured={{ id: node.id, sink }}
>
  <div class="note-text" class:placeholder={!text} style="font-size: {pixels}px; padding: {TEXT_MARGIN}px" data-note-text data-testid="note-text-{node.id}">{text || PLACEHOLDER}</div>
  {#if editing}
    <textarea
      class="note-editor"
      data-testid="note-editor-{node.id}"
      use:focusEditor
      oninput={typed}
      onblur={finish}
      onkeydown={(e) => {
        e.stopPropagation();
        if (e.key === 'Escape') (e.currentTarget as HTMLTextAreaElement).blur();
      }}
    ></textarea>
  {/if}
  {#if !readOnly}<ResizeHandles id={node.id} />{/if}
</div>
