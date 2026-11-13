/**
 * Values on the wire (design 10.3, contract 5.1): Doubles travel as their Python text
 * ("1.0", "nan", "inf"), containers as Python literal text, whole numbers beyond 2**53 as text.
 * The page shows and edits that text and never rebuilds a container value itself.
 *
 * This module also reads Python literal text (for display: the items of a List entry, the
 * nested values of a ROS goal; for edits inside a dictionary field) and writes it back.
 */

/** The browser form of a value of a field kind or an entry type. */
export type Wire = null | boolean | number | string;

/**
 * A Double typed as text: a number with `.` or `,` as the decimal point, or `nan`, `inf`,
 * `-inf` (also `infinity`), in any case (design 11.4, values.parse_double_text).
 * @throws Error when the text is not a number.
 */
export function parseDoubleText(text: string): number {
  const cleaned = text.trim().replace(/,/g, '.');
  const lowered = cleaned.toLowerCase();
  if (lowered === 'nan' || lowered === '+nan' || lowered === '-nan') return NaN;
  if (lowered === 'inf' || lowered === '+inf' || lowered === 'infinity' || lowered === '+infinity') return Infinity;
  if (lowered === '-inf' || lowered === '-infinity') return -Infinity;
  if (!cleaned || cleaned.split('.').length > 2 || !/^[+-]?(\d+\.?\d*|\.\d+)(e[+-]?\d+)?$/i.test(cleaned)) {
    throw new Error(`'${text}' is not a number`);
  }
  const value = Number(cleaned);
  if (Number.isNaN(value)) throw new Error(`'${text}' is not a number`);
  return value;
}

/** Python's `repr(float(value))`: "1.0", "0.1", "1e+16", "1.5e-05", "nan", "inf", "-inf". */
export function doubleToWire(value: number): string {
  if (Number.isNaN(value)) return 'nan';
  if (value === Infinity) return 'inf';
  if (value === -Infinity) return '-inf';
  if (value === 0) return Object.is(value, -0) ? '-0.0' : '0.0';
  // toExponential() without digits gives the shortest digits that read back as the same value.
  const [mantissa, exponentText] = value.toExponential().split('e');
  const exponent = Number(exponentText);
  const negative = mantissa.startsWith('-');
  const digits = mantissa.replace('-', '').replace('.', '');
  const sign = negative ? '-' : '';
  if (exponent >= -5 + 1 && exponent < 16) {
    // Fixed notation (Python uses it for -4 <= exponent < 16).
    let text: string;
    if (exponent >= 0) {
      const whole = digits.length > exponent + 1 ? digits.slice(0, exponent + 1) : digits.padEnd(exponent + 1, '0');
      const fraction = digits.length > exponent + 1 ? digits.slice(exponent + 1) : '0';
      text = `${whole}.${fraction}`;
    } else {
      text = `0.${'0'.repeat(-exponent - 1)}${digits}`;
    }
    return sign + text;
  }
  const fraction = digits.length > 1 ? `.${digits.slice(1)}` : '';
  const expSign = exponent < 0 ? '-' : '+';
  const expDigits = String(Math.abs(exponent)).padStart(2, '0');
  return `${sign}${digits[0]}${fraction}e${expSign}${expDigits}`;
}

/** Normalise Double text typed by the user to its wire form, or throw when it is not a number. */
export function normaliseDoubleText(text: string): string {
  return doubleToWire(parseDoubleText(text));
}

/** The number a Double wire value stands for (NaN for text that is not a number). */
export function doubleFromWire(wire: Wire): number {
  if (typeof wire === 'number') return wire;
  if (typeof wire !== 'string') return NaN;
  try {
    return parseDoubleText(wire);
  } catch {
    return NaN;
  }
}

/** A whole number typed as text (32 bit range checked when `int32`). */
export function parseIntText(text: string, int32 = true): number {
  const cleaned = text.trim();
  if (!/^[+-]?\d+$/.test(cleaned)) throw new Error(`'${text}' is not a whole number`);
  const value = Number(cleaned);
  if (int32 && (value < -(2 ** 31) || value > 2 ** 31 - 1)) throw new Error(`${cleaned} is outside the 32-bit range`);
  return value;
}

