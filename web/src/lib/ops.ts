/**
 * Edit operations of contract 5.3 applied to a document in memory.
 *
 * The page applies every `op` the gateway sends, in order, its own included (design 9.3), and
 * applies its own at once when it sends them. So each operation here is idempotent: applying
 * it twice leaves the same document as applying it once.
 */

import type { Connection, NodeId, TreeDocument, WireNode } from './types';
import { parseDoubleText, parsePyLiteral, pyRepr, splitFieldPath, type PyValue } from './wire';

export type Op = { op: string; [key: string]: unknown };

/** What applying an op did: `resync` when the page cannot follow it and needs the document again. */
export type ApplyResult = 'applied' | 'ignored' | 'resync';

export function findNode(doc: TreeDocument, id: NodeId): WireNode | undefined {
  return doc.nodes.find((node) => node.id === id);
}

/** Ids of a parent's children in execution order (the order of its connections). */
export function childIds(doc: TreeDocument, parent: NodeId): NodeId[] {
  return doc.connections.filter((c) => c.parent === parent).map((c) => c.child);
}

export function parentId(doc: TreeDocument, child: NodeId): NodeId | undefined {
  return doc.connections.find((c) => c.child === child)?.parent;
}

/** Put a parent's connections into the order of `ids`, leaving every other connection in place. */
export function setChildOrder(doc: TreeDocument, parent: NodeId, ids: NodeId[]): void {
  const slots: number[] = [];
  const mine: Connection[] = [];
  doc.connections.forEach((c, index) => {
    if (c.parent === parent) {
      slots.push(index);
      mine.push(c);
    }
  });
  const rank = new Map(ids.map((id, index) => [id, index]));
  const ordered = mine
    .map((c, index) => ({ c, index }))
    .sort((a, b) => (rank.get(a.c.child) ?? ids.length + a.index) - (rank.get(b.c.child) ?? ids.length + b.index))
    .map((item) => item.c);
  const sameOrder = ordered.every((c, index) => c === mine[index]);
  if (sameOrder) return;
  slots.forEach((slot, index) => {
    doc.connections[slot] = ordered[index];
  });
}

function applyOrders(doc: TreeDocument, orders: unknown): void {
  if (!orders || typeof orders !== 'object') return;
  for (const [parent, ids] of Object.entries(orders as Record<string, unknown>)) {
    if (Array.isArray(ids)) setChildOrder(doc, parent, ids.map(String));
  }
}

function upsertNode(doc: TreeDocument, node: WireNode): void {
  const index = doc.nodes.findIndex((item) => item.id === node.id);
  const copy = structuredClone(node);
  if (index >= 0) doc.nodes[index] = copy;
  else doc.nodes.push(copy);
}

function addConnection(doc: TreeDocument, parent: NodeId, child: NodeId): void {
  if (doc.connections.some((c) => c.parent === parent && c.child === child)) return;
  // A node has one parent; a Negation has one child (design 9.3).
  let connections = doc.connections.filter((c) => c.child !== child);
  if (findNode(doc, parent)?.type === 'Negation') connections = connections.filter((c) => c.parent !== parent);
  connections.push({ parent, child });
  doc.connections.splice(0, doc.connections.length, ...connections);
}

const COMPOSITES = new Set(['Sequence', 'Selector', 'Parallel']);

