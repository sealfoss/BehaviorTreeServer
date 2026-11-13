/**
 * The latest log lines, as a ring (design 11.2 `log`). Lines are numbered per run from 0
 * (contract 5.6); older lines are fetched with `ask log_before` and put in front.
 */

import type { LogLine } from '../types';

export interface LogRow {
  seq: number;
  time: string;
  level: number;
  text: string;
}

export const LOG_CAPACITY = 5000;

export class LogStore {
  rows = $state<LogRow[]>([]);
  /** Lines the gateway dropped before the current ones (a slow tab), not yet fetched. */
  gapBefore = $state(0);

  constructor(private readonly capacity = LOG_CAPACITY) {}

  get firstSeq(): number {
    return this.rows.length ? this.rows[0].seq : 0;
  }

  replace(first: number, lines: LogLine[]): void {
    this.rows = toRows(first, lines).slice(-this.capacity);
    this.gapBefore = 0;
  }

  clear(): void {
    this.rows = [];
    this.gapBefore = 0;
  }

  /** A `log` batch. `skipped` lines were dropped just before it. */
  append(first: number, lines: LogLine[], skipped = 0): void {
    const rows = toRows(first, lines);
    const last = this.rows.length ? this.rows[this.rows.length - 1].seq : -1;
    if (first === 0 && last >= 0 && rows.length) {
      // Numbers start at 0 again: a new run.
      this.replace(first, lines);
      return;
    }
    const fresh = rows.filter((row) => row.seq > last);
    if (!fresh.length) return;
    if (skipped > 0 || (last >= 0 && fresh[0].seq > last + 1)) {
      // Lines are missing in between: keep only the newest stretch, older ones can be fetched.
      this.rows = fresh.slice(-this.capacity);
      this.gapBefore = fresh[0].seq;
      return;
    }
    const combined = this.rows.concat(fresh);
    this.rows = combined.length > this.capacity ? combined.slice(combined.length - this.capacity) : combined;
  }

  /** Older lines (`ask log_before`), put in front of the ring. */
  prepend(first: number, lines: LogLine[]): void {
    const rows = toRows(first, lines).filter((row) => row.seq < this.firstSeq || !this.rows.length);
    this.rows = rows.concat(this.rows).slice(0, this.capacity);
    if (this.gapBefore && this.rows.length && this.rows[0].seq <= 0) this.gapBefore = 0;
  }
}

function toRows(first: number, lines: LogLine[]): LogRow[] {
  return lines.map(([time, level, text], index) => ({ seq: first + index, time, level, text }));
}
