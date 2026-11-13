/**
 * Findings of validation (contract 5.1): what a finding's path names, a text for the list
 * (the node's title and tree, not its id), and the way from the session's tree to it.
 */

import { findNode } from './ops';
import { resolveSubtree } from './paths';
import type { Finding, TreeDocument } from './types';

export type FindingTarget =
  | { kind: 'tree'; key: string; id: string }
  | { kind: 'instance'; ids: string[] }
  | { kind: 'blackboard'; name: string }
  | { kind: 'other'; text: string };

/** `tree:<key>/<id>`, `blackboard:<name>` or an instance path `a/b/c`. */
export function findingTarget(path: string): FindingTarget {
  if (path.startsWith('blackboard:')) return { kind: 'blackboard', name: path.slice('blackboard:'.length) };
  if (path.startsWith('tree:')) {
    const rest = path.slice('tree:'.length);
    const slash = rest.lastIndexOf('/');
    return slash < 0 ? { kind: 'other', text: rest } : { kind: 'tree', key: rest.slice(0, slash), id: rest.slice(slash + 1) };
  }
  const ids = path.split('/').filter(Boolean);
  return ids.length && ids.every((id) => /^[0-9a-f]{32}$/i.test(id)) ? { kind: 'instance', ids } : { kind: 'other', text: path };
}

/**
 * Sub-Tree node ids leading from `rootKey` to the document `targetKey`, through documents that
 * are open (`docs`); [] for the root itself, null when no way is known.
 */
export function routeTo(docs: Record<string, TreeDocument>, rootKey: string, targetKey: string): string[] | null {
  if (rootKey === targetKey) return [];
  const queue: [string, string[]][] = [[rootKey, []]];
  const seen = new Set([rootKey]);
  while (queue.length) {
    const [key, route] = queue.shift()!;
    const doc = docs[key];
    if (!doc) continue;
    for (const node of doc.nodes) {
      if (node.type !== 'Sub-Tree') continue;
      const file = typeof node.fields?.File === 'string' ? node.fields.File : '';
      const next = resolveSubtree(key, file);
      if (!next || seen.has(next)) continue;
      if (next === targetKey) return [...route, node.id];
      seen.add(next);
      queue.push([next, [...route, node.id]]);
    }
  }
  return null;
}

/** The node an instance path ends at, followed through the open documents from the root. */
function instanceNode(docs: Record<string, TreeDocument>, rootKey: string, ids: string[]): { key: string; title: string } | null {
  let key = rootKey;
  for (let index = 0; index < ids.length; index++) {
    const doc = docs[key];
    const node = doc ? findNode(doc, ids[index]) : undefined;
    if (!doc || !node) return null;
    if (index === ids.length - 1) return { key, title: node.title };
    const next = resolveSubtree(key, typeof node.fields?.File === 'string' ? node.fields.File : '');
    if (!next) return null;
    key = next;
  }
  return null;
}

/** "tree › title" for the list of findings; the raw path when the node is not known here. */
export function findingLabel(finding: Finding & { executor?: string }, docs: Record<string, TreeDocument>, rootKey: string | null): string {
  const target = findingTarget(finding.path);
  const on = finding.executor ? ` on ${finding.executor}` : '';
  if (target.kind === 'blackboard') return `blackboard entry ${target.name}`;
  if (target.kind === 'tree') {
    const node = docs[target.key] ? findNode(docs[target.key], target.id) : undefined;
    return `${target.key} › ${node ? node.title : `node ${target.id.slice(0, 8)}…`}${on}`;
  }
  if (target.kind === 'instance' && rootKey) {
    const found = instanceNode(docs, rootKey, target.ids);
    if (found) return `${found.key} › ${found.title}${on}`;
    return `node ${target.ids[target.ids.length - 1].slice(0, 8)}…${on}`;
  }
  return `${finding.path}${on}`;
}
