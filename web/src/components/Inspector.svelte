<script lang="ts">
  // The inspector (design 11.3): the whole form of the selected leaf. For a ROS leaf it is built
  // from the executor's description of the interface (ask interface); bindings, outputs and the
  // fields pinned on the node are edited here, and so are the per-node cancel options.
  import { pins } from '../lib/canvasState.svelte';
  import { interfaceRows as rows, interfaceSections, type IfaceField, type RowKind } from '../lib/iface';
  import { findNode } from '../lib/ops';
  import { session } from '../lib/stores/session.svelte';
  import type { WireNode } from '../lib/types';
  import { parsePyLiteral, pyDisplay, pyLookup, type PyValue } from '../lib/wire';
  import DoubleInput from './DoubleInput.svelte';
  import FieldEditor from './FieldEditor.svelte';

  const doc = $derived(session.shownDoc);
  const node = $derived.by((): WireNode | undefined => {
    if (!doc || session.view.selection.size !== 1) return undefined;
    const [id] = session.view.selection;
    const found = findNode(doc, id);
    return found && found.type !== 'Notes' && found.type !== 'Group' ? found : undefined;
  });
  const spec = $derived(node ? session.catalog.get(node.type) : undefined);
  const readOnly = $derived(!session.canEdit);
  const special = $derived(spec?.special ?? null);
  const ros = $derived(special === 'call_action' || special === 'call_service' || special === 'publish');
  const isLeaf = $derived(spec?.kind === 'leaf' || (!spec && node?.type !== 'Root'));
  const inputField = $derived(special === 'call_action' ? 'Goal' : special === 'call_service' ? 'Request' : 'Message');
  const targetField = $derived(special === 'call_action' ? 'Action' : special === 'call_service' ? 'Service' : 'Topic');
  const outputPrefixes = $derived(special === 'call_action' ? ['Result', 'Feedback'] : special === 'call_service' ? ['Response'] : []);

  let graph = $state<{ actions: [string, string][]; services: [string, string][]; topics: [string, string][] }>({ actions: [], services: [], topics: [] });
  let iface = $state<Record<string, IfaceField[]>>({});
  let ifaceError = $state('');
  let loadedType = '';

  const typeName = $derived(node ? String(node.fields?.Type ?? '') : '');

  $effect(() => {
    if (!ros) return;
    void session.ask('graph').then((reply) => {
      if (reply?.t !== 'ok') return;
      const g = (reply.graph ?? {}) as Record<string, [string, string | string[]][]>;
      const norm = (list?: [string, string | string[]][]) => (list ?? []).map(([n, t]) => [n, Array.isArray(t) ? (t[0] ?? '') : t] as [string, string]);
      graph = { actions: norm(g.actions), services: norm(g.services), topics: norm(g.topics) };
    });
  });

  $effect(() => {
    const type = typeName;
    if (!ros || !type || type === loadedType) return;
    loadedType = type;
    ifaceError = '';
    void session.ask('interface', { type }).then((reply) => {
      if (reply?.t !== 'ok') {
        ifaceError = reply?.reason ?? 'The interface could not be described.';
        iface = {};
        return;
      }
      iface = interfaceSections(reply.interface, inputField);
    });
  });

  const inputValue = $derived.by((): PyValue | null => {
    try {
      return node ? parsePyLiteral(String(node.fields?.[inputField] ?? '{}')) : null;
    } catch {
      return null;
    }
  });

  function valueAt(path: string): string {
    const value = pyLookup(inputValue, path);
    return value ? pyDisplay(value) : '';
  }

  function setPath(path: string, kind: RowKind, value: unknown): void {
    if (!node || !doc) return;
    void session.sendOp(doc.key, { op: 'node.set_field', id: node.id, field: inputField, path, kind: kind === 'array' ? 'other' : kind, value });
  }

  function bind(key: string, entry: string): void {
    if (node && doc) void session.sendOp(doc.key, { op: 'node.set_binding', id: node.id, key, entry: entry || null });
  }

  function output(key: string, entry: string): void {
    if (node && doc) void session.sendOp(doc.key, { op: 'node.set_output', id: node.id, key, entry: entry.trim() || null });
  }

  function togglePin(path: string, kind: string, on: boolean): void {
    if (!node) return;
    const current = pins.of(node.id).filter((pin) => pin.split('|')[0] !== path);
    pins.set(node.id, on ? [...current, `${path}|${kind}`] : current);
  }

  function isPinned(path: string): boolean {
    return !!node && pins.of(node.id).some((pin) => pin.split('|')[0] === path);
  }

  function setOption(name: string, value: unknown): void {
    if (node && doc) void session.sendOp(doc.key, { op: 'node.set_option', id: node.id, name, value });
  }

  function targetChosen(event: Event): void {
    if (!node || !doc) return;
    const name = (event.currentTarget as HTMLInputElement).value;
    void session.sendOp(doc.key, { op: 'node.set_field', id: node.id, field: targetField, value: name });
    const list = special === 'call_action' ? graph.actions : special === 'call_service' ? graph.services : graph.topics;
    const known = list.find(([n]) => n === name);
    if (known?.[1] && known[1] !== typeName) void session.sendOp(doc.key, { op: 'node.set_field', id: node.id, field: 'Type', value: known[1] });
  }

  // Remote Sub-Tree: bindings and outputs are remote entry name -> local entry name.
  let newRemote = $state('');
  let newLocal = $state('');
  const bindings = $derived((node?.bindings ?? {}) as Record<string, string>);
  const outputs = $derived((node?.outputs ?? {}) as Record<string, string>);
  const entryNames = $derived(session.blackboard.entries.map((entry) => entry.name));