// ---------------------------------------------------------------------------------------------
// Python literals

/** Python's repr of a str: single quotes unless the text holds ' and no ". */
export function pyStr(text: string): string {
  const quote = text.includes("'") && !text.includes('"') ? '"' : "'";
  let out = quote;
  for (const ch of text) {
    const code = ch.codePointAt(0) ?? 0;
    if (ch === '\\') out += '\\\\';
    else if (ch === quote) out += '\\' + quote;
    else if (ch === '\n') out += '\\n';
    else if (ch === '\r') out += '\\r';
    else if (ch === '\t') out += '\\t';
    else if (code < 0x20 || code === 0x7f) out += '\\x' + code.toString(16).padStart(2, '0');
    else out += ch;
  }
  return out + quote;
}

/** A parsed Python literal (enough of Python's grammar for `repr` of the entry types). */
export type PyValue =
  | { t: 'none' }
  | { t: 'bool'; v: boolean }
  | { t: 'int'; v: string }
  | { t: 'float'; v: number }
  | { t: 'str'; v: string }
  | { t: 'list' | 'tuple' | 'set'; items: PyValue[] }
  | { t: 'dict'; items: [PyValue, PyValue][] };

class PyParser {
  private pos = 0;
  constructor(private readonly text: string) {}

  parse(): PyValue {
    const value = this.value();
    this.space();
    if (this.pos !== this.text.length) this.fail('unexpected text');
    return value;
  }

  private fail(why: string): never {
    throw new Error(`not a Python literal: ${why} at ${this.pos}`);
  }

  private space(): void {
    while (this.pos < this.text.length && /\s/.test(this.text[this.pos])) this.pos++;
  }

  private peek(): string {
    this.space();
    return this.text[this.pos] ?? '';
  }

  private eat(ch: string): boolean {
    if (this.peek() === ch) {
      this.pos++;
      return true;
    }
    return false;
  }

  private value(): PyValue {
    const ch = this.peek();
    if (ch === '[') return this.sequence('[', ']', 'list');
    if (ch === '(') return this.sequence('(', ')', 'tuple');
    if (ch === '{') return this.braces();
    if (ch === "'" || ch === '"') return this.string();
    const rest = this.text.slice(this.pos);
    const word = /^[A-Za-z_]+/.exec(rest)?.[0];
    if (word === 'None') return this.advance(4, { t: 'none' });
    if (word === 'True') return this.advance(4, { t: 'bool', v: true });
    if (word === 'False') return this.advance(5, { t: 'bool', v: false });
    if (word && ['nan', 'inf', 'infinity'].includes(word.toLowerCase())) {
      return this.advance(word.length, { t: 'float', v: word.toLowerCase() === 'nan' ? NaN : Infinity });
    }
    const number = /^[+-]?\s*(\d[\d_]*\.?[\d_]*|\.\d[\d_]*)([eE][+-]?\d+)?/.exec(rest);
    if (number) {
      this.pos += number[0].length;
      const literal = number[0].replace(/[\s_]/g, '');
      return /[.eE]/.test(literal) ? { t: 'float', v: Number(literal) } : { t: 'int', v: String(BigInt(literal)) };
    }
    if (ch === '-' && /^-\s*(inf|infinity)\b/i.test(rest)) {
      const match = /^-\s*(inf|infinity)/i.exec(rest)!;
      return this.advance(match[0].length, { t: 'float', v: -Infinity });
    }
    return this.fail('unexpected character');
  }

  private advance(count: number, value: PyValue): PyValue {
    this.pos += count;
    return value;
  }

  private sequence(open: string, close: string, t: 'list' | 'tuple'): PyValue {
    this.eat(open);
    const items: PyValue[] = [];
    while (!this.eat(close)) {
      if (this.pos >= this.text.length) this.fail(`missing ${close}`);
      items.push(this.value());
      if (!this.eat(',') && this.peek() !== close) this.fail(`expected , or ${close}`);
    }
    return { t, items };
  }

