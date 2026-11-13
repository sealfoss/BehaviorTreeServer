/** Node types and their fields, from the executor (design 11.2 `catalog`). */

import type { CatalogEntry } from '../types';

export class CatalogStore {
  types = $state<CatalogEntry[]>([]);

  replace(types: CatalogEntry[]): void {
    this.types = structuredClone(types);
  }

  get(type: string): CatalogEntry | undefined {
    return this.types.find((entry) => entry.type === type);
  }

  /** The Add Node menu: categories in catalog order, types in catalog order (custom ones by title). */
  menu(): { category: string; types: CatalogEntry[] }[] {
    const groups = new Map<string, CatalogEntry[]>();
    for (const entry of this.types) {
      if (entry.kind === 'root') continue;
      const list = groups.get(entry.category) ?? [];
      list.push(entry);
      groups.set(entry.category, list);
    }
    const result = [...groups].map(([category, types]) => ({
      category,
      types: types.some((t) => !t.builtin) ? [...types].sort((a, b) => a.title.localeCompare(b.title)) : types,
    }));
    // Notes and Groups last, as in the widget's menu.
    result.sort((a, b) => Number(a.category === 'Annotation') - Number(b.category === 'Annotation'));
    return result;
  }
}
