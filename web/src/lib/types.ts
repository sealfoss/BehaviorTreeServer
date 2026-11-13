/** Data shapes of the browser protocol (contract 5.1). */

import type { Wire } from './wire';

export type NodeId = string;

/** A node in wire form: as in the tree file, field values in wire form by their declared kind. */
export interface WireNode {
  id: NodeId;
  type: string;
  title: string;
  x: number;
  y: number;
  fields?: Record<string, unknown>;
  field_selections?: Record<string, number>;
  composite?: string; // Root: "Sequence" | "Selector"
  memory?: boolean;
  policy?: 'all' | 'any';
  text?: string; // Notes
  width?: number; // Notes and Groups
  height?: number;
  color?: string; // Groups
  bindings?: Record<string, string>;
  outputs?: Record<string, string>;
  wait_for_cancels?: boolean;
  cancel_timeout_ms?: number;
  unknown?: boolean;
  [key: string]: unknown;
}

export interface Connection {
  parent: NodeId;
  child: NodeId;
}

export interface TreeConfig {
  tick_interval_ms?: number;
  repeat?: boolean;
  restore_blackboard?: boolean;
  default_memory?: boolean;
  log_level?: number;
  wait_for_cancels?: boolean;
  cancel_timeout_ms?: number;
  on_unconfirmed_cancel?: 'stop' | 'continue';
  [key: string]: unknown;
}

export interface DocumentView {
  zoom: number;
  center: [number, number];
}

/** A tree file in memory (contract 5.1). `connections` are in execution order per parent. */
export interface TreeDocument {
  key: string;
  path: string;
  rev: number;
  version: number;
  unsaved: boolean;
  config: TreeConfig;
  view: DocumentView;
  nodes: WireNode[];
  connections: Connection[];
  problems: string[];
}

export interface EntrySource {
  topic: string;
  type: string;
  field: string;
  max_rate_hz: number;
  max_age_s: number;
}

export interface Entry {
  id: string;
  name: string;
  type: string;
  value: Wire;
  reset: boolean;
  source: EntrySource | null;
  freshness: 'ok' | 'nodata' | 'stale' | null;
  age: number | null;
  truncated: boolean;
  /** Why the entry's source cannot feed it (QoS, type, field), or null. */
  problem?: string | null;
}

export interface CatalogField {
  name: string;
  kind: 'int' | 'float' | 'str' | 'bool' | 'choice' | 'entry' | 'other';
  default: unknown;
  doc: string;
}

export interface CatalogEntry {
  type: string;
  title: string;
  kind: 'root' | 'composite' | 'decorator' | 'leaf' | 'note' | 'group';
  category: string;
  fields: CatalogField[];
  leaf_kind: string | null;
  special: string | null;
  waits_for_cancels: boolean;
  needs_ros: boolean;
  builtin: boolean;
  doc: string;
  [key: string]: unknown;
}

export interface Finding {
  path: string;
  severity: 'error' | 'warning';
  text: string;
}

export interface ExecutorInfo {
  name: string;
  online: boolean;
  state: number;
}

export type LogLine = [string, number, string];

/** The `st` body: run state and every node's status as one digit per node path. */
export interface StBody {
  run_id: string | null;
  state: number;
  loop: number;
  cancelling: number;
  unsettled: number;
  outcome: number;
  message: string;
  v: string;
  /** The session is between the click on Execute and the goal's acceptance (design 9.5). */
  starting?: boolean;
}

export interface RunMessage {
  t: 'run';
  exec: string;
  event: 'started' | 'ended';
  run_id: string;
  label: string;
  requested_by: string;
  node_paths: string[];
  own: boolean;
  bundle: Bundle | null;
  outcome?: number;
  message?: string;
}

/** A run's documents (design 4.3); trees in file form. */
export interface Bundle {
  format: string;
  version: number;
  root: string;
  trees: Record<string, { config?: TreeConfig; nodes: WireNode[]; connections: Connection[] }>;
  blackboards?: Record<string, unknown[]>;
}

export type Details = Record<string, unknown>;

export interface Snapshot {
  t: 'snapshot';
  exec: string;
  client: string;
  root: string | null;
  docs: Record<string, TreeDocument>;
  blackboard: Entry[];
  catalog: CatalogEntry[];
  online: boolean;
  starting: boolean;
  run: Omit<RunMessage, 't'> | null;
  st: StBody | null;
  details: Record<string, Details>;
  log: { first: number; lines: LogLine[] };
  recovery: string[];
  attached: number;
  findings: Finding[];
}

/** Questions of contract 5.4 that a `no` may carry. */
export type FileQuestion =
  | { kind: 'unsaved'; docs: string[] }
  | { kind: 'replace'; name: string }
  | { kind: 'choose_file'; node: string; file: string };

/** Run states, statuses and outcomes (contract 2.2). */
export const STATES = ['IDLE', 'RUNNING', 'PAUSED', 'STOPPING', 'RESTARTING'] as const;
export const STATUS_NAMES = ['Ready', 'Running', 'Succeeded', 'Failed', 'Cancelling'] as const;
export const OUTCOMES = ['', 'succeeded', 'failed', 'stopped', 'error'] as const;
export const LOG_LEVELS = ['0: None', '1: ERROR', '2: WARNING', '3: DEBUG', '4: INFO'] as const;
export const ENTRY_TYPES = ['Integer', 'Double', 'String', 'Bool', 'List', 'Dictionary', 'Set'] as const;
export const CONTAINER_TYPES = new Set(['List', 'Dictionary', 'Set']);
