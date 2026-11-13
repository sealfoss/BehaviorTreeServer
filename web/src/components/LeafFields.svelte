<script lang="ts">
  // The editors on a leaf node, built from the catalog's field declarations (design 6.4), with
  // the special editors of Evaluation, Set, Print Message, Sub-Tree, Remote Sub-Tree and the
  // ROS leaves (which show only the fields the user pinned; the whole form is in the inspector).
  import { pins } from '../lib/canvasState.svelte';
  import { session } from '../lib/stores/session.svelte';
  import type { CatalogEntry, CatalogField, WireNode } from '../lib/types';
  import { LOG_LEVELS } from '../lib/types';
  import { relativeFile, resolveSubtree, treeKeyOf } from '../lib/paths';
  import { defaultWire, parsePyLiteral, pyLookup, pyDisplay, type Wire } from '../lib/wire';
  import DoubleInput from './DoubleInput.svelte';
  import EntrySelect from './EntrySelect.svelte';
  import FieldEditor from './FieldEditor.svelte';
  import ValueEditor from './ValueEditor.svelte';

  let { node, spec, docKey, readOnly }: { node: WireNode; spec: CatalogEntry | undefined; docKey: string; readOnly: boolean } = $props();

  const fields = $derived(node.fields ?? {});
  const special = $derived(spec?.special ?? null);
  const OPERATORS = ['==', '!=', '<', '<=', '>', '>='];

  /** A field of kind `entry`: an entry name, or null for Literal. */
  function entryName(value: unknown): string | null {
    return typeof value === 'string' && value !== '' ? value : null;
  }

  function setField(field: string, value: unknown): void {
    void session.sendOp(docKey, { op: 'node.set_field', id: node.id, field, value });
  }

  // Evaluation and Set ------------------------------------------------------------------------
  const valueName = $derived(typeof fields.ValueName === 'string' ? fields.ValueName : '');
  const valueEntry = $derived(session.blackboard.byName(valueName));
  const compareTo = $derived(entryName(fields.CompareTo));
  const printSource = $derived(entryName(fields.Source));
  const literal = $derived((fields.LiteralValue ?? null) as { type?: string; value?: Wire } | null);
  // The editor follows the typed value as the document holds it; the gateway converts the
  // literal when Value Name changes type, and sends the result like any other edit.
  const literalType = $derived(literal?.type ?? valueEntry?.type ?? 'String');

  function chooseValueName(name: string | null): void {
    const chosen = name ?? '';
    const compared = compareTo !== null ? session.blackboard.byName(compareTo) : undefined;
    setField('ValueName', chosen);
    // Another type: Compare To goes back to Literal (the widget's rule).
    const entry = session.blackboard.byName(chosen);
    if (entry && compared && compared.type !== entry.type) setField('CompareTo', null);
  }

  // Sub-Tree files ----------------------------------------------------------------------------
  const file = $derived(typeof fields.File === 'string' ? fields.File : '');
  const resolved = $derived(resolveSubtree(docKey, file));

  /** "..." of the widget's Sub-Tree node: choose a tree of the server; stored relative to this tree's folder. */
  async function browse(): Promise<void> {
    const list = await session.file('list');
    const trees = list ? ((list.trees ?? []) as { key: string }[]).map((tree) => tree.key) : [];
    const answer = await session.ui.ask({
      title: 'Choose the tree to run',
      text: 'The trees on the server:',
      buttons: [
        { label: 'Choose', value: 'ok', primary: true },
        { label: 'Cancel', value: 'cancel' },
      ],
      input: { label: 'Tree', value: resolved ?? '' },
      choices: trees,
      testid: 'dialog-choose-subtree',
    });
    const chosen = answer?.button === 'ok' ? treeKeyOf(answer.input) : '';
    if (chosen) setField('File', relativeFile(docKey, chosen));
  }

  // Generic fields ----------------------------------------------------------------------------
  const declared = $derived(spec?.fields ?? []);
  const undeclared = $derived(Object.keys(fields).filter((name) => !declared.some((f) => f.name === name)));
  const bindings = $derived((node.bindings ?? {}) as Record<string, string>);

  function rosPinned(): { path: string; kind: string }[] {
    return pins.of(node.id).map((pin) => {
      const [path, kind] = pin.split('|');
      return { path, kind: kind ?? 'other' };
    });
  }

  function pinnedValue(path: string): string {
    const [field, ...rest] = path.split('.');
    const raw = fields[field];
    if (!rest.length) return raw === null || raw === undefined ? '' : String(raw);
    try {
      const value = pyLookup(parsePyLiteral(String(raw ?? '{}')), rest.join('.'));
      return value ? pyDisplay(value) : '';
    } catch {
      return '';
    }
  }

  function setPinned(path: string, kind: string, wire: unknown): void {
    const [field, ...rest] = path.split('.');
    if (!rest.length) setField(field, wire);
    else void session.sendOp(docKey, { op: 'node.set_field', id: node.id, field, value: wire, path: rest.join('.'), kind });
  }

  function isRos(kind: string | null): boolean {
    return kind === 'call_action' || kind === 'call_service' || kind === 'publish';
  }
