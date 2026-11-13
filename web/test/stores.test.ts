import { describe, expect, it } from 'vitest';
import { applyOp, childIds, setInLiteral, type Op } from '../src/lib/ops';
import { BlackboardStore } from '../src/lib/stores/blackboard.svelte';
import { DocsStore } from '../src/lib/stores/docs.svelte';
import { LogStore } from '../src/lib/stores/log.svelte';
import { RunStore } from '../src/lib/stores/run.svelte';
import type { Entry, TreeDocument, WireNode } from '../src/lib/types';

function node(id: string, type: string, x = 0, y = 0, extra: Partial<WireNode> = {}): WireNode {
  return { id, type, title: type, x, y, ...extra };
}

function makeDoc(): TreeDocument {
  return {
    key: 'robot', path: 'robot_BT.json', rev: 1, version: 1, unsaved: false,
    config: { tick_interval_ms: 100, repeat: false }, view: { zoom: 1, center: [0, 0] },
    nodes: [
      node('root', 'Root', 0, 0, { composite: 'Sequence', memory: true }),
      node('seq', 'Sequence', 0, 100, { memory: true }),
      node('a', 'Print Message', -100, 200, { fields: { Level: 3, Source: null, Message: 'a' } }),
      node('b', 'Print Message', 100, 200, { fields: { Level: 3, Source: null, Message: 'b' } }),
      node('neg', 'Negation', 300, 100),
      node('note', 'Notes', 500, 0, { text: '', width: 220, height: 120 }),
      node('group', 'Group', -400, 0, { width: 360, height: 240, color: '#3d7cc9' }),
      node('call', 'CallAction', 0, 400, { fields: { Goal: "{'pose': {'x': 1.0}}" } }),
    ],
    connections: [
      { parent: 'root', child: 'seq' },
      { parent: 'seq', child: 'a' },
      { parent: 'seq', child: 'b' },
      { parent: 'root', child: 'neg' },
    ],
    problems: [],
  };
}

/** Apply an op twice: own edits are applied at once and again when they come back. */
function twice(doc: TreeDocument, op: Op): TreeDocument {
  applyOp(doc, op);
  const once = structuredClone(doc);
  applyOp(doc, op);
  expect(doc).toEqual(once);
  return doc;
}