/** Apply one operation (contract 5.3) to `doc`. Unknown nodes are skipped, never invented. */
export function applyOp(doc: TreeDocument, op: Op): ApplyResult {
  const id = op.id as NodeId | undefined;
  const node = id !== undefined ? findNode(doc, id) : undefined;
  switch (op.op) {
    case 'node.add': {
      upsertNode(doc, op.node as WireNode);
      applyOrders(doc, op.order);
      return 'applied';
    }
    case 'node.remove': {
      const ids = new Set((op.ids as NodeId[]) ?? []);
      const kept = doc.nodes.filter((n) => !ids.has(n.id));
      doc.nodes.splice(0, doc.nodes.length, ...kept);
      const connections = doc.connections.filter((c) => !ids.has(c.parent) && !ids.has(c.child));
      doc.connections.splice(0, doc.connections.length, ...connections);
      return 'applied';
    }
    case 'node.move': {
      const positions = (op.positions ?? {}) as Record<string, [number, number]>;
      for (const [nodeId, pos] of Object.entries(positions)) {
        const target = findNode(doc, nodeId);
        if (target && Array.isArray(pos)) {
          target.x = Number(pos[0]);
          target.y = Number(pos[1]);
        }
      }
      applyOrders(doc, op.orders);
      return 'applied';
    }
    case 'node.set_title':
      if (!node) return 'ignored';
      node.title = String(op.title);
      return 'applied';
    case 'node.set_field': {
      if (!node) return 'ignored';
      const field = String(op.field);
      node.fields ??= {};
      if (typeof op.path === 'string' && op.path) {
        const updated = setInLiteral(node.fields[field], op.path, op.value, op.kind as string | undefined);
        if (updated === null) return 'resync';
        node.fields[field] = updated;
      } else {
        node.fields[field] = structuredClone(op.value);
      }
      return 'applied';
    }
    case 'node.set_selection':
      if (!node) return 'ignored';
      node.field_selections ??= {};
      node.field_selections[String(op.field)] = Number(op.index);
      return 'applied';
    case 'node.set_binding':
    case 'node.set_output': {
      if (!node) return 'ignored';
      const name = op.op === 'node.set_binding' ? 'bindings' : 'outputs';
      const map = { ...(node[name] ?? {}) } as Record<string, string>;
      if (op.entry === null || op.entry === undefined) delete map[String(op.key)];
      else map[String(op.key)] = String(op.entry);
      if (Object.keys(map).length) node[name] = map;
      else delete node[name];
      return 'applied';
    }
    case 'node.set_option': {
      if (!node) return 'ignored';
      const name = String(op.name);
      if (op.value === null || op.value === undefined) delete node[name];
      else node[name] = op.value;
      return 'applied';
    }
    case 'node.resize':
      if (!node) return 'ignored';
      node.width = Number(op.width);
      node.height = Number(op.height);
      if (typeof op.x === 'number') node.x = op.x;
      if (typeof op.y === 'number') node.y = op.y;
      return 'applied';
    case 'node.set_text':
      if (!node) return 'ignored';
      node.text = String(op.text);
      return 'applied';
    case 'node.set_color':
      if (!node) return 'ignored';
      node.color = String(op.color);
      return 'applied';
    case 'composite.set_type': {
      if (!node) return 'ignored';
      const composite = String(op.composite);
      const old = node.type === 'Root' ? (node.composite ?? 'Sequence') : node.type;
      if (node.type === 'Root') node.composite = composite;
      else if (COMPOSITES.has(node.type)) node.type = composite;
      else return 'ignored';
      if (node.title === old && node.type !== 'Root') node.title = composite;
      if (composite === 'Parallel') {
        delete node.memory;
        node.policy ??= 'all';
      } else {
        delete node.policy;
        node.memory ??= true;
      }
      return 'applied';
    }
    case 'composite.set_memory':
      if (!node) return 'ignored';
      node.memory = Boolean(op.memory);
      return 'applied';
    case 'composite.set_policy':
      if (!node) return 'ignored';
      node.policy = op.policy === 'any' ? 'any' : 'all';
      return 'applied';
    case 'conn.add': {
      const parent = String(op.parent);
      const child = String(op.child);
      if (!findNode(doc, parent) || !findNode(doc, child)) return 'ignored';
      addConnection(doc, parent, child);
      if (Array.isArray(op.order)) setChildOrder(doc, parent, op.order.map(String));
      return 'applied';
    }
    case 'conn.remove': {
      const connections = doc.connections.filter((c) => !(c.parent === op.parent && c.child === op.child));
      doc.connections.splice(0, doc.connections.length, ...connections);
      return 'applied';
    }
    case 'paste': {
      for (const pasted of (op.nodes as WireNode[]) ?? []) upsertNode(doc, pasted);
      for (const c of (op.connections as Connection[]) ?? []) {
        if (findNode(doc, c.parent) && findNode(doc, c.child)) addConnection(doc, c.parent, c.child);
      }
      return 'applied';
    }
    case 'doc.set_config':
      Object.assign(doc.config, (op.config ?? {}) as object);
      return 'applied';
    default:
      return 'resync';
  }
}

/** True for operations that change the structure (refused while the tree executes, design 9.3). */
export function isStructural(op: Op): boolean {
  return ['node.add', 'node.remove', 'conn.add', 'conn.remove', 'paste', 'group.promote', 'composite.set_type', 'composite.set_memory', 'composite.set_policy'].includes(op.op);
}

/** A wire value of a field kind as a parsed Python value. */
export function wireToPy(value: unknown, kind?: string): PyValue {
  if (value === null || value === undefined) return { t: 'none' };
  if (typeof value === 'boolean') return { t: 'bool', v: value };
  if (kind === 'float') return { t: 'float', v: typeof value === 'number' ? value : parseDoubleText(String(value)) };
  if (typeof value === 'number') return Number.isInteger(value) && kind !== 'float' ? { t: 'int', v: String(value) } : { t: 'float', v: value };
  if (kind === 'int') return { t: 'int', v: String(BigInt(String(value).trim())) };
  if (kind === 'str') return { t: 'str', v: String(value) };
  if (kind === 'other') return parsePyLiteral(String(value));
  return { t: 'str', v: String(value) };
}

/**
 * The Python literal text of a dictionary field with the value at `path` replaced
 * (`node.set_field` with `path`). Returns null when the text cannot be read here.
 */
export function setInLiteral(literal: unknown, path: string, value: unknown, kind?: string): string | null {
  let root: PyValue;
  try {
    root = literal === null || literal === undefined || literal === '' ? { t: 'dict', items: [] } : parsePyLiteral(String(literal));
  } catch {
    return null;
  }
  let replacement: PyValue;
  try {
    replacement = wireToPy(value, kind);
  } catch {
    return null;
  }
  const parts = splitFieldPath(path);
  const set = (current: PyValue, index: number): PyValue | null => {
    if (index === parts.length) return replacement;
    const part = parts[index];
    if (typeof part === 'number') {
      if (current.t !== 'list' && current.t !== 'tuple') return null;
      if (part >= current.items.length) return null;
      const inner = set(current.items[part], index + 1);
      if (!inner) return null;
      const items = [...current.items];
      items[part] = inner;
      return { ...current, items };
    }
    const dict: PyValue = current.t === 'dict' ? current : { t: 'dict', items: [] };
    if (dict.t !== 'dict') return null;
    const at = dict.items.findIndex(([k]) => k.t === 'str' && k.v === part);
    const inner = set(at >= 0 ? dict.items[at][1] : { t: 'dict', items: [] }, index + 1);
    if (!inner) return null;
    const items = [...dict.items];
    if (at >= 0) items[at] = [items[at][0], inner];
    else items.push([{ t: 'str', v: part }, inner]);
    return { t: 'dict', items };
  };
  const result = set(root, 0);
  return result ? pyRepr(result) : null;
}