</script>

{#snippet label(text: string, title = '')}
  <span class="field-key" data-drag-area {title}>{text}</span>
{/snippet}

{#snippet bound(field: string)}
  {@const entry = bindings[field]}
  <span class="binding" class:missing={!session.blackboard.byName(entry)} title="Bound to the blackboard entry {entry}">
    ⇐ {entry}{session.blackboard.byName(entry) ? '' : ' (missing)'}
  </span>
{/snippet}

<div class="fields" data-testid="fields-{node.id}">
  {#if special === 'evaluation' || special === 'set'}
    {@render label('Value Name', 'The blackboard entry')}
    <EntrySelect value={valueName} disabled={readOnly} onchange={chooseValueName} testid="field-{node.id}-ValueName" />
    {#if special === 'evaluation'}
      {@render label('Operator')}
      <select value={String(fields.Operator ?? '==')} disabled={readOnly} onchange={(e) => setField('Operator', (e.currentTarget as HTMLSelectElement).value)} data-testid="field-{node.id}-Operator">
        {#each OPERATORS as operator (operator)}<option value={operator}>{operator}</option>{/each}
      </select>
    {/if}
    {@render label(special === 'set' ? 'Set To' : 'Compare To')}
    <EntrySelect
      value={compareTo}
      type={valueEntry?.type ?? null}
      literal="Literal"
      disabled={readOnly}
      onchange={(name) => setField('CompareTo', name || null)}
      testid="field-{node.id}-CompareTo"
    />
    {#if compareTo === null}
      {@render label('Literal Value')}
      <ValueEditor
        type={literalType}
        value={literal?.value ?? defaultWire(literalType)}
        disabled={readOnly}
        onchange={(wire) => setField('LiteralValue', { type: literalType, value: wire })}
        testid="field-{node.id}-LiteralValue"
      />
    {/if}
    {#if special === 'evaluation'}
      {@render label('Wait', 'Keep running until the comparison holds')}
      <input type="checkbox" checked={fields.Wait === true} disabled={readOnly} onchange={(e) => setField('Wait', (e.currentTarget as HTMLInputElement).checked)} data-testid="field-{node.id}-Wait" />
      {#if fields.Wait === true}
        {@render label('Timeout', 'Seconds; 0 waits without limit')}
        <DoubleInput value={fields.WaitTimeout ?? '0.0'} disabled={readOnly} onchange={(wire) => setField('WaitTimeout', wire)} testid="field-{node.id}-WaitTimeout" />
      {/if}
    {/if}
  {:else if special === 'print'}
    {@render label('Level')}
    <select value={String(fields.Level ?? 3)} disabled={readOnly} onchange={(e) => setField('Level', Number((e.currentTarget as HTMLSelectElement).value))} data-testid="field-{node.id}-Level">
      {#each LOG_LEVELS as text, level (level)}<option value={String(level)}>{text}</option>{/each}
    </select>
    {@render label('Print')}
    <EntrySelect value={printSource} literal="Literal" disabled={readOnly} onchange={(name) => setField('Source', name || null)} testid="field-{node.id}-Source" />
    {#if printSource === null}
      {@render label('Message')}
      <input type="text" value={String(fields.Message ?? '')} disabled={readOnly} onchange={(e) => setField('Message', (e.currentTarget as HTMLInputElement).value)} data-testid="field-{node.id}-Message" />
    {/if}
  {:else if special === 'subtree'}
    {@render label('File', 'The tree file to run, relative to the folder of this tree')}
    <span class="file-field">
      <input
        type="text"
        value={file}
        placeholder="tree_BT.json"
        title={resolved ? `${resolved}_BT.json in the trees folder` : 'No file chosen'}
        disabled={readOnly}
        onchange={(e) => setField('File', (e.currentTarget as HTMLInputElement).value)}
        onkeydown={(e) => e.key === 'Enter' && (e.currentTarget as HTMLInputElement).blur()}
        data-testid="field-{node.id}-File"
      />
      <button type="button" class="browse" title="Choose the behavior tree file to run" disabled={readOnly} onclick={browse} data-testid="browse-{node.id}">…</button>
    </span>
  {:else if special === 'remote_subtree'}
    {@render label('Executor', 'The other executor')}
    <select value={String(fields.Executor ?? '')} disabled={readOnly} onchange={(e) => setField('Executor', (e.currentTarget as HTMLSelectElement).value)} data-testid="field-{node.id}-Executor">
      <option value="">(choose an executor)</option>
      {#each session.executors.list.filter((item) => item.name !== session.executors.attached) as executor (executor.name)}
        <option value={executor.name}>{executor.name}{executor.online ? '' : ' (offline)'}</option>
      {/each}
      {#if fields.Executor && !session.executors.list.some((item) => item.name === fields.Executor)}
        <option value={String(fields.Executor)} class="missing">{fields.Executor} (missing)</option>
      {/if}
    </select>
    {#each declared.filter((f) => f.name !== 'Executor') as field (field.name)}
      {@render label(field.name, field.doc)}
      <FieldEditor {node} {field} {docKey} {readOnly} />
    {/each}
  {:else if isRos(special)}
    {@const first = declared[0] as CatalogField | undefined}
    {#if first}
      {@render label(first.name, first.doc)}
      <span class="ros-target" title={String(fields.Type ?? '')}>{String(fields[first.name] ?? '') || '(not set)'}</span>
    {/if}
    {#each rosPinned() as pin (pin.path)}
      {@render label(pin.path)}
      {#if bindings[pin.path]}
        {@render bound(pin.path)}
      {:else if pin.kind === 'float'}
        <DoubleInput value={pinnedValue(pin.path)} disabled={readOnly} onchange={(wire) => setPinned(pin.path, 'float', wire)} />
      {:else if pin.kind === 'bool'}
        <input type="checkbox" checked={pinnedValue(pin.path) === 'True'} disabled={readOnly} onchange={(e) => setPinned(pin.path, 'bool', (e.currentTarget as HTMLInputElement).checked)} />
      {:else}
        <input type="text" value={pinnedValue(pin.path)} disabled={readOnly} onchange={(e) => setPinned(pin.path, pin.kind === 'int' ? 'int' : 'str', pin.kind === 'int' ? Number((e.currentTarget as HTMLInputElement).value) : (e.currentTarget as HTMLInputElement).value)} />
      {/if}
    {/each}
  {:else}
    {#each declared as field (field.name)}
      {@render label(field.name, field.doc)}
      {#if bindings[field.name]}
        {@render bound(field.name)}
      {:else}
        <FieldEditor {node} {field} {docKey} {readOnly} />
      {/if}
    {/each}
    {#each undeclared as name (name)}
      {@render label(name)}
      <input type="text" readonly value={JSON.stringify(fields[name])} title="Not declared by the node type: read-only" />
    {/each}
  {/if}
</div>
