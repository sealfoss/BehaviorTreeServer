/** Blackboard entries in order (design 11.2): value, type, Resets box, source, age. */

import type { Entry } from '../types';

export interface BbChanges {
  entries?: Entry[];
  removed?: string[];
  order?: string[] | null;
}

export class BlackboardStore {
  entries = $state<Entry[]>([]);

  replace(entries: Entry[]): void {
    this.entries = structuredClone(entries);
  }

  byId(id: string): Entry | undefined {
    return this.entries.find((entry) => entry.id === id);
  }

  byName(name: string | null | undefined): Entry | undefined {
    return name ? this.entries.find((entry) => entry.name === name) : undefined;
  }

  /** A `bb` message: changed entries (new ones at the end), removed ids, then the new order. */
  apply(changes: BbChanges): void {
    const removed = new Set(changes.removed ?? []);
    const list = this.entries.filter((entry) => !removed.has(entry.id));
    for (const entry of changes.entries ?? []) {
      const index = list.findIndex((item) => item.id === entry.id);
      if (index >= 0) list[index] = structuredClone(entry);
      else list.push(structuredClone(entry));
    }
    if (changes.order) {
      const rank = new Map(changes.order.map((id, index) => [id, index]));
      list.sort((a, b) => (rank.get(a.id) ?? Infinity) - (rank.get(b.id) ?? Infinity));
    }
    this.entries = list;
  }
}
