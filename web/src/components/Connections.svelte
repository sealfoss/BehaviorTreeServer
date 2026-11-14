<script lang="ts">
  // Connection lines as SVG paths in the world layer, each with its order number (design 11.3).
  // A line follows the measured labels of its two nodes; only the lines of a moved node change.
  import { badgeCenter, connectionPath, type Point } from '../lib/geometry';
  // The line of a connection being dragged is drawn by the canvas above the nodes.
  import { estimateSize, session } from '../lib/stores/session.svelte';
  import type { TreeDocument, WireNode } from '../lib/types';

  let { doc }: { doc: TreeDocument } = $props();

  const byId = $derived(new Map(doc.nodes.map((node) => [node.id, node])));
  const lines = $derived.by(() => {
    const counters = new Map<string, number>();
    return doc.connections.map((c) => {
      const order = (counters.get(c.parent) ?? 0) + 1;
      counters.set(c.parent, order);
      return { ...c, order };
    });
  });

  function childrenAnchor(node: WireNode): Point {
    const size = session.view.sizes[node.id];
    const fallback = estimateSize(node);
    return { x: node.x + (size?.w ?? fallback.w) / 2, y: node.y + (size?.childrenBottom ?? fallback.h) };
  }

  function parentAnchor(node: WireNode): Point {
    const size = session.view.sizes[node.id];
    const fallback = estimateSize(node);
    return { x: node.x + (size?.w ?? fallback.w) / 2, y: node.y + (size?.parentTop ?? 0) };
  }

  function isSelected(parent: string, child: string): boolean {
    const selected = session.view.selectedConnection;
    return !!selected && selected.parent === parent && selected.child === child;
  }
</script>

<svg class="connections" aria-hidden="true">
  {#each lines as line (line.parent + '>' + line.child)}
    {@const parent = byId.get(line.parent)}
    {@const child = byId.get(line.child)}
    {#if parent && child}
      {@const start = childrenAnchor(parent)}
      {@const end = parentAnchor(child)}
      {@const d = connectionPath(start, end)}
      {@const badge = badgeCenter(end)}
      <g class="connection" class:selected={isSelected(line.parent, line.child)} data-testid="conn-{line.parent}-{line.child}">
        <path class="hit" {d} data-conn="{line.parent} {line.child}" />
        <path class="line" {d} />
        <g class="badge" data-conn="{line.parent} {line.child}" data-testid="order-{line.parent}-{line.child}">
          <circle cx={badge.x} cy={badge.y} r="8" />
          <text x={badge.x} y={badge.y}>{line.order}</text>
        </g>
      </g>
    {/if}
  {/each}
</svg>