  private braces(): PyValue {
    this.eat('{');
    if (this.eat('}')) return { t: 'dict', items: [] };
    const first = this.value();
    if (this.eat(':')) {
      const items: [PyValue, PyValue][] = [[first, this.value()]];
      while (this.eat(',')) {
        if (this.peek() === '}') break;
        const key = this.value();
        if (!this.eat(':')) this.fail('expected :');
        items.push([key, this.value()]);
      }
      if (!this.eat('}')) this.fail('expected }');
      return { t: 'dict', items };
    }
    const items = [first];
    while (this.eat(',')) {
      if (this.peek() === '}') break;
      items.push(this.value());
    }
    if (!this.eat('}')) this.fail('expected }');
    return { t: 'set', items };
  }

  private string(): PyValue {
    let result = '';
    // Adjacent string literals are joined, as in Python.
    while (this.peek() === "'" || this.peek() === '"') {
      const quote = this.text[this.pos++];
      for (;;) {
        if (this.pos >= this.text.length) this.fail('unterminated string');
        const ch = this.text[this.pos++];
        if (ch === quote) break;
        if (ch !== '\\') {
          result += ch;
          continue;
        }
        const next = this.text[this.pos++];
        const simple: Record<string, string> = { n: '\n', r: '\r', t: '\t', '\\': '\\', "'": "'", '"': '"', '0': '\0', a: '\x07', b: '\b', f: '\f', v: '\v' };
        if (next in simple) result += simple[next];
        else if (next === 'x') result += this.codePoint(2);
        else if (next === 'u') result += this.codePoint(4);
        else if (next === 'U') result += this.codePoint(8);
        else if (next === '\n') continue;
        else result += '\\' + next;
      }
    }
    return { t: 'str', v: result };
  }

  private codePoint(length: number): string {
    const hex = this.text.slice(this.pos, this.pos + length);
    if (!/^[0-9a-fA-F]+$/.test(hex) || hex.length !== length) this.fail('bad escape');
    this.pos += length;
    return String.fromCodePoint(parseInt(hex, 16));
  }
}

/** Parse Python literal text. @throws Error when it is not a literal this reader knows. */
export function parsePyLiteral(text: string): PyValue {
  return new PyParser(text).parse();
}

/** Python literal text of a parsed value (the inverse of {@link parsePyLiteral}). */
export function pyRepr(value: PyValue): string {
  switch (value.t) {
    case 'none':
      return 'None';
    case 'bool':
      return value.v ? 'True' : 'False';
    case 'int':
      return value.v;
    case 'float':
      return doubleToWire(value.v);
    case 'str':
      return pyStr(value.v);
    case 'list':
      return `[${value.items.map(pyRepr).join(', ')}]`;
    case 'tuple':
      return value.items.length === 1 ? `(${pyRepr(value.items[0])},)` : `(${value.items.map(pyRepr).join(', ')})`;
    case 'set':
      return value.items.length ? `{${value.items.map(pyRepr).join(', ')}}` : 'set()';
    case 'dict':
      return `{${value.items.map(([k, v]) => `${pyRepr(k)}: ${pyRepr(v)}`).join(', ')}}`;
  }
}

/** What Python's `str()` shows for a value (the widget's combo box items of List entries). */
export function pyDisplay(value: PyValue): string {
  return value.t === 'str' ? value.v : pyRepr(value);
}

/** The items a container entry's combo box lists (`str()` of each item; `key: value` for a dict). */
export function containerItems(text: string): string[] {
  try {
    const value = parsePyLiteral(text);
    if (value.t === 'dict') return value.items.map(([k, v]) => `${pyDisplay(k)}: ${pyDisplay(v)}`);
    if (value.t === 'list' || value.t === 'tuple' || value.t === 'set') return value.items.map(pyDisplay);
  } catch {
    // Not readable here (or set() written as a call): no items to list.
  }
  return [];
}

