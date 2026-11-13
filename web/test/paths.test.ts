import { describe, expect, it } from 'vitest';
import { findingLabel, findingTarget, routeTo } from '../src/lib/findings';
import { entryTypeOf, interfaceRows, interfaceSections } from '../src/lib/iface';
import { relativeFile, resolveSubtree, treeKeyOf } from '../src/lib/paths';
import type { TreeDocument, WireNode } from '../src/lib/types';

const id = (n: number) => n.toString(16).padStart(32, '0');

function doc(key: string, nodes: WireNode[]): TreeDocument {
  return { key, path: `${key}_BT.json`, rev: 1, version: 1, unsaved: false, config: {}, view: { zoom: 1, center: [0, 0] }, nodes, connections: [], problems: [] };
}

describe('tree names and Sub-Tree files', () => {
  it('reads the four spellings of a tree name', () => {
    for (const name of ['robot', 'robot.json', 'robot_BT.json', 'robot_BB.json']) expect(treeKeyOf(name)).toBe('robot');
    expect(treeKeyOf(String.raw`cells\patrol_BT.json`)).toBe('cells/patrol');
  });

  it('resolves a File relative to the folder of the tree that holds it', () => {
    expect(resolveSubtree('robot', 'patrol_BT.json')).toBe('patrol');
    expect(resolveSubtree('cells/robot', 'patrol_BT.json')).toBe('cells/patrol');
    expect(resolveSubtree('cells/robot', '../shared/dock_BT.json')).toBe('shared/dock');
    expect(resolveSubtree('cells/robot', String.raw`sub\x_BT.json`)).toBe('cells/sub/x');
    expect(resolveSubtree('robot', '../outside_BT.json')).toBeNull();
    expect(resolveSubtree('robot', String.raw`C:\trees\patrol_BT.json`)).toBeNull();
    expect(resolveSubtree('robot', '')).toBeNull();
  });

  it('writes a File relative to the holding tree, as the gateway does', () => {
    expect(relativeFile('robot', 'patrol')).toBe('patrol_BT.json');
    expect(relativeFile('cells/robot', 'cells/patrol')).toBe('patrol_BT.json');
    expect(relativeFile('cells/robot', 'shared/dock')).toBe('../shared/dock_BT.json');
    expect(relativeFile('robot', 'cells/patrol')).toBe('cells/patrol_BT.json');
    for (const [holder, target] of [['a/b/c', 'a/x/y'], ['robot', 'patrol'], ['x/y', 'z']]) {
      expect(resolveSubtree(holder, relativeFile(holder, target))).toBe(target);
    }
  });
});

describe('findings (list text and the way to the node)', () => {
  const sub = { id: id(2), type: 'Sub-Tree', title: 'patrol', x: 0, y: 0, fields: { File: 'patrol_BT.json' } };
  const docs = {
    robot: doc('robot', [{ id: id(1), type: 'Root', title: 'Root', x: 0, y: 0 }, sub]),
    patrol: doc('patrol', [{ id: id(3), type: 'Evaluation', title: 'Battery ok', x: 0, y: 0 }]),
  };

  it('names the node and its tree instead of the id', () => {
    expect(findingLabel({ path: `tree:patrol/${id(3)}`, severity: 'error', text: 'x' }, docs, 'robot')).toBe('patrol › Battery ok');
    expect(findingLabel({ path: `${id(2)}/${id(3)}`, severity: 'error', text: 'x' }, docs, 'robot')).toBe('patrol › Battery ok');
    expect(findingLabel({ path: 'blackboard:battery', severity: 'warning', text: 'x' }, docs, 'robot')).toBe('blackboard entry battery');
    expect(findingLabel({ path: `tree:other/${id(9)}`, severity: 'warning', text: 'x' }, docs, 'robot')).toMatch(/^other › node 00000000/);
  });

  it('finds the Sub-Tree nodes leading to a sub-tree', () => {
    expect(routeTo(docs, 'robot', 'patrol')).toEqual([id(2)]);
    expect(routeTo(docs, 'robot', 'robot')).toEqual([]);
    expect(routeTo(docs, 'robot', 'elsewhere')).toBeNull();
    expect(findingTarget(`tree:cells/patrol/${id(3)}`)).toEqual({ kind: 'tree', key: 'cells/patrol', id: id(3) });
    expect(findingTarget(`${id(2)}/${id(3)}`)).toEqual({ kind: 'instance', ids: [id(2), id(3)] });
  });
});

describe('interface descriptions (contract 5.8 item 4)', () => {
  const battery = {
    message: [
      { name: 'header', kind: 'std_msgs/msg/Header', fields: [{ name: 'frame_id', kind: 'string' }] },
      { name: 'percentage', kind: 'float32' },
      { name: 'present', kind: 'boolean' },
      { name: 'cell_voltage', kind: 'sequence<float32>' },
      { name: 'power_supply_status', kind: 'uint8' },
    ],
  };

  it('lists the field paths a source can read, with the entry type each fills', () => {
    const rows = interfaceRows(interfaceSections(battery).Message);
    expect(rows.map((row) => [row.path, entryTypeOf(row.kind)])).toEqual([
      ['header', 'Dictionary'],
      ['header.frame_id', 'String'],
      ['percentage', 'Double'],
      ['present', 'Bool'],
      ['cell_voltage', 'List'],
      ['power_supply_status', 'Integer'],
    ]);
  });

  it('takes sections in any case, or a bare list', () => {
    expect(Object.keys(interfaceSections({ Goal: [], RESULT: { fields: [] }, feedback: [] }))).toEqual(['Goal', 'Result', 'Feedback']);
    expect(Object.keys(interfaceSections([{ name: 'x', kind: 'int32' }], 'Request'))).toEqual(['Request']);
  });
});
