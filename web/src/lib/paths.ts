/**
 * Tree names and Sub-Tree file names (contract 2.1, design 9.4): a tree is named by its key,
 * its path relative to `trees_dir` without `_BT.json`; a Sub-Tree's `File` is relative to the
 * folder of the tree that holds it, `\` read as `/`.
 */

/** The key of a tree from any of the widget's four spellings: `robot`, `robot.json`, `robot_BT.json`, `robot_BB.json`. */
export function treeKeyOf(name: string): string {
  return name.trim().replace(/\\/g, '/').replace(/(_BT\.json|_BB\.json|\.json)$/i, '');
}

function folderOf(key: string): string {
  const index = key.lastIndexOf('/');
  return index < 0 ? '' : key.slice(0, index);
}

function normalise(parts: string[]): string[] | null {
  const result: string[] = [];
  for (const part of parts) {
    if (part === '' || part === '.') continue;
    if (part === '..') {
      if (!result.length) return null; // outside the trees folder
      result.pop();
    } else {
      result.push(part);
    }
  }
  return result;
}

/** The key of the tree a Sub-Tree node of `docKey` runs, or null (no file, absolute, outside). */
export function resolveSubtree(docKey: string, file: string): string | null {
  const name = file.trim().replace(/\\/g, '/');
  if (!name || name.startsWith('/') || /^[A-Za-z]:/.test(name)) return null;
  const parts = normalise([...folderOf(docKey).split('/'), ...name.split('/')]);
  if (!parts || !parts.length) return null;
  return treeKeyOf(parts.join('/'));
}

/** The `File` of a Sub-Tree node of `docKey` that runs `targetKey`: relative to its folder. */
export function relativeFile(docKey: string, targetKey: string): string {
  const from = folderOf(docKey).split('/').filter(Boolean);
  const to = `${treeKeyOf(targetKey)}_BT.json`.split('/');
  let common = 0;
  while (common < from.length && common < to.length - 1 && from[common] === to[common]) common++;
  return [...Array(from.length - common).fill('..'), ...to.slice(common)].join('/');
}
