<script lang="ts">
  // The findings of validation (design 9.5, 11.5), each linked to its node; with the go-ahead
  // of an Execute that waits for it. A finding inside a sub-tree zooms into it.
  import { findingLabel, findingTarget, routeTo } from '../lib/findings';
  import { findNode } from '../lib/ops';
  import { session } from '../lib/stores/session.svelte';
  import type { Finding } from '../lib/types';

  let { onclose }: { onclose: () => void } = $props();

  /** Show what a finding is about: its node (zooming into the sub-tree that holds it), or its entry. */
  async function reveal(finding: Finding): Promise<void> {
    const target = findingTarget(finding.path);
    if (target.kind === 'blackboard') {
      session.ui.panelTab = 'blackboard';
      document.querySelector(`[data-testid="bb-row-${CSS.escape(target.name)}"]`)?.scrollIntoView({ block: 'center' });
      return;
    }
    const root = session.docs.root;
    let id: string;
    let route: string[] | null = null;
    if (target.kind === 'tree') {
      id = target.id;
      const level = session.view.level;
      route = level?.kind === 'doc' && level.doc === target.key ? null : root ? routeTo(session.docs.docs, root, target.key) : null;
      if (route === null && !(level?.kind === 'doc' && level.doc === target.key)) {
        session.ui.notify('info', `The node is in ${target.key}, which no Sub-Tree node of the trees open here runs.`);
        return;
      }
    } else if (target.kind === 'instance') {
      id = target.ids[target.ids.length - 1];
      route = target.ids.slice(0, -1);
    } else {
      session.ui.notify('info', `Not a node of the tree: ${finding.path}`);
      return;
    }
    if (route !== null) {
      while (session.view.stack.length > 1) session.goBack();
      for (const step of route) {
        const holder = session.shownDoc;
        const node = holder ? findNode(holder, step) : undefined;
        if (!node) {
          session.ui.notify('info', `The way to the node is not in the trees shown (${finding.path}).`);
          return;
        }
        await session.zoomIn(node);
      }
    }
    const doc = session.shownDoc;
    const node = doc ? findNode(doc, id) : undefined;
    if (!node) {
      session.ui.notify('info', `The node of this finding is not in the tree shown (${finding.path}).`);
      return;
    }
    session.view.select([node.id]);
    const size = session.view.sizes[node.id];
    session.view.centerOn(node.x + (size?.w ?? 150) / 2, node.y + (size?.h ?? 80) / 2);
    onclose(); // the Findings button of the toolbar opens the list again
  }

  function executeAnyway(): void {
    session.ui.findingsNeedConfirm = false;
    onclose();
    void session.command('execute', true);
  }
</script>

<div class="modal-backdrop">
  <div class="dialog findings-dialog" role="dialog" aria-modal="true" aria-label="Findings" data-testid="dialog-findings">
    <h2>Findings</h2>
    <ul class="findings">
      {#each session.ui.findings as finding, index (index)}
        <li class="finding finding-{finding.severity}">
          <button type="button" onclick={() => reveal(finding)} data-testid="finding-{index}">
            <span class="severity">{finding.severity}</span> {finding.text}
            <span class="muted" title={finding.path}>{findingLabel(finding, session.docs.docs, session.docs.root)}</span>
          </button>
        </li>
      {/each}
    </ul>
    <div class="dialog-buttons">
      {#if session.ui.findingsNeedConfirm}
        <button type="button" class="primary" onclick={executeAnyway} data-testid="btn-execute-anyway">Execute anyway</button>
      {/if}
      <button type="button" onclick={onclose} data-testid="btn-findings-close">Close</button>
    </div>
  </div>
</div>
