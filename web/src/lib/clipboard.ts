/**
 * Copy and paste (design 11.6): the widget's clipboard JSON (`nodes` and `connections` of a
 * tree file with a format name) as plain text, and the widget's placement rule for the copies:
 * the free area nearest to the originals, at least 20 px from every node, arrangement and
 * connections kept. The Root is never copied.
 *
 * Nodes are written in wire form (marked `"encoding": "wire"`), the form `paste` takes;
 * text in tree-file form (copied from a file) is converted when it is pasted.
 */

import { nearestFreeOffset, type Rect } from './geometry';
import type { CatalogEntry, Connection, WireNode } from './types';
import { doubleToWire, fileValueToPy } from './wire';

export const CLIPBOARD_FORMAT = 'behavior_tree_widget_nodes';
const WIDGET_TYPES = new Set(['Root', 'Sequence', 'Selector', 'Negation', 'Evaluation', 'Set', 'Restart', 'Pause', 'Stop', 'UDP Message', 'Print Message', 'Sub-Tree', 'Notes', 'Group']);

export interface ClipboardData {
  format: string;
  version: number;
  encoding?: string;
  nodes: WireNode[];
  connections: Connection[];
  sizes?: Record<string, [number, number]>;
}

/** The clipboard text for `ids` of `nodes` (the Root left out); null when nothing is copied. */
export function copyText(
  nodes: WireNode[],
  connections: Connection[],
  ids: Iterable<string>,
  sizeOf: (id: string) => { w: number; h: number } | undefined,
): string | null {
  const wanted = new Set(ids);
  const copied = nodes.filter((node) => wanted.has(node.id) && node.type !== 'Root');
  if (!copied.length) return null;
  const copiedIds = new Set(copied.map((node) => node.id));
  const sizes: Record<string, [number, number]> = {};
  for (const node of copied) {
    const size = sizeOf(node.id);
    if (size) sizes[node.id] = [Math.round(size.w), Math.round(size.h)];
  }
  const newFeatures = copied.some((node) => !WIDGET_TYPES.has(node.type) || node.bindings || node.outputs || node.policy);
  const data: ClipboardData = {
    format: CLIPBOARD_FORMAT,
    version: newFeatures ? 2 : 1,
    encoding: 'wire',
    nodes: copied,
    connections: connections.filter((c) => copiedIds.has(c.parent) && copiedIds.has(c.child)),
    sizes,
  };
  return JSON.stringify(data);
}

/** The node data of clipboard text, or null when it holds no nodes this page can read. */
export function readClipboard(text: string): ClipboardData | null {
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    return null;
  }
  if (!data || typeof data !== 'object') return null;
  const record = data as Record<string, unknown>;
  const version = record.version;
  if (record.format !== CLIPBOARD_FORMAT || typeof version !== 'number' || !Number.isInteger(version) || version < 1 || version > 2) return null;
  if (!Array.isArray(record.nodes)) return null;
  if (record.connections !== undefined && !Array.isArray(record.connections)) return null;
  return {
    format: CLIPBOARD_FORMAT,
    version,
    encoding: typeof record.encoding === 'string' ? record.encoding : undefined,
    nodes: (record.nodes as unknown[]).filter((n): n is WireNode => !!n && typeof n === 'object' && typeof (n as WireNode).id === 'string'),
    connections: ((record.connections as unknown[]) ?? []).filter(
      (c): c is Connection => !!c && typeof c === 'object' && typeof (c as Connection).parent === 'string' && typeof (c as Connection).child === 'string',
    ),
    sizes: (record.sizes as Record<string, [number, number]>) ?? {},
  };
}

/** A node of a tree file (JSON values, tags for sets) in wire form, using the catalog's field kinds. */
export function fileNodeToWire(node: WireNode, spec: CatalogEntry | undefined): WireNode {
  const copy = structuredClone(node);
  if (!copy.fields || !spec) return copy;
  for (const field of spec.fields) {
    if (!(field.name in copy.fields)) continue;
    const value = copy.fields[field.name];
    if ((spec.special === 'evaluation' || spec.special === 'set') && field.name === 'LiteralValue') {
      copy.fields[field.name] = value === null ? null : { type: inferEntryType(value), value: valueToWire(inferEntryType(value), value) };
    } else if (field.kind === 'float' && typeof value === 'number') {
      copy.fields[field.name] = doubleToWire(value);
    } else if (field.kind === 'other' && value !== null) {
      copy.fields[field.name] = fileValueToPy(value);
    }
  }
  return copy;
}

function inferEntryType(value: unknown): string {
  if (typeof value === 'boolean') return 'Bool';
  if (typeof value === 'number') return Number.isInteger(value) ? 'Integer' : 'Double';
  if (typeof value === 'string') return 'String';
  if (Array.isArray(value)) return 'List';
  if (value && typeof value === 'object') {
    const keys = Object.keys(value);
    if (keys.length === 1 && (keys[0] === '__set__' || keys[0] === '__frozenset__')) return 'Set';
    if (keys.length === 1 && keys[0] === '__tuple__') return 'List';
    return 'Dictionary';
  }
  return 'Object';
}

function valueToWire(type: string, value: unknown): unknown {
  if (type === 'Double' && typeof value === 'number') return doubleToWire(value);
  if (type === 'List' || type === 'Dictionary' || type === 'Set') return fileValueToPy(value);
  return value;
}

export interface PastePlan {
  nodes: WireNode[];
  connections: Connection[];
  /** The copies' sizes as the originals were drawn (or estimated): new id -> [w, h]. */
  sizes: Record<string, [number, number]>;
}

/**
 * New ids for the copies, connections between copies only, positions moved together to the
 * nearest free area (`obstacles`: every node of the view). The Root is never pasted.
 */
export function planPaste(
  data: ClipboardData,
  obstacles: Rect[],
  newId: () => string,
  catalog: (type: string) => CatalogEntry | undefined,
  estimate: (node: WireNode) => { w: number; h: number },
): PastePlan {
  const ids = new Map<string, string>();
  const nodes: WireNode[] = [];
  const rects: Rect[] = [];
  const sizes: Record<string, [number, number]> = {};
  for (const item of data.nodes) {
    if (item.type === 'Root' || ids.has(item.id)) continue;
    const id = newId();
    ids.set(item.id, id);
    const wire = data.encoding === 'wire' ? structuredClone(item) : fileNodeToWire(item, catalog(item.type));
    const x = Number.isFinite(Number(wire.x)) ? Number(wire.x) : 0;
    const y = Number.isFinite(Number(wire.y)) ? Number(wire.y) : 0;
    const size = data.sizes?.[item.id];
    const { w, h } = size ? { w: size[0], h: size[1] } : estimate(wire);
    nodes.push({ ...wire, id, x, y });
    rects.push({ x, y, w, h });
    sizes[id] = [Math.round(w), Math.round(h)];
  }
  const offset = nearestFreeOffset(rects, obstacles);
  for (const node of nodes) {
    node.x = Math.round((node.x + offset.x) * 100) / 100;
    node.y = Math.round((node.y + offset.y) * 100) / 100;
  }
  const connections = data.connections
    .filter((c) => ids.has(c.parent) && ids.has(c.child))
    .map((c) => ({ parent: ids.get(c.parent)!, child: ids.get(c.child)! }));
  return { nodes, connections, sizes };
}
