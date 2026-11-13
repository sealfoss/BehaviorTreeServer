import { describe, expect, it } from 'vitest';
import {
  containerItems, convertLiteral, doubleFromWire, doubleToWire, fileValueToPy, normaliseDoubleText, parseDoubleText, parseIntText, parsePyLiteral, pyRepr,
  pyStr, splitFieldPath,
} from '../src/lib/wire';

describe('Double text (P-10)', () => {
  it('accepts numbers, nan, inf, -inf and "," as the decimal point', () => {
    expect(parseDoubleText('1,5')).toBe(1.5);
    expect(parseDoubleText(' 2.25 ')).toBe(2.25);
    expect(parseDoubleText('nan')).toBeNaN();
    expect(parseDoubleText('NaN')).toBeNaN();
    expect(parseDoubleText('inf')).toBe(Infinity);
    expect(parseDoubleText('Infinity')).toBe(Infinity);
    expect(parseDoubleText('-inf')).toBe(-Infinity);
    expect(parseDoubleText('1e-3')).toBe(0.001);
    expect(parseDoubleText('-.5')).toBe(-0.5);
  });

  it('refuses what is not a number', () => {
    for (const text of ['', 'abc', '1.2.3', '1,2,3', '0x10', '1 2', 'nanx']) expect(() => parseDoubleText(text)).toThrow();
  });

  it('writes Python\'s repr of a float', () => {
    expect(doubleToWire(1)).toBe('1.0');
    expect(doubleToWire(1.5)).toBe('1.5');
    expect(doubleToWire(0.1)).toBe('0.1');
    expect(doubleToWire(-0)).toBe('-0.0');
    expect(doubleToWire(100)).toBe('100.0');
    expect(doubleToWire(123.456)).toBe('123.456');
    expect(doubleToWire(0.0001)).toBe('0.0001');
    expect(doubleToWire(1.5e-5)).toBe('1.5e-05');
    expect(doubleToWire(1e16)).toBe('1e+16');
    expect(doubleToWire(1234567890123456.0)).toBe('1234567890123456.0');
    expect(doubleToWire(1e100)).toBe('1e+100');
    expect(doubleToWire(NaN)).toBe('nan');
    expect(doubleToWire(-Infinity)).toBe('-inf');
  });

  it('normalises typed text to the wire form, and reads it back', () => {
    expect(normaliseDoubleText('1,5')).toBe('1.5');
    expect(normaliseDoubleText('NAN')).toBe('nan');
    expect(normaliseDoubleText('3')).toBe('3.0');
    expect(doubleFromWire('2.5')).toBe(2.5);
    expect(doubleFromWire('inf')).toBe(Infinity);
    expect(doubleFromWire(null)).toBeNaN();
  });

  it('reads whole numbers within 32 bits', () => {
    expect(parseIntText(' -12 ')).toBe(-12);
    expect(() => parseIntText('1.5')).toThrow();
    expect(() => parseIntText('2147483648')).toThrow();
    expect(parseIntText('2147483648', false)).toBe(2147483648);
  });
});

describe('Python literal text', () => {
  it('reads and writes the literals of the entry types', () => {
    for (const text of ["[1, 2.5, 'text']", "{'key': 3, 'n': [True, None]}", '{1, 2}', "(1,)", "('a', 'b')", 'set()', "{'x': nan, 'y': -inf}", "'it\\'s'"]) {
      const value = text === 'set()' ? { t: 'set' as const, items: [] } : parsePyLiteral(text);
      expect(pyRepr(value)).toBe(text === "'it\\'s'" ? `"it's"` : text);
    }
  });

  it('refuses what is not a literal', () => {
    for (const text of ["__import__('os')", '[1, 2', '{1: }', 'x + 1']) expect(() => parsePyLiteral(text)).toThrow();
  });

  it('quotes strings as Python does', () => {
    expect(pyStr('battery')).toBe("'battery'");
    expect(pyStr("it's")).toBe(`"it's"`);
    expect(pyStr(`both ' and "`)).toBe(`'both \\' and "'`);
    expect(pyStr('a\nb\\')).toBe("'a\\nb\\\\'");
  });

  it('lists the items of a container entry as the widget\'s combo box does', () => {
    expect(containerItems("[1, 'two', 3.0]")).toEqual(['1', 'two', '3.0']);
    expect(containerItems("{'a': 1, 'b': 'x'}")).toEqual(['a: 1', 'b: x']);
    expect(containerItems('not a literal')).toEqual([]);
  });

  it('writes tree-file JSON values as Python literals', () => {
    expect(fileValueToPy({ pose: { x: 1.5, frame: 'map' }, ids: [1, 2], ok: true, none: null })).toBe(
      "{'pose': {'x': 1.5, 'frame': 'map'}, 'ids': [1, 2], 'ok': True, 'none': None}",
    );
    expect(fileValueToPy({ __tuple__: [1] })).toBe('(1,)');
    expect(fileValueToPy({ __dict__: [[1, 'a']] })).toBe("{1: 'a'}");
  });

  it('splits field paths', () => {
    expect(splitFieldPath('Goal.waypoints[0].x')).toEqual(['Goal', 'waypoints', 0, 'x']);
    expect(splitFieldPath('pose.position.x')).toEqual(['pose', 'position', 'x']);
  });
});

describe('literals of Evaluation and Set follow the entry type (the widget\'s convert_literal)', () => {
  it('converts numbers, booleans and text', () => {
    expect(convertLiteral(5, 'Integer', 'Double')).toBe('5.0');
    expect(convertLiteral(5, 'Integer', 'String')).toBe('5');
    expect(convertLiteral('2.6', 'Double', 'Integer')).toBe(3);
    expect(convertLiteral('yes', 'String', 'Bool')).toBe(true);
    expect(convertLiteral(true, 'Bool', 'Integer')).toBe(1);
    expect(convertLiteral('word', 'String', 'Double')).toBe('0.0');
    expect(convertLiteral(3, 'Integer', 'List')).toBe('[]');
  });
});
