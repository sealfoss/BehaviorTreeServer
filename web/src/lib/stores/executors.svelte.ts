/** The executors found, and the one this tab is attached to (design 11.2 `executors`). */

import type { ExecutorInfo } from '../types';

export class ExecutorsStore {
  list = $state<ExecutorInfo[]>([]);
  attached = $state<string | null>(null);
  /** The gateway's id for this tab (`by` of its own op broadcasts). */
  client = $state<string | null>(null);
  online = $state(false);
  /** Whether the attached executor is up (its tree can be edited while it is down). */
  executorOnline = $state(true);
  /** Browsers attached to the session (presence). */
  attachedCount = $state(0);
  /** Server time minus local time, seconds. */
  clockOffset = $state(0);
  local = $state(false);

  setList(list: ExecutorInfo[]): void {
    this.list = list;
  }
}