describe('operations of contract 5.3', () => {
  it('node.add, with an order', () => {
    const doc = twice(makeDoc(), { op: 'node.add', node: node('c', 'Stop', 0, 300), order: { seq: ['b', 'a'] } });
    expect(doc.nodes.find((n) => n.id === 'c')?.type).toBe('Stop');
    expect(childIds(doc, 'seq')).toEqual(['b', 'a']);
  });

  it('node.remove takes the connections along', () => {
    const doc = twice(makeDoc(), { op: 'node.remove', ids: ['seq'] });
    expect(doc.nodes.some((n) => n.id === 'seq')).toBe(false);
    expect(doc.connections.some((c) => c.parent === 'seq' || c.child === 'seq')).toBe(false);
  });

  it('node.move sets positions and child orders', () => {
    const doc = twice(makeDoc(), { op: 'node.move', positions: { a: [150, 200] }, orders: { seq: ['b', 'a'] } });
    expect(doc.nodes.find((n) => n.id === 'a')).toMatchObject({ x: 150, y: 200 });
    expect(childIds(doc, 'seq')).toEqual(['b', 'a']);
    expect(childIds(doc, 'root')).toEqual(['seq', 'neg']); // other parents untouched
  });

  it('node.set_title, set_text, set_color, resize', () => {
    const doc = makeDoc();
    twice(doc, { op: 'node.set_title', id: 'a', title: 'Hello' });
    twice(doc, { op: 'node.set_text', id: 'note', text: 'line one\nline two' });
    twice(doc, { op: 'node.set_color', id: 'group', color: '#2e9d4f' });
    twice(doc, { op: 'node.resize', id: 'note', width: 300, height: 90, x: 480, y: -10 });
    const byId = (id: string) => doc.nodes.find((n) => n.id === id)!;
    expect(byId('a').title).toBe('Hello');
    expect(byId('note')).toMatchObject({ text: 'line one\nline two', width: 300, height: 90, x: 480, y: -10 });
    expect(byId('group').color).toBe('#2e9d4f');
  });

  it('node.set_field, whole and inside a dictionary field', () => {
    const doc = makeDoc();
    twice(doc, { op: 'node.set_field', id: 'a', field: 'Message', value: 'hi' });
    twice(doc, { op: 'node.set_field', id: 'call', field: 'Goal', path: 'pose.y', kind: 'float', value: '2.5' });
    twice(doc, { op: 'node.set_field', id: 'call', field: 'Goal', path: 'frame', kind: 'str', value: "it's" });
    expect(doc.nodes.find((n) => n.id === 'a')?.fields?.Message).toBe('hi');
    expect(doc.nodes.find((n) => n.id === 'call')?.fields?.Goal).toBe(`{'pose': {'x': 1.0, 'y': 2.5}, 'frame': "it's"}`);
  });

  it('node.set_selection, set_binding, set_output, set_option', () => {
    const doc = makeDoc();
    twice(doc, { op: 'node.set_selection', id: 'a', field: 'mode', index: 1 });
    twice(doc, { op: 'node.set_binding', id: 'call', key: 'Goal.pose', entry: 'dock_pose' });
    twice(doc, { op: 'node.set_output', id: 'call', key: 'Result.error_code', entry: 'nav_error' });
    twice(doc, { op: 'node.set_option', id: 'call', name: 'cancel_timeout_ms', value: 3000 });
    const call = doc.nodes.find((n) => n.id === 'call')!;
    expect(doc.nodes.find((n) => n.id === 'a')?.field_selections).toEqual({ mode: 1 });
    expect(call.bindings).toEqual({ 'Goal.pose': 'dock_pose' });
    expect(call.outputs).toEqual({ 'Result.error_code': 'nav_error' });
    expect(call.cancel_timeout_ms).toBe(3000);
    twice(doc, { op: 'node.set_binding', id: 'call', key: 'Goal.pose', entry: null });
    twice(doc, { op: 'node.set_option', id: 'call', name: 'cancel_timeout_ms', value: null });
    expect(call.bindings).toBeUndefined();
    expect(call.cancel_timeout_ms).toBeUndefined();
  });

  it('composite.set_type, set_memory, set_policy', () => {
    const doc = makeDoc();
    twice(doc, { op: 'composite.set_type', id: 'seq', composite: 'Parallel' });
    let seq = doc.nodes.find((n) => n.id === 'seq')!;
    expect(seq).toMatchObject({ type: 'Parallel', title: 'Parallel', policy: 'all' });
    expect(seq.memory).toBeUndefined();
    twice(doc, { op: 'composite.set_policy', id: 'seq', policy: 'any' });
    expect(seq.policy).toBe('any');
    twice(doc, { op: 'composite.set_type', id: 'seq', composite: 'Selector' });
    seq = doc.nodes.find((n) => n.id === 'seq')!;
    expect(seq.policy).toBeUndefined();
    twice(doc, { op: 'composite.set_memory', id: 'seq', memory: false });
    expect(seq.memory).toBe(false);
    twice(doc, { op: 'composite.set_type', id: 'root', composite: 'Selector' });
    expect(doc.nodes.find((n) => n.id === 'root')).toMatchObject({ type: 'Root', composite: 'Selector', title: 'Root' });
  });

  it('conn.add replaces an old parent and the child of a Negation', () => {
    const doc = makeDoc();
    twice(doc, { op: 'conn.add', parent: 'neg', child: 'a' });
    expect(childIds(doc, 'seq')).toEqual(['b']);
    expect(childIds(doc, 'neg')).toEqual(['a']);
    twice(doc, { op: 'conn.add', parent: 'neg', child: 'b' });
    expect(childIds(doc, 'neg')).toEqual(['b']);
    twice(doc, { op: 'conn.add', parent: 'seq', child: 'a', order: ['a'] });
    twice(doc, { op: 'conn.add', parent: 'seq', child: 'call', order: ['call', 'a'] });
    expect(childIds(doc, 'seq')).toEqual(['call', 'a']);
  });

  it('conn.remove', () => {
    const doc = twice(makeDoc(), { op: 'conn.remove', parent: 'seq', child: 'a' });
    expect(childIds(doc, 'seq')).toEqual(['b']);
  });

  it('paste adds nodes and their connections', () => {
    const doc = twice(makeDoc(), {
      op: 'paste',
      nodes: [node('p1', 'Sequence', 0, 600), node('p2', 'Stop', 0, 700)],
      connections: [{ parent: 'p1', child: 'p2' }],
    });
    expect(childIds(doc, 'p1')).toEqual(['p2']);
  });

  it('doc.set_config merges', () => {
    const doc = twice(makeDoc(), { op: 'doc.set_config', config: { repeat: true, cancel_timeout_ms: 3000 } });
    expect(doc.config).toEqual({ tick_interval_ms: 100, repeat: true, cancel_timeout_ms: 3000 });
  });

  it('asks for the document again for an operation it cannot follow', () => {
    expect(applyOp(makeDoc(), { op: 'something.new' })).toBe('resync');
    expect(applyOp(makeDoc(), { op: 'node.set_field', id: 'call', field: 'Goal', path: 'pose[3]', kind: 'float', value: '1.0' })).toBe('resync');
    expect(applyOp(makeDoc(), { op: 'node.set_title', id: 'nobody', title: 'x' })).toBe('ignored');
  });

  it('setInLiteral creates missing dictionaries and edits list items', () => {
    expect(setInLiteral(null, 'a.b', 3, 'int')).toBe("{'a': {'b': 3}}");
    expect(setInLiteral("{'w': [1.0, 2.0]}", 'w[1]', 'nan', 'float')).toBe("{'w': [1.0, nan]}");
    expect(setInLiteral('not python', 'a', 1, 'int')).toBeNull();
  });
});