</script>

<datalist id="inspector-entries">
  {#each entryNames as name (name)}<option value={name}></option>{/each}
</datalist>

<section class="inspector" data-testid="inspector">
  {#if !node || !doc}
    <p class="muted">Select one node to see all of its fields here.</p>
  {:else}
    <h3 data-testid="inspector-title">{node.title} <span class="muted">({spec?.title ?? node.type})</span></h3>
    <div class="form">
      <label for="insp-title">Title:</label>
      <input
        id="insp-title"
        value={node.title}
        disabled={readOnly}
        onchange={(e) => void session.sendOp(doc.key, { op: 'node.set_title', id: node.id, title: (e.currentTarget as HTMLInputElement).value })}
        data-testid="inspector-title-input"
      />
    </div>

    {#if ros}
      <div class="form">
        <label for="insp-target">{targetField}:</label>
        <input id="insp-target" list="insp-targets" value={String(node.fields?.[targetField] ?? '')} disabled={readOnly} onchange={targetChosen} data-testid="inspector-target" />
        <datalist id="insp-targets">
          {#each special === 'call_action' ? graph.actions : special === 'call_service' ? graph.services : graph.topics as [name, type] (name)}
            <option value={name}>{type}</option>
          {/each}
        </datalist>
        {#each spec?.fields.filter((f) => f.name !== targetField && f.kind !== 'other') ?? [] as field (field.name)}
          <label for="insp-{field.name}">{field.name}:</label>
          <FieldEditor {node} {field} docKey={doc.key} {readOnly} />
        {/each}
      </div>
      {#if ifaceError}<p class="error">{ifaceError}</p>{/if}
      <h4>{inputField}</h4>
      <table class="iface">
        <thead><tr><th>Field</th><th>Value</th><th>Bound to</th><th title="Show on the node">Pin</th></tr></thead>
        <tbody>
          {#each rows(iface[inputField]) as row (row.path)}
            {@const key = `${inputField}.${row.path}`}
            <tr data-testid="iface-row-{key}">
              <td style="padding-left: {row.depth * 12 + 4}px">{row.path.split('.').pop()}</td>
              <td>
                {#if bindings[key]}
                  <span class="binding" class:missing={!session.blackboard.byName(bindings[key])}>⇐ {bindings[key]}{session.blackboard.byName(bindings[key]) ? '' : ' (missing)'}</span>
                {:else if row.kind === 'msg'}
                  <span class="muted">message</span>
                {:else if row.kind === 'bool'}
                  <input type="checkbox" checked={valueAt(row.path) === 'True'} disabled={readOnly} onchange={(e) => setPath(row.path, 'bool', (e.currentTarget as HTMLInputElement).checked)} />
                {:else if row.kind === 'float'}
                  <DoubleInput value={valueAt(row.path)} disabled={readOnly} onchange={(wire) => setPath(row.path, 'float', wire)} />
                {:else if row.kind === 'int'}
                  <input type="number" step="1" value={valueAt(row.path)} disabled={readOnly} onchange={(e) => setPath(row.path, 'int', Number((e.currentTarget as HTMLInputElement).value))} />
                {:else}
                  <input type="text" value={valueAt(row.path)} disabled={readOnly} onchange={(e) => setPath(row.path, row.kind === 'array' ? 'array' : 'str', (e.currentTarget as HTMLInputElement).value)} />
                {/if}
              </td>
              <td>
                <select value={bindings[key] ?? ''} disabled={readOnly} onchange={(e) => bind(key, (e.currentTarget as HTMLSelectElement).value)} data-testid="bind-{key}">
                  <option value="">(value)</option>
                  {#each entryNames as name (name)}<option value={name}>{name}</option>{/each}
                  {#if bindings[key] && !entryNames.includes(bindings[key])}<option value={bindings[key]}>{bindings[key]} (missing)</option>{/if}
                </select>
              </td>
              <td>
                {#if row.kind !== 'msg'}
                  <input type="checkbox" checked={isPinned(key)} onchange={(e) => togglePin(key, row.kind === 'array' ? 'other' : row.kind, (e.currentTarget as HTMLInputElement).checked)} data-testid="pin-{key}" />
                {/if}
              </td>
            </tr>
          {:else}
            <tr><td colspan="4" class="muted">{typeName ? 'No fields described.' : 'Choose a type first.'}</td></tr>
          {/each}
        </tbody>
      </table>
      {#each outputPrefixes as prefix (prefix)}
        <h4>Outputs from {prefix}</h4>
        <table class="iface">
          <tbody>
            {#each rows(iface[prefix]) as row (row.path)}
              {@const key = `${prefix}.${row.path}`}
              <tr>
                <td style="padding-left: {row.depth * 12 + 4}px">{row.path.split('.').pop()}</td>
                <td>
                  <input list="inspector-entries" value={outputs[key] ?? ''} placeholder="(not copied)" disabled={readOnly} onchange={(e) => output(key, (e.currentTarget as HTMLInputElement).value)} data-testid="output-{key}" />
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/each}
    {:else if special === 'remote_subtree'}
      <h4>Entries copied in (remote ⇐ local)</h4>
      <table class="iface">
        <tbody>
          {#each Object.entries(bindings) as [remote, localName] (remote)}
            <tr><td>{remote}</td><td class:missing={!session.blackboard.byName(localName)}>{localName}{session.blackboard.byName(localName) ? '' : ' (missing)'}</td>
              <td><button type="button" disabled={readOnly} onclick={() => bind(remote, '')}>Remove</button></td></tr>
          {/each}
        </tbody>
      </table>
      <h4>Entries copied back (remote ⇒ local)</h4>
      <table class="iface">
        <tbody>
          {#each Object.entries(outputs) as [remote, localName] (remote)}
            <tr><td>{remote}</td><td>{localName}</td><td><button type="button" disabled={readOnly} onclick={() => output(remote, '')}>Remove</button></td></tr>
          {/each}
        </tbody>
      </table>
      {#if !readOnly}
        <div class="form">
          <label for="insp-remote">Remote entry:</label>
          <input id="insp-remote" bind:value={newRemote} data-testid="remote-entry" />
          <label for="insp-local">Local entry:</label>
          <input id="insp-local" list="inspector-entries" bind:value={newLocal} data-testid="local-entry" />
        </div>
        <button type="button" disabled={!newRemote.trim() || !newLocal.trim()} onclick={() => { bind(newRemote.trim(), newLocal.trim()); newRemote = ''; newLocal = ''; }}>Copy in</button>
        <button type="button" disabled={!newRemote.trim() || !newLocal.trim()} onclick={() => { output(newRemote.trim(), newLocal.trim()); newRemote = ''; newLocal = ''; }}>Copy back</button>
      {/if}
    {:else if isLeaf && spec}
      <h4>Fields</h4>
      <table class="iface">
        <thead><tr><th>Field</th><th>Value</th><th>Bound to</th></tr></thead>
        <tbody>
          {#each spec.fields as field (field.name)}
            <tr>
              <td title={field.doc}>{field.name}</td>
              <td>
                {#if bindings[field.name]}
                  <span class="binding" class:missing={!session.blackboard.byName(bindings[field.name])}>⇐ {bindings[field.name]}</span>
                {:else}
                  <FieldEditor {node} {field} docKey={doc.key} {readOnly} />
                {/if}
              </td>
              <td>
                {#if spec.special !== 'evaluation' && spec.special !== 'set' && spec.special !== 'print'}
                  <select value={bindings[field.name] ?? ''} disabled={readOnly} onchange={(e) => bind(field.name, (e.currentTarget as HTMLSelectElement).value)} data-testid="bind-{field.name}">
                    <option value="">(value)</option>
                    {#each entryNames as name (name)}<option value={name}>{name}</option>{/each}
                  </select>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}

    {#if isLeaf}
      <h4>Cancelling</h4>
      <div class="form">
        <label for="insp-wfc">Start waits for cancels:</label>
        <select
          id="insp-wfc"
          value={node.wait_for_cancels === undefined ? '' : String(node.wait_for_cancels)}
          disabled={readOnly}
          onchange={(e) => {
            const v = (e.currentTarget as HTMLSelectElement).value;
            setOption('wait_for_cancels', v === '' ? null : v === 'true');
          }}
          data-testid="inspector-wait-for-cancels"
        >
          <option value="">(the tree's option)</option>
          <option value="true">yes</option>
          <option value="false">no</option>
        </select>
        <label for="insp-cto">Cancel timeout (ms):</label>
        <input
          id="insp-cto"
          type="number"
          min="0"
          placeholder="(the tree's option)"
          value={node.cancel_timeout_ms ?? ''}
          disabled={readOnly}
          onchange={(e) => {
            const v = (e.currentTarget as HTMLInputElement).value;
            setOption('cancel_timeout_ms', v === '' ? null : Math.max(0, Math.round(Number(v))));
          }}
          data-testid="inspector-cancel-timeout"
        />
      </div>
    {/if}
  {/if}
</section>