/** A JSON value from a tree file (tags such as `{"__set__": [...]}`) as Python literal text. */
export function fileValueToPy(value: unknown): string {
  if (value === null || value === undefined) return 'None';
  if (typeof value === 'boolean') return value ? 'True' : 'False';
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : doubleToWire(value);
  if (typeof value === 'string') return pyStr(value);
  if (Array.isArray(value)) return `[${value.map(fileValueToPy).join(', ')}]`;
  const record = value as Record<string, unknown>;
  const keys = Object.keys(record);
  if (keys.length === 1 && Array.isArray(record[keys[0]])) {
    const items = record[keys[0]] as unknown[];
    if (keys[0] === '__tuple__') return items.length === 1 ? `(${fileValueToPy(items[0])},)` : `(${items.map(fileValueToPy).join(', ')})`;
    if (keys[0] === '__set__' || keys[0] === '__frozenset__') return items.length ? `{${items.map(fileValueToPy).join(', ')}}` : 'set()';
    if (keys[0] === '__dict__') {
      return `{${items.map((pair) => (Array.isArray(pair) ? `${fileValueToPy(pair[0])}: ${fileValueToPy(pair[1])}` : '')).join(', ')}}`;
    }
  }
  return `{${keys.map((key) => `${pyStr(key)}: ${fileValueToPy(record[key])}`).join(', ')}}`;
}

/** Look up a value inside a parsed literal by a field path (`pose.position.x`, `waypoints[0]`). */
export function pyLookup(root: PyValue | null, path: string): PyValue | undefined {
  let current: PyValue | undefined = root ?? undefined;
  for (const part of splitFieldPath(path)) {
    if (!current) return undefined;
    if (typeof part === 'number') {
      current = current.t === 'list' || current.t === 'tuple' ? current.items[part] : undefined;
    } else {
      current = current.t === 'dict' ? current.items.find(([k]) => k.t === 'str' && k.v === part)?.[1] : undefined;
    }
  }
  return current;
}

/** `Goal.waypoints[0].x` -> ['Goal', 'waypoints', 0, 'x'] (contract 2.1 field paths). */
export function splitFieldPath(path: string): (string | number)[] {
  const parts: (string | number)[] = [];
  for (const piece of path.split('.')) {
    const match = /^([^[]*)((?:\[\d+\])*)$/.exec(piece);
    if (!match) {
      parts.push(piece);
      continue;
    }
    if (match[1]) parts.push(match[1]);
    for (const index of match[2].matchAll(/\[(\d+)\]/g)) parts.push(Number(index[1]));
  }
  return parts;
}

/** Default wire values of the entry types. */
export function defaultWire(type: string): Wire {
  switch (type) {
    case 'Integer':
      return 0;
    case 'Double':
      return '0.0';
    case 'String':
      return '';
    case 'Bool':
      return false;
    case 'List':
      return '[]';
    case 'Dictionary':
      return '{}';
    case 'Set':
      return 'set()';
    default:
      return null;
  }
}

/**
 * A literal of an Evaluation or Set node for an entry of another type, as the widget converts
 * it (values.convert_literal): numbers, booleans and number text between Integer, Double and
 * Bool, anything to text for String, the type's default when it cannot be converted.
 */
export function convertLiteral(value: Wire, from: string, to: string): Wire {
  if (from === to) return value;
  const number = typeof value === 'number' ? value : typeof value === 'boolean' ? Number(value) : from === 'Double' || from === 'String' ? doubleFromWire(value) : NaN;
  switch (to) {
    case 'Integer':
      return Number.isFinite(number) ? Math.min(Math.max(Math.round(number), -(2 ** 31)), 2 ** 31 - 1) : 0;
    case 'Double':
      return Number.isNaN(number) && from !== 'Double' ? '0.0' : doubleToWire(number);
    case 'String':
      return value === null ? '' : typeof value === 'boolean' ? (value ? 'True' : 'False') : String(value);
    case 'Bool':
      if (typeof value === 'string' && from === 'String') return ['true', '1', 'yes', 'on'].includes(value.trim().toLowerCase());
      return Number.isFinite(number) ? number !== 0 : false;
    default:
      return defaultWire(to);
  }
}