describe('DocsStore', () => {
  it('applies broadcasts in order, own ones included, and takes their revisions', () => {
    const store = new DocsStore();
    store.replaceAll({ robot: makeDoc() }, 'robot');
    // Our own edit, shown at once...
    store.apply('robot', { op: 'node.set_field', id: 'a', field: 'Message', value: 'mine' });
    expect(store.get('robot')!.rev).toBe(1);
    // ...then the gateway's order: another browser's edit first, ours after it.
    store.apply('robot', { op: 'node.set_field', id: 'a', field: 'Message', value: 'theirs' }, 2, true);
    store.apply('robot', { op: 'node.set_field', id: 'a', field: 'Message', value: 'mine' }, 3, true);
    const doc = store.get('robot')!;
    expect(doc.nodes.find((n) => n.id === 'a')?.fields?.Message).toBe('mine');
    expect(doc.rev).toBe(3);
    expect(doc.unsaved).toBe(true);
  });

  it('values written by running leaves are not unsaved changes', () => {
    const store = new DocsStore();
    store.replaceAll({ robot: makeDoc() }, 'robot');
    store.apply('robot', { op: 'node.set_field', id: 'a', field: 'Message', value: 'leaf' }, 2, false);
    expect(store.get('robot')!.unsaved).toBe(false);
  });
});

describe('BlackboardStore', () => {
  const entry = (id: string, name: string, value: unknown = 0): Entry => ({
    id, name, type: 'Integer', value: value as number, reset: false, source: null, freshness: null, age: null, truncated: false,
  });

  it('applies changed, removed and reordered entries', () => {
    const store = new BlackboardStore();
    store.replace([entry('1', 'a'), entry('2', 'b'), entry('3', 'c')]);
    store.apply({ entries: [entry('2', 'b', 7), entry('4', 'd')], removed: ['1'], order: null });
    expect(store.entries.map((e) => e.name)).toEqual(['b', 'c', 'd']);
    expect(store.byName('b')?.value).toBe(7);
    store.apply({ order: ['4', '3', '2'] });
    expect(store.entries.map((e) => e.id)).toEqual(['4', '3', '2']);
    store.apply({ entries: [entry('3', 'renamed')] });
    expect(store.byId('3')?.name).toBe('renamed');
  });
});

