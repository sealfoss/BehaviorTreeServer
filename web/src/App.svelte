<script lang="ts">
  // The page: toolbar, canvas, side panel (Blackboard, Log, Inspector) and the dialogs.
  import BlackboardPanel from './components/BlackboardPanel.svelte';
  import Canvas from './components/Canvas.svelte';
  import ConfigureDialog from './components/ConfigureDialog.svelte';
  import ContextMenu from './components/ContextMenu.svelte';
  import EntryValueDialog from './components/EntryValueDialog.svelte';
  import FileDialog from './components/FileDialog.svelte';
  import FindingsDialog from './components/FindingsDialog.svelte';
  import Inspector from './components/Inspector.svelte';
  import LogPane from './components/LogPane.svelte';
  import Notices from './components/Notices.svelte';
  import QuestionDialog from './components/QuestionDialog.svelte';
  import SourceDialog from './components/SourceDialog.svelte';
  import Toolbar from './components/Toolbar.svelte';
  import { findNode } from './lib/ops';
  import { session } from './lib/stores/session.svelte';

  const ui = session.ui;
  let fileMode = $state<'new' | 'load' | 'save_as' | 'browse'>('load');
  let panelWidth = $state(480);

  function openFiles(mode: 'new' | 'load' | 'save_as' | 'browse'): void {
    fileMode = mode;
    ui.dialog = 'files';
  }

  function close(): void {
    ui.dialog = null;
    ui.dialogEntry = null;
  }

  // The browser tab names the trees shown, as the widget's window title does.
  $effect(() => {
    const files = session.view.stack.map((level) =>
      level.kind === 'doc' ? (session.docs.get(level.doc)?.path || `${level.doc}_BT.json`) : level.title,
    );
    document.title = files.length ? `Behavior Tree - ${files.join(' > ')}` : 'Behavior Tree';
  });

  // Selecting one ROS leaf opens its whole form in the inspector (design 11.3).
  $effect(() => {
    if (session.view.selection.size !== 1) return;
    const [id] = session.view.selection;
    const doc = session.shownDoc;
    const node = doc ? findNode(doc, id) : undefined;
    const special = node ? session.catalog.get(node.type)?.special : null;
    if (special === 'call_action' || special === 'call_service' || special === 'publish') ui.panelTab = 'inspector';
  });

  // The recovery offer after a gateway restart (design 9.4).
  let offering = false;
  $effect(() => {
    const key = ui.recovery[0];
    if (!key || offering) return;
    offering = true;
    void ui
      .ask({
        title: 'Recover unsaved changes',
        text: `Unsaved changes of ${key} were kept when the server stopped. Restore them?`,
        buttons: [
          { label: 'Restore', value: 'yes', primary: true },
          { label: 'Discard', value: 'no' },
        ],
        testid: 'dialog-recovery',
      }, key)
      .then(async (answer) => {
        // null: dismissed, or answered in another tab (the gateway said so).
        if (answer) await session.file('recover', { doc: key, accept: answer.button === 'yes' });
        ui.recovery = ui.recovery.filter((item) => item !== key);
        offering = false;
      });
  });

  function resizePanel(event: PointerEvent): void {
    const startX = event.clientX;
    const startWidth = panelWidth;
    const target = event.currentTarget as HTMLElement;
    target.setPointerCapture(event.pointerId);
    const move = (e: PointerEvent) => (panelWidth = Math.min(Math.max(startWidth - (e.clientX - startX), 240), window.innerWidth - 300));
    const up = () => {
      target.removeEventListener('pointermove', move);
      target.removeEventListener('pointerup', up);
    };
    target.addEventListener('pointermove', move);
    target.addEventListener('pointerup', up);
  }
</script>

<div class="app" class:offline={!session.executors.online}>
  <Toolbar onFiles={openFiles} />
  <main class="main">
    <Canvas />
    <div class="splitter" role="separator" aria-orientation="vertical" onpointerdown={resizePanel}></div>
    <aside class="side" style="width: {panelWidth}px">
      <nav class="tabs">
        <button type="button" class:active={ui.panelTab === 'blackboard'} onclick={() => (ui.panelTab = 'blackboard')} data-testid="tab-blackboard">Blackboard</button>
        <button type="button" class:active={ui.panelTab === 'log'} onclick={() => (ui.panelTab = 'log')} data-testid="tab-log">Log</button>
        <button type="button" class:active={ui.panelTab === 'inspector'} onclick={() => (ui.panelTab = 'inspector')} data-testid="tab-inspector">Inspector</button>
      </nav>
      <div class="tab-body" class:hidden={ui.panelTab !== 'blackboard'}><BlackboardPanel /></div>
      <div class="tab-body" class:hidden={ui.panelTab !== 'log'}><LogPane /></div>
      <div class="tab-body" class:hidden={ui.panelTab !== 'inspector'}><Inspector /></div>
    </aside>
  </main>
  {#if !session.executors.online}
    <div class="offline-banner" data-testid="offline">Offline: reconnecting…</div>
  {/if}
  {#if ui.dialog === 'files'}<FileDialog mode={fileMode} onclose={close} />{/if}
  {#if ui.dialog === 'configure'}<ConfigureDialog onclose={close} />{/if}
  {#if ui.dialog === 'findings'}<FindingsDialog onclose={close} />{/if}
  {#if ui.dialog === 'source'}<SourceDialog onclose={close} />{/if}
  {#if ui.dialog === 'entry'}<EntryValueDialog onclose={close} />{/if}
  <QuestionDialog />
  <ContextMenu />
  <Notices />
</div>
