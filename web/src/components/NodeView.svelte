<script lang="ts">
  // A node: positioned HTML with its Parent label, title, status, fields and Children label,
  // laid out like the widget's .ui forms. The status is one attribute (data-status); CSS sets
  // the colours (design 11.3).
  import { canvasState } from '../lib/canvasState.svelte';
  import { measured } from '../lib/measure';
  import { childIds, parentId } from '../lib/ops';
  import type { NodeSize } from '../lib/stores/view.svelte';
  import { session } from '../lib/stores/session.svelte';
  import { STATUS_NAMES, type TreeDocument, type WireNode } from '../lib/types';
  import LeafFields from './LeafFields.svelte';

  let { node, doc, readOnly }: { node: WireNode; doc: TreeDocument; readOnly: boolean } = $props();

  const spec = $derived(session.catalog.get(node.type));
  const kind = $derived(node.type === 'Root' ? 'root' : (spec?.kind ?? (['Sequence', 'Selector', 'Parallel'].includes(node.type) ? 'composite' : node.type === 'Negation' ? 'decorator' : 'leaf')));
  const hasParentLabel = $derived(kind !== 'root');
  const hasChildrenLabel = $derived(kind === 'root' || kind === 'composite' || kind === 'decorator');
  const path = $derived(session.view.pathOf(node.id));
  const status = $derived(session.run.status(session.statusExec, path));
  const statusName = $derived(STATUS_NAMES[status] ?? 'Ready');
  const details = $derived(session.run.detailsOf(session.statusExec, path));
  const selected = $derived(session.view.selection.has(node.id));
  const hasParent = $derived(parentId(doc, node.id) !== undefined);
  const hasChildren = $derived(childIds(doc, node.id).length > 0);

  const compositeType = $derived(node.type === 'Root' ? (node.composite ?? 'Sequence') : node.type);
  const displayTitle = $derived.by(() => {
    if (node.unknown) return `${node.title} (unknown type)`;
    if (kind === 'root' || kind === 'composite') return node.title === compositeType ? node.title : `${node.title} (${compositeType})`;
    if (kind === 'decorator') return node.title === node.type ? node.title : `${node.title} (${node.type})`;
    return node.title;
  });
  const titleTooltip = $derived.by(() => {
    if (kind === 'root' || kind === 'composite') {
      return compositeType === 'Parallel' ? `Parallel - policy ${node.policy ?? 'all'}` : `${compositeType} - memory ${node.memory === false ? 'off' : 'on'}`;
    }
    if (kind === 'decorator') return 'Negation - succeeds when its child fails and fails when its child succeeds';
    return spec?.doc ?? (node.unknown ? 'This node type is not known to the executor; its data is kept.' : node.type);
  });

  /** The node details tooltip of the Status label (design 11.5). */
  const detailsText = $derived.by(() => {
    if (!details) return '';
    const parts: string[] = [];
    if (details.error) parts.push(String(details.error));
    if (details.waiting) parts.push(String(details.waiting));
    if (details.feedback !== undefined) parts.push(`feedback: ${typeof details.feedback === 'string' ? details.feedback : JSON.stringify(details.feedback)}`);
    const remote = details.remote_run as { executor?: string; run_id?: string } | undefined;
    if (remote?.executor) parts.push(`remote run on ${remote.executor}`);
    return parts.join('\n');
  });

  function labelState(kindOfLabel: 'parent' | 'children', connected: boolean): string {
    const from = canvasState.connectFrom;
    const over = canvasState.connectOver;
    if ((from?.id === node.id && from.kind === kindOfLabel) || (over?.id === node.id && over.kind === kindOfLabel)) return 'placing';
    return connected ? 'connected' : 'disconnected';
  }

  function sink(id: string, size: NodeSize): void {
    const old = session.view.sizes[id];
    if (!old || old.w !== size.w || old.h !== size.h || old.parentTop !== size.parentTop || old.childrenBottom !== size.childrenBottom) {
      session.view.sizes[id] = size;
    }
  }

  const showFields = $derived(kind === 'leaf' && ((spec?.fields.length ?? 0) > 0 || Object.keys(node.fields ?? {}).length > 0));
  const childrenText = $derived(kind === 'decorator' ? 'Child' : 'Children');
</script>

<div
  class="node kind-{kind}"
  class:selected
  class:unknown={node.unknown}
  data-node-id={node.id}
  data-testid="node-{node.id}"
  data-status={statusName.toLowerCase()}
  data-type={node.type}
  style="transform: translate({node.x}px, {node.y}px)"
  use:measured={{ id: node.id, sink }}
>
  {#if hasParentLabel}
    <div class="frame">
      <span class="conn-label" data-label="parent" data-node-id={node.id} data-state={labelState('parent', hasParent)} data-testid="parent-{node.id}">Parent</span>
    </div>
  {/if}
  <div class="frame status-frame">
    {#if canvasState.renaming === node.id && !readOnly}
      <!-- svelte-ignore a11y_autofocus -->
      <input
        class="title-editor"
        value={node.title}
        autofocus
        data-testid="rename-{node.id}"
        onkeydown={(e) => {
          const input = e.currentTarget as HTMLInputElement;
          if (e.key === 'Enter') input.blur();
          if (e.key === 'Escape') {
            input.value = node.title;
            canvasState.renaming = null;
          }
        }}
        onblur={(e) => {
          const title = (e.currentTarget as HTMLInputElement).value.trim();
          canvasState.renaming = null;
          if (title && title !== node.title) void session.sendOp(doc.key, { op: 'node.set_title', id: node.id, title });
        }}
      />
    {:else}
      <div class="title" title={titleTooltip} data-testid="title-{node.id}" data-drag-area>{displayTitle}</div>
    {/if}
    <div class="status-label" title={detailsText} data-testid="status-{node.id}" data-drag-area>{statusName}</div>
  </div>
  {#if showFields}
    <div class="frame fields-frame">
      <LeafFields {node} {spec} docKey={doc.key} {readOnly} />
    </div>
  {/if}
  {#if hasChildrenLabel}
    <div class="frame">
      <span class="conn-label" data-label="children" data-node-id={node.id} data-state={labelState('children', hasChildren)} data-testid="children-{node.id}">{childrenText}</span>
    </div>
  {/if}
</div>