describe('RunStore', () => {
  it('reads statuses by instance path, once per frame', () => {
    const frames: (() => void)[] = [];
    const store = new RunStore((fn) => frames.push(fn));
    store.applyRun({ exec: 'e', event: 'started', run_id: 'r1', label: 'robot', requested_by: 'me', node_paths: ['root', 'sub', 'sub/x'], own: true, bundle: null });
    store.applySt('e', { run_id: 'r1', state: 1, loop: 0, cancelling: 0, unsettled: 0, outcome: 0, message: '', v: '110' });
    store.applySt('e', { run_id: 'r1', state: 3, loop: 0, cancelling: 1, unsettled: 1, outcome: 0, message: '', v: '104' });
    expect(frames).toHaveLength(1); // one frame for both
    expect(store.run('e')).toMatchObject({ state: 3, cancelling: 1 }); // counts at once
    expect(store.status('e', 'sub/x')).toBe(0); // digits at the frame
    frames[0]();
    expect(store.status('e', 'root')).toBe(1);
    expect(store.status('e', 'sub')).toBe(0);
    expect(store.status('e', 'sub/x')).toBe(4);
    expect(store.status('e', 'unconnected')).toBe(0);
  });

  it('keeps details by path; null clears a kind; a new run clears them', () => {
    const store = new RunStore((fn) => fn());
    store.applyRun({ exec: 'e', event: 'started', run_id: 'r1', label: '', requested_by: '', node_paths: ['a', 'b'], own: true, bundle: null });
    store.applyNd('e', { 1: { error: 'boom', feedback: { d: 1 } } });
    expect(store.detailsOf('e', 'b')).toEqual({ error: 'boom', feedback: { d: 1 } });
    store.applyNd('e', { 1: { error: null } });
    expect(store.detailsOf('e', 'b')).toEqual({ feedback: { d: 1 } });
    store.applyRun({ exec: 'e', event: 'ended', run_id: 'r1', label: '', requested_by: '', node_paths: ['a', 'b'], own: true, bundle: null, outcome: 2 });
    expect(store.run('e')).toMatchObject({ active: false, outcome: 2 });
    store.applyRun({ exec: 'e', event: 'started', run_id: 'r2', label: '', requested_by: '', node_paths: ['a', 'b'], own: true, bundle: null });
    expect(store.detailsOf('e', 'b')).toBeUndefined();
  });
});

describe('LogStore', () => {
  const lines = (n: number, from = 0): [string, number, string][] => Array.from({ length: n }, (_, i) => ['t', 4, `line ${from + i}`]);

  it('appends batches, keeps a ring, and starts over with a new run', () => {
    const log = new LogStore(5);
    log.replace(0, lines(3));
    log.append(3, lines(4, 3));
    expect(log.rows.map((r) => r.seq)).toEqual([2, 3, 4, 5, 6]);
    log.append(5, lines(3, 5)); // overlaps: only the new ones
    expect(log.rows.map((r) => r.seq)).toEqual([3, 4, 5, 6, 7]);
    log.append(0, lines(2)); // numbers start again: a new run
    expect(log.rows.map((r) => r.seq)).toEqual([0, 1]);
  });

  it('remembers skipped lines and puts fetched older lines in front', () => {
    const log = new LogStore(100);
    log.replace(0, lines(2));
    log.append(2010, lines(2, 2010), 2008);
    expect(log.gapBefore).toBe(2010);
    expect(log.firstSeq).toBe(2010);
    log.prepend(2000, lines(10, 2000));
    expect(log.rows[0].seq).toBe(2000);
    expect(log.rows.map((r) => r.seq).slice(0, 3)).toEqual([2000, 2001, 2002]);
  });
});
