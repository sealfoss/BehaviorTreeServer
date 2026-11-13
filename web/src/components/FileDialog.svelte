<script lang="ts">
  // Server-side files (design 9.4, contract 5.4): the trees under trees_dir, New, Load,
  // Save As, Rename, Delete, Upload and Download.
  import { session } from '../lib/stores/session.svelte';

  let { mode, onclose }: { mode: 'new' | 'load' | 'save_as' | 'browse'; onclose: () => void } = $props();

  interface TreeItem {
    key: string;
    dirs?: string[];
    mtime?: number;
    open_in?: string | null;
  }

  let trees = $state<TreeItem[]>([]);
  let dirs = $state<string[]>([]);
  let name = $state('');
  let chosen = $state<string | null>(null);
  let busy = $state(false);
  let uploadDir = $state('');
  let fileInput = $state<HTMLInputElement | null>(null);

  const title = $derived({ new: 'New Tree', load: 'Load Tree', save_as: 'Save As', browse: 'Files' }[mode]);

  async function refresh(): Promise<void> {
    const reply = await session.file('list');
    if (!reply) return;
    trees = ((reply.trees ?? []) as TreeItem[]).slice().sort((a, b) => a.key.localeCompare(b.key));
    dirs = (reply.dirs ?? []) as string[];
  }

  $effect(() => {
    void refresh();
  });

  function pick(item: TreeItem): void {
    chosen = item.key;
    name = item.key;
  }

  /** New and Load replace the session's tree: take the whole session again. */
  function reattach(): void {
    if (session.executors.attached) session.conn?.send({ t: 'attach', executor: session.executors.attached });
  }

  async function primary(): Promise<void> {
    const target = name.trim();
    if (!target) return;
    busy = true;
    try {
      if (mode === 'new') {
        if (await session.file('new', { name: target })) {
          reattach();
          onclose();
        }
      } else if (mode === 'load') {
        if (await session.file('load', { name: target })) {
          reattach();
          onclose();
        }
      } else if (mode === 'save_as') {
        const level = session.view.level;
        if (level?.kind !== 'doc') return;
        const reply = await session.file('save_as', { doc: level.doc, name: target, view: { zoom: session.view.zoom, center: session.view.center() } });
        if (reply) {
          const warnings = (reply.warnings ?? []) as string[];
          if (warnings.length) session.ui.notify('warning', warnings.join('\n'), 15000);
          if (level.doc === session.docs.root) reattach();
          onclose();
        }
      }
    } finally {
      busy = false;
    }
  }

  async function rename(): Promise<void> {
    if (!chosen) return;
    const answer = await session.ui.ask({
      title: 'Rename',
      text: `New name for ${chosen}:`,
      input: { label: 'Name', value: chosen },
      buttons: [
        { label: 'Rename', value: 'ok', primary: true },
        { label: 'Cancel', value: 'cancel' },
      ],
      testid: 'dialog-rename-file',
    });
    if (answer?.button !== 'ok' || !answer.input.trim()) return;
    if (await session.file('rename', { name: chosen, new_name: answer.input.trim() })) await refresh();
  }

  async function remove(): Promise<void> {
    if (!chosen) return;
    const answer = await session.ui.ask({
      title: 'Delete',
      text: `Delete the tree ${chosen} (its _BT.json and _BB.json files)?`,
      buttons: [
        { label: 'Delete', value: 'ok', primary: true },
        { label: 'Cancel', value: 'cancel' },
      ],
      testid: 'dialog-delete-file',
    });
    if (answer?.button !== 'ok') return;
    if (await session.file('delete', { name: chosen })) {
      chosen = null;
      await refresh();
    }
  }

  async function upload(): Promise<void> {
    const files = fileInput?.files;
    if (!files?.length) return;
    const form = new FormData();
    for (const file of files) form.append('file', file, file.name);
    form.append('dir', uploadDir);
    busy = true;
    try {
      const response = await fetch('/upload', { method: 'POST', body: form });
      if (!response.ok) session.ui.notify('warning', `Upload refused: ${(await response.text()) || response.status}`);
      else session.ui.notify('info', 'Uploaded.', 2500);
    } catch (error) {
      session.ui.notify('error', `Upload failed: ${(error as Error).message}`);
    } finally {
      busy = false;
      if (fileInput) fileInput.value = '';
      await refresh();
    }
  }

  function formatTime(mtime?: number): string {
    return mtime ? new Date(mtime * 1000).toLocaleString() : '';
  }
</script>

<div class="modal-backdrop">
  <div class="dialog file-dialog" role="dialog" aria-modal="true" aria-label={title} data-testid="dialog-files">
    <h2>{title}</h2>
    <div class="file-list" data-testid="file-list">
      {#each trees as item (item.key)}
        <button
          type="button"
          class="file-row"
          class:chosen={chosen === item.key}
          onclick={() => pick(item)}
          ondblclick={() => {
            pick(item);
            if (mode !== 'browse') void primary();
          }}
          data-testid="file-{item.key}"
        >
          <span class="file-key">{item.key}</span>
          <span class="file-meta">{item.open_in ? `open in ${item.open_in}` : ''} {formatTime(item.mtime)}</span>
        </button>
      {:else}
        <p class="muted">No trees yet.</p>
      {/each}
    </div>
    {#if mode !== 'browse'}
      <label class="dialog-field">
        <span>Name</span>
        <input type="text" bind:value={name} placeholder="robot or cells/patrol" data-testid="file-name" onkeydown={(e) => e.key === 'Enter' && void primary()} />
      </label>
    {/if}
    <div class="file-actions">
      <button type="button" disabled={!chosen} onclick={rename} data-testid="btn-file-rename">Rename…</button>
      <button type="button" disabled={!chosen} onclick={remove} data-testid="btn-file-delete">Delete…</button>
      <a class="button" class:disabled={!chosen} href={chosen ? `/download/${chosen.split('/').map(encodeURIComponent).join('/')}` : undefined} download data-testid="btn-file-download">Download</a>
      <span class="upload">
        <input type="file" multiple accept=".json" bind:this={fileInput} data-testid="file-upload-input" />
        <select bind:value={uploadDir} title="Folder to upload into" data-testid="file-upload-dir">
          <option value="">(top folder)</option>
          {#each dirs as dir (dir)}<option value={dir}>{dir}</option>{/each}
        </select>
        <button type="button" onclick={upload} disabled={busy} data-testid="btn-file-upload">Upload</button>
      </span>
    </div>
    <div class="dialog-buttons">
      {#if mode !== 'browse'}
        <button type="button" class="primary" disabled={busy || !name.trim()} onclick={primary} data-testid="btn-file-ok">
          {mode === 'new' ? 'Create' : mode === 'load' ? 'Load' : 'Save'}
        </button>
      {/if}
      <button type="button" onclick={onclose} data-testid="btn-file-close">{mode === 'browse' ? 'Close' : 'Cancel'}</button>
    </div>
  </div>
</div>
