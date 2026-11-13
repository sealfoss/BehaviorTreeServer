import { describe, expect, it } from 'vitest';
import { CLIPBOARD_FORMAT, copyText, fileNodeToWire, planPaste, readClipboard } from '../src/lib/clipboard';
import { rectsIntersect } from '../src/lib/geometry';
import type { CatalogEntry, Connection, WireNode } from '../src/lib/types';

const nodes: WireNode[] = [
  { id: 'root', type: 'Root', title: 'Root', x: 0, y: 0, composite: 'Sequence', memory: true },
  { id: 'seq', type: 'Sequence', title: 'Sequence', x: 0, y: 100, memory: true },
  { id: 'a', type: 'Print Message', title: 'A', x: -100, y: 200, fields: { Level: 3, Source: null, Message: 'a' } },
  { id: 'b', type: 'UDP Message', title: 'B', x: 100, y: 200, fields: { 'Destination IP': '127.0.0.1', 'Destination Port': 5005, Message: '' } },
];
const connections: Connection[] = [
  { parent: 'root', child: 'seq' },
  { parent: 'seq', child: 'a' },
  { parent: 'seq', child: 'b' },
];
const size = () => ({ w: 160, h: 80 });

let counter = 0;
const newId = () => (++counter).toString(16).padStart(32, '0');

describe('clipboard JSON', () => {
  it('never copies the Root, nor connections to nodes not copied', () => {
    const text = copyText(nodes, connections, ['root', 'seq', 'a'], size)!;
    const data = JSON.parse(text);
    expect(data.format).toBe(CLIPBOARD_FORMAT);
    expect(data.version).toBe(1);
    expect(data.nodes.map((n: WireNode) => n.id)).toEqual(['seq', 'a']);
    expect(data.connections).toEqual([{ parent: 'seq', child: 'a' }]);
    expect(copyText(nodes, connections, ['root'], size)).toBeNull();
  });

  it('writes version 2 when a copied node uses what the widget lacks', () => {
    const call: WireNode = { id: 'c', type: 'CallAction', title: 'Go', x: 0, y: 0, fields: {} };
    expect(JSON.parse(copyText([call], [], ['c'], size)!).version).toBe(2);
  });

  it('reads back what it wrote (round trip)', () => {
    const text = copyText(nodes, connections, ['seq', 'a', 'b'], size)!;
    const data = readClipboard(text)!;
    expect(data.nodes).toEqual(nodes.slice(1));
    expect(data.connections).toEqual(connections.slice(1));
    expect(data.sizes).toEqual({ seq: [160, 80], a: [160, 80], b: [160, 80] });
    expect(data.encoding).toBe('wire');
  });

  it('refuses text that is not node data', () => {
    expect(readClipboard('hello')).toBeNull();
    expect(readClipboard(JSON.stringify({ format: 'other', version: 1, nodes: [] }))).toBeNull();
    expect(readClipboard(JSON.stringify({ format: CLIPBOARD_FORMAT, version: 9, nodes: [] }))).toBeNull();
    expect(readClipboard(JSON.stringify({ format: CLIPBOARD_FORMAT, version: 1, nodes: {} }))).toBeNull();
  });
});

describe('paste placement (P-03)', () => {
  it('gives new ids, keeps the arrangement and the connections, and keeps 20 px from every node', () => {
    const data = readClipboard(copyText(nodes, connections, ['seq', 'a', 'b'], size)!)!;
    const obstacles = nodes.map((n) => ({ x: n.x, y: n.y, w: 160, h: 80 }));
    const plan = planPaste(data, obstacles, newId, () => undefined, size);
    expect(plan.nodes).toHaveLength(3);
    const ids = plan.nodes.map((n) => n.id);
    expect(ids.every((id) => /^[0-9a-f]{32}$/.test(id) && !nodes.some((n) => n.id === id))).toBe(true);
    const [seq, a, b] = plan.nodes;
    expect(plan.connections).toEqual([{ parent: seq.id, child: a.id }, { parent: seq.id, child: b.id }]);
    // Arrangement kept: the same offsets between the copies as between the originals.
    expect([a.x - seq.x, a.y - seq.y, b.x - seq.x, b.y - seq.y]).toEqual([-100, 100, 100, 100]);
    for (const copy of plan.nodes) {
      const grown = { x: copy.x - 19.99, y: copy.y - 19.99, w: 160 + 39.98, h: 80 + 39.98 };
      for (const obstacle of obstacles) expect(rectsIntersect(grown, obstacle)).toBe(false);
    }
  });

  it('pastes where the originals were when that place is free', () => {
    const data = readClipboard(copyText(nodes, connections, ['a'], size)!)!;
    const plan = planPaste(data, [], newId, () => undefined, size);
    expect(plan.nodes[0]).toMatchObject({ x: -100, y: 200 });
  });

  it('converts nodes in tree-file form to wire form', () => {
    const spec = {
      type: 'MoveTo', title: 'Move To', kind: 'leaf', category: 'Custom', leaf_kind: 'thread', special: null, waits_for_cancels: true, needs_ros: false, builtin: false, doc: '',
      fields: [
        { name: 'x', kind: 'float', default: 0.0, doc: '' },
        { name: 'targets', kind: 'other', default: null, doc: '' },
      ],
    } as CatalogEntry;
    const fileNode: WireNode = { id: 'm', type: 'MoveTo', title: 'Move To', x: 0, y: 0, fields: { x: 1, targets: { __set__: [1, 2] } } };
    expect(fileNodeToWire(fileNode, spec).fields).toEqual({ x: '1.0', targets: '{1, 2}' });
    const text = JSON.stringify({ format: CLIPBOARD_FORMAT, version: 1, nodes: [fileNode], connections: [] });
    const plan = planPaste(readClipboard(text)!, [], newId, () => spec, size);
    expect(plan.nodes[0].fields).toEqual({ x: '1.0', targets: '{1, 2}' });
  });
});
