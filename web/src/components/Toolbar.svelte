<script lang="ts">
  // The toolbar (design 11.5): New, Load, Save, Go Back, Execute, Pause, Stop, Reset,
  // Configure, Loop Execution, the executor picker, the run state and the attached count.
  import { session } from '../lib/stores/session.svelte';
  import { OUTCOMES, STATES } from '../lib/types';

  let { onFiles }: { onFiles: (mode: 'new' | 'load' | 'save_as' | 'browse') => void } = $props();

  const executors = session.executors;
  const exec = $derived(executors.attached);
  const run = $derived(session.run.run(exec));
  const state = $derived(run.state);
  const starting = $derived(session.run.starting);
  const level = $derived(session.view.level);
  const rootDoc = $derived(session.docs.get(session.docs.root));
  const shown = $derived(session.shownDoc);
  const foreign = $derived(run.active && !run.own);
  const busy = $derived(state !== 0 || starting);
  const canRun = $derived(executors.online && executors.executorOnline && !!rootDoc);

  const stateText = $derived.by(() => {
    if (!executors.online) return 'OFFLINE';
    if (!executors.executorOnline) return 'EXECUTOR DOWN';
    if (starting) return 'STARTING';
    const name = STATES[state] ?? String(state);
    if (state === 3) return `${name} (${run.cancelling} cancel${run.cancelling === 1 ? '' : 's'} outstanding)`;
    if (state === 1 || state === 2) return `${name} · loop ${run.loop + 1}`;
    if (state === 0 && session.lastOutcome) return `${name} (${OUTCOMES[session.lastOutcome] ?? ''})`;
    return name;
  });

  const breadcrumb = $derived(
    session.view.stack.map((item) => (item.kind === 'doc' ? item.doc : item.title)).join(' › '),
  );

  async function save(): Promise<void> {
    if (!level || level.kind !== 'doc') return;
    const reply = await session.file('save', { doc: level.doc, view: { zoom: session.view.zoom, center: session.view.center() } });
    if (!reply) return;
    const warnings = (reply.warnings ?? []) as string[];
    if (warnings.length) session.ui.notify('warning', warnings.join('\n'), 15000);
    else session.ui.notify('info', `Saved ${level.doc}.`, 2500);
  }

  async function revert(): Promise<void> {
    if (!level || level.kind !== 'doc') return;
    const answer = await session.ui.ask({
      title: 'Revert',
      text: `Discard the unsaved changes of ${level.doc} for everyone and reload its file?`,
      buttons: [
        { label: 'Revert', value: 'yes', primary: true },
        { label: 'Cancel', value: 'no' },
      ],
      testid: 'dialog-revert',
    });
    if (answer?.button === 'yes') await session.file('revert', { doc: level.doc });
  }

  function execute(): void {
    if (state === 2) void session.command('resume');
    else void session.command('execute');
  }

  function setLoop(event: Event): void {
    const repeat = (event.currentTarget as HTMLInputElement).checked;
    if (session.docs.root) void session.sendOp(session.docs.root, { op: 'doc.set_config', config: { repeat } });
  }
</script>

<header class="toolbar" data-testid="toolbar">
  <div class="tool-group">
    <button type="button" onclick={() => onFiles('new')} disabled={busy || !executors.online} title="Create a new tree" data-testid="btn-new">New</button>
    <button type="button" onclick={() => onFiles('load')} disabled={busy || !executors.online} title="Load a tree" data-testid="btn-load">Load</button>
    <button type="button" onclick={save} disabled={level?.kind !== 'doc' || !executors.online} title="Save the tree shown (the sub-tree zoomed into, to its own file)" data-testid="btn-save">Save</button>
    <button type="button" onclick={() => onFiles('save_as')} disabled={level?.kind !== 'doc' || !executors.online} title="Save the tree shown under another name" data-testid="btn-save-as">Save As…</button>
    <button type="button" onclick={() => onFiles('browse')} disabled={!executors.online} title="Files: rename, delete, upload, download" data-testid="btn-files">Files…</button>
    <button type="button" onclick={revert} disabled={level?.kind !== 'doc' || !shown?.unsaved || !executors.online} title="Discard the unsaved changes of the tree shown, for everyone" data-testid="btn-revert">Revert</button>
    <button type="button" onclick={() => session.goBack()} disabled={session.view.stack.length <= 1} title="Show the tree containing this sub-tree" data-testid="btn-go-back">Go Back</button>
  </div>
  <div class="tool-group run-controls">
    <button type="button" class="icon" onclick={execute} disabled={!canRun || foreign || starting || (state !== 0 && state !== 2)} title="Execute the tree (resume when paused)" data-testid="btn-execute">▶</button>
    <button type="button" class="icon" onclick={() => void session.command('pause')} disabled={!canRun || (state !== 1 && state !== 4)} title="Pause execution" data-testid="btn-pause">⏸</button>
    <button type="button" class="icon" onclick={() => void session.command('stop')} disabled={!executors.online || (state !== 1 && state !== 2 && state !== 4)} title="Stop execution and reset the tree" data-testid="btn-stop">⏹</button>
    <button type="button" class="icon" onclick={() => void session.command('reset')} disabled={!canRun || foreign || state === 3 || state === 4} title="Reset the tree without stopping execution" data-testid="btn-reset">🔄</button>
    <button type="button" class="icon" onclick={() => (session.ui.dialog = 'configure')} disabled={!rootDoc || !executors.online} title="Configure tree options" data-testid="btn-configure">⚙</button>
    <label class="check" title="Execute the tree again each time the root node returns (blackboard and node values are kept)">
      <input type="checkbox" checked={rootDoc?.config.repeat === true} disabled={!rootDoc || !executors.online} onchange={setLoop} data-testid="chk-loop" />
      Loop Execution
    </label>
  </div>
  <div class="tool-group">
    <span class="run-state state-{state}" class:offline={!executors.online} data-testid="run-state" title={run.message}>{stateText}</span>
    {#if foreign && run.bundle}
      <button type="button" onclick={() => session.showRun(exec!)} title="Show the run, read-only" data-testid="btn-view-run">View Run</button>
    {/if}
    {#if session.ui.findings.length}
      <button type="button" onclick={() => (session.ui.dialog = 'findings')} data-testid="btn-findings">Findings ({session.ui.findings.length})</button>
    {/if}
  </div>
  <div class="tool-group right">
    <span class="breadcrumb" title="The trees shown" data-testid="breadcrumb">{breadcrumb}{shown?.unsaved ? ' *' : ''}</span>
    <select
      value={exec ?? ''}
      disabled={!executors.online}
      onchange={(e) => session.attach((e.currentTarget as HTMLSelectElement).value)}
      title="The executor this page is attached to"
      data-testid="executor-picker"
    >
      {#if !exec}<option value="">(no executor)</option>{/if}
      {#each executors.list as item (item.name)}
        <option value={item.name}>{item.name}{item.online ? '' : ' (offline)'}</option>
      {/each}
    </select>
    <span class="attached" title="Browsers attached to this session" data-testid="attached-count">👥 {executors.online ? executors.attachedCount : '?'}</span>
    <span class="connection" class:online={executors.online} title={executors.online ? 'Connected' : 'Offline: reconnecting'} data-testid="connection-state">{executors.online ? '●' : 'offline'}</span>
  </div>
</header>
