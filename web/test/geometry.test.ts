import { describe, expect, it } from 'vitest';
import { childOrder, clampZoom, connectionPath, nearestFreeOffset, type Rect } from '../src/lib/geometry';

const r = (x: number, y: number, w: number, h: number): Rect => ({ x, y, w, h });

describe('child order (design 11.4)', () => {
  it('goes left to right by the centre of each node', () => {
    expect(childOrder([{ id: 'right', cx: 300, top: 0 }, { id: 'left', cx: -20, top: 50 }, { id: 'mid', cx: 100, top: 10 }])).toEqual(['left', 'mid', 'right']);
  });

  it('puts the higher node first on a tie', () => {
    expect(childOrder([{ id: 'low', cx: 100, top: 200 }, { id: 'high', cx: 100, top: 40 }])).toEqual(['high', 'low']);
  });

  it('keeps the current order of nodes at exactly the same place', () => {
    expect(childOrder([{ id: 'b', cx: 10, top: 10 }, { id: 'a', cx: 10, top: 10 }])).toEqual(['b', 'a']);
  });

  it('uses the centre, not the left edge: a wide node left of a narrow one can come second', () => {
    // wide: x 0..400 (centre 200); narrow: x 50..150 (centre 100)
    expect(childOrder([{ id: 'wide', cx: 200, top: 0 }, { id: 'narrow', cx: 100, top: 0 }])).toEqual(['narrow', 'wide']);
  });
});

/** True when every moved rectangle keeps at least `gap` from every obstacle. */
function free(group: Rect[], obstacles: Rect[], dx: number, dy: number, gap: number): boolean {
  return group.every((g) =>
    obstacles.every((o) => {
      const x1 = g.x + dx;
      const y1 = g.y + dy;
      return !(x1 < o.x + o.w + gap - 1e-9 && x1 + g.w > o.x - gap + 1e-9 && y1 < o.y + o.h + gap - 1e-9 && y1 + g.h > o.y - gap + 1e-9);
    }),
  );
}

/** A small seeded random generator (the widget's test draws random layouts too). */
function rng(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state * 1664525 + 1013904223) >>> 0;
    return state / 2 ** 32;
  };
}

describe('paste placement (the widget\'s nearest_free_offset)', () => {
  it('does not move what is already free', () => {
    expect(nearestFreeOffset([r(0, 0, 10, 10)], [])).toEqual({ x: 0, y: 0 });
    expect(nearestFreeOffset([r(0, 0, 10, 10)], [r(100, 100, 10, 10)])).toEqual({ x: 0, y: 0 });
  });

  it('moves by the smaller extent', () => {
    expect(nearestFreeOffset([r(0, 0, 100, 40)], [r(0, 0, 100, 40)], 20)).toEqual({ x: 0, y: 60 });
    expect(nearestFreeOffset([r(0, 0, 40, 100)], [r(0, 0, 40, 100)], 20)).toEqual({ x: 60, y: 0 });
  });

  it('prefers right, then down, on ties', () => {
    const square = r(0, 0, 50, 50);
    expect(nearestFreeOffset([square], [square], 10)).toEqual({ x: 60, y: 0 });
    expect(nearestFreeOffset([square], [square, r(60, 0, 50, 50)], 10)).toEqual({ x: 0, y: 60 });
  });

  it('allows exactly the gap; 20 px by default', () => {
    expect(nearestFreeOffset([r(0, 0, 10, 10)], [r(0, 0, 10, 10)], 5)).toEqual({ x: 15, y: 0 });
    expect(nearestFreeOffset([r(0, 0, 10, 10)], [r(0, 0, 10, 10)])).toEqual({ x: 30, y: 0 });
  });

  it('moves the copies as a whole', () => {
    const group = [r(0, 0, 10, 10), r(30, 0, 10, 10)];
    const obstacles = [...group, r(12, 30, 16, 10)];
    const offset = nearestFreeOffset(group, obstacles, 2);
    expect(free(group, obstacles, offset.x, offset.y, 2)).toBe(true);
  });

  it.each([1, 2, 3, 4, 5, 6, 7, 8])('finds a free place and none nearer is free (seed %i)', (seed) => {
    const random = rng(seed);
    const between = (a: number, b: number) => a + (b - a) * random();
    const obstacles = Array.from({ length: 2 + Math.floor(random() * 16) }, () => r(between(-500, 500), between(-300, 300), between(40, 220), between(40, 160)));
    const group = obstacles.slice(0, 1 + Math.floor(random() * 3));
    const offset = nearestFreeOffset(group, obstacles, 20);
    expect(free(group, obstacles, offset.x, offset.y, 20)).toBe(true);
    const distance = Math.hypot(offset.x, offset.y);
    for (let radius = 0; radius < distance - 3; radius += 4) {
      const steps = Math.max(12, Math.floor((2 * Math.PI * radius) / 4));
      for (let step = 0; step < steps; step++) {
        const angle = (2 * Math.PI * step) / steps;
        expect(free(group, obstacles, radius * Math.cos(angle), radius * Math.sin(angle), 20)).toBe(false);
      }
    }
  });
});

describe('canvas helpers', () => {
  it('keeps the zoom within 0.2x and 3x', () => {
    expect(clampZoom(0.01)).toBe(0.2);
    expect(clampZoom(10)).toBe(3);
    expect(clampZoom(1.5)).toBe(1.5);
  });

  it('draws the widget\'s S-shaped curve, bending at least 40 px', () => {
    expect(connectionPath({ x: 0, y: 0 }, { x: 100, y: 20 })).toBe('M 0 0 C 0 40, 100 -20, 100 20');
    expect(connectionPath({ x: 0, y: 0 }, { x: 0, y: 200 })).toBe('M 0 0 C 0 100, 0 100, 0 200');
  });
});
