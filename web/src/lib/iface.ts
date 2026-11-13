/**
 * Descriptions of ROS interfaces from `ask interface` (contract 5.8 item 4): sections such as
 * `goal`, `result`, `feedback`, `request`, `response`, `message`, each a list of fields
 * `{name, kind, fields?}` (or `{fields: [...]}`). The inspector builds its form from them, and
 * the Source dialog offers their field paths.
 */

export interface IfaceField {
  name: string;
  kind: string;
  fields?: IfaceField[];
  default?: unknown;
  constants?: { name: string; value: unknown }[];
}

export type RowKind = 'bool' | 'int' | 'float' | 'str' | 'msg' | 'array';

export interface IfaceRow {
  /** The path inside the section: `pose.position.x`. */
  path: string;
  depth: number;
  kind: RowKind;
}

const SECTIONS = ['Goal', 'Result', 'Feedback', 'Request', 'Response', 'Message'];

function asFields(value: unknown): IfaceField[] | null {
  if (Array.isArray(value)) return value as IfaceField[];
  if (value && typeof value === 'object' && Array.isArray((value as { fields?: unknown }).fields)) return (value as { fields: IfaceField[] }).fields;
  return null;
}

/** The sections of a description by their capitalised names; a bare list is `fallback`'s. */
export function interfaceSections(data: unknown, fallback = 'Message'): Record<string, IfaceField[]> {
  const result: Record<string, IfaceField[]> = {};
  const direct = asFields(data);
  if (direct) {
    result[fallback] = direct;
    return result;
  }
  if (data && typeof data === 'object') {
    for (const [key, value] of Object.entries(data as Record<string, unknown>)) {
      const name = SECTIONS.find((section) => section.toLowerCase() === key.toLowerCase());
      const fields = asFields(value);
      if (name && fields) result[name] = fields;
    }
  }
  return result;
}

/** The editor kind of a field from its ROS type text (`float64`, `uint8`, `sequence<int32>`, ...). */
export function rowKind(field: IfaceField): RowKind {
  const kind = String(field.kind ?? '').toLowerCase();
  if (/(\[|sequence<|array)/.test(kind)) return 'array';
  if (field.fields?.length) return 'msg';
  if (kind.startsWith('bool')) return 'bool';
  if (/^(u?int|integer|byte|char|octet)/.test(kind)) return 'int';
  if (/^(float|double)/.test(kind)) return 'float';
  if (/string/.test(kind)) return 'str';
  return kind.includes('/') || kind === 'message' || kind === 'msg' ? 'msg' : 'str';
}

/** Every field in order, nested messages followed by their fields. */
export function interfaceRows(fields: IfaceField[] | undefined, prefix = '', depth = 0): IfaceRow[] {
  const rows: IfaceRow[] = [];
  for (const field of fields ?? []) {
    const path = prefix ? `${prefix}.${field.name}` : field.name;
    const kind = rowKind(field);
    rows.push({ path, depth, kind });
    if (kind === 'msg') rows.push(...interfaceRows(field.fields, path, depth + 1));
  }
  return rows;
}

/** The entry type a source field fills (design 7.4): bool, numbers, text, arrays, messages. */
export function entryTypeOf(kind: RowKind): string {
  return { bool: 'Bool', int: 'Integer', float: 'Double', str: 'String', msg: 'Dictionary', array: 'List' }[kind];
}
