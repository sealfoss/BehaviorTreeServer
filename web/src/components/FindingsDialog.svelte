<script lang="ts">
  // The findings of validation (design 9.5, 11.5), each linked to its node; with the go-ahead
  // of an Execute that waits for it.
  import { session } from '../lib/stores/session.svelte';
  import { findNode } from '../lib/ops';
  import type { Finding } from '../lib/types';

  let { onclose }: { onclose: () => void } = $props();

  /** Show what a finding is about: `blackboard:<entry>`, `tree:<key>/<id>` or an instance path. */
  function reveal(finding: Finding): void {
    if (finding.path.startsWith('blackboard:')) {
      const name = finding.path.slice('blackboard:'.length);
      session.ui.panelTab = 'blackboard';
      document.querySelector(`[data-testid="bb-row-${CSS.escape(name)}"]`)?.scrollIntoView({ block: 'center' });
      return;
    }
    const id = finding.path.startsWith('tree:') ? finding.path.slice(finding.path.lastIndexOf('/') + 1) : finding.path.split('/').pop();
    const doc = session.shownDoc;
    const node = id && doc ? findNode(doc, id) : undefined;
    if (!node) {
      session.ui.notify('info', `The node of this finding is not in the tree shown (${finding.path}).`);
      return;
    }
    session.view.select([node.id]);
    const size = session.view.sizes[node.id];
    session.view.centerOn(node.x + (size?.w ?? 150) / 2, node.y + (size?.h ?? 80) / 2);
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
            <span class="severity">{finding.severity}</span> {finding.text} <span class="muted">{finding.path}</span>
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
