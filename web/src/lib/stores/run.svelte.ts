/**
 * Runs (design 11.2 `run`): state, loop count and counts per executor, status per instance
 * path, details per instance path. Statuses arrive as one digit per node path (`st.v`) and are
 * applied once per animation frame (design 11.3).
 */

import type { Bundle, Details, RunMessage, StBody } from '../types';

export interface RunInfo {
  exec: string;
  runId: string | null;
  state: number;
  loop: number;
  cancelling: number;
  unsettled: number;
  outcome: number;
  message: string;
  nodePaths: string[];
  own: boolean;
  label: string;
  requestedBy: string;
  bundle: Bundle | null;
  active: boolean;
}

function emptyRun(exec: string): RunInfo {
  return {
    exec, runId: null, state: 0, loop: 0, cancelling: 0, unsettled: 0, outcome: 0, message: '',
    nodePaths: [], own: true, label: '', requestedBy: '', bundle: null, active: false,
  };
}

export type FrameScheduler = (fn: () => void) => void;

export class RunStore {
  runs = $state<Record<string, RunInfo>>({});
  /** exec -> instance path -> status digit (0 Ready ... 4 Cancelling). */
  statuses = $state<Record<string, Record<string, number>>>({});
  /** exec -> instance path -> details by kind. */
  details = $state<Record<string, Record<string, Details>>>({});
  /** The session is between the click on Execute and the goal's acceptance (design 9.5). */
  starting = $state(false);

  private pendingV = new Map<string, string>();
  private frameRequested = false;

  constructor(private readonly schedule: FrameScheduler = (fn) => requestAnimationFrame(() => fn())) {}

  run(exec: string | null): RunInfo {
    return (exec && this.runs[exec]) || emptyRun(exec ?? '');
  }

  private ensure(exec: string): RunInfo {
    this.runs[exec] ??= emptyRun(exec);
    this.statuses[exec] ??= {};
    this.details[exec] ??= {};
    return this.runs[exec];
  }

  status(exec: string | null, path: string): number {
    return (exec && this.statuses[exec]?.[path]) || 0;
  }

  detailsOf(exec: string | null, path: string): Details | undefined {
    return exec ? this.details[exec]?.[path] : undefined;
  }

  /** Forget an executor's run (another executor attached). */
  clear(exec: string): void {
    delete this.runs[exec];
    delete this.statuses[exec];
    delete this.details[exec];
    this.pendingV.delete(exec);
  }

  /** A `run` message: a run started (the table of node paths) or ended (the outcome). */
  applyRun(message: Omit<RunMessage, 't'>): void {
    const run = this.ensure(message.exec);
    if (message.event === 'started') {
      run.runId = message.run_id;
      run.nodePaths = [...(message.node_paths ?? [])];
      run.own = message.own;
      run.label = message.label ?? '';
      run.requestedBy = message.requested_by ?? '';
      run.bundle = message.bundle ?? null;
      run.active = true;
      run.outcome = 0;
      run.message = '';
      this.details[message.exec] = {};
      this.starting = false;
    } else {
      run.active = false;
      run.outcome = message.outcome ?? run.outcome;
      run.message = message.message ?? run.message;
      if (message.node_paths?.length) run.nodePaths = [...message.node_paths];
    }
  }

  /** An `st` message: state and counts at once, the status digits at the next frame. */
  applySt(exec: string, st: StBody): void {
    const run = this.ensure(exec);
    run.state = st.state;
    run.loop = st.loop;
    run.cancelling = st.cancelling;
    run.unsettled = st.unsettled;
    run.outcome = st.outcome;
    run.message = st.message ?? '';
    if (st.state !== 0) run.active = true;
    this.pendingV.set(exec, st.v ?? '');
    if (!this.frameRequested) {
      this.frameRequested = true;
      this.schedule(() => this.flush());
    }
  }

  /** Apply the latest status digits of every executor (once per animation frame). */
  flush(): void {
    this.frameRequested = false;
    for (const [exec, v] of this.pendingV) {
      const paths = this.runs[exec]?.nodePaths ?? [];
      const current = (this.statuses[exec] ??= {});
      const seen = new Set<string>();
      for (let index = 0; index < paths.length; index++) {
        const digit = index < v.length ? v.charCodeAt(index) - 48 : 0;
        const path = paths[index];
        seen.add(path);
        if ((current[path] ?? 0) !== digit) current[path] = digit;
      }
      for (const path of Object.keys(current)) if (!seen.has(path)) delete current[path];
    }
    this.pendingV.clear();
  }

  /** An `nd` message: details by node index; a null value clears that kind. */
  applyNd(exec: string, details: Record<string, Record<string, unknown>>): void {
    const run = this.ensure(exec);
    const byPath = (this.details[exec] ??= {});
    for (const [index, kinds] of Object.entries(details)) {
      const path = run.nodePaths[Number(index)];
      if (path === undefined) continue;
      const entry: Details = { ...(byPath[path] ?? {}) };
      for (const [kind, value] of Object.entries(kinds ?? {})) {
        if (value === null) delete entry[kind];
        else entry[kind] = value;
      }
      if (Object.keys(entry).length) byPath[path] = entry;
      else delete byPath[path];
    }
  }
}
