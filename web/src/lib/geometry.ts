/**
 * Canvas geometry: rectangles, connection curves, the child order rule and the paste placement
 * rule, ported from the widget's `geometry.py` and `nodes.py`.
 */

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface Point {
  x: number;
  y: number;
}

export const PASTE_GAP = 20;
export const MIN_ZOOM = 0.2;
export const MAX_ZOOM = 3;

export function clampZoom(zoom: number): number {
  return Math.min(Math.max(zoom, MIN_ZOOM), MAX_ZOOM);
}

export function rectContains(outer: Rect, inner: Rect): boolean {
  return inner.x >= outer.x && inner.y >= outer.y && inner.x + inner.w <= outer.x + outer.w && inner.y + inner.h <= outer.y + outer.h;
}

export function rectsIntersect(a: Rect, b: Rect): boolean {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

export function normaliseRect(a: Point, b: Point): Rect {
  return { x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), w: Math.abs(a.x - b.x), h: Math.abs(a.y - b.y) };
}

/** Vertical S-shaped cubic Bézier from a parent anchor to a child anchor (the widget's curve). */
export function connectionPath(start: Point, end: Point): string {
  const bend = Math.max(40, Math.abs(end.y - start.y) * 0.5);
  return `M ${start.x} ${start.y} C ${start.x} ${start.y + bend}, ${end.x} ${end.y - bend}, ${end.x} ${end.y}`;
}

/** Centre of the order badge of a connection ending at `end` (radius 8, 6 px above the label). */
export function badgeCenter(end: Point): Point {
  return { x: end.x, y: end.y - 8 - 6 };
}

/** What orders a child among its siblings: the x of its centre, then the top (higher first). */
export interface OrderItem {
  id: string;
  cx: number;
  top: number;
}

/**
 * Children left to right by the centre of each node, the higher node first on a tie
 * (design 11.4). Children at exactly the same place keep their current order.
 */
export function childOrder(items: OrderItem[]): string[] {
  return items
    .map((item, index) => ({ item, index }))
    .sort((a, b) => a.item.cx - b.item.cx || a.item.top - b.item.top || a.index - b.index)
    .map(({ item }) => item.id);
}

export function sameOrder(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((id, index) => id === b[index]);
}

type Box = [number, number, number, number]; // x1, y1, x2, y2

/**
 * The shortest offset that moves `rects` together (keeping their arrangement) off `obstacles`,
 * keeping at least `gap` from every obstacle (exactly `gap` is allowed). Ties prefer moving
 * right, then down. The widget's exact search (`geometry.nearest_free_offset`).
 */
export function nearestFreeOffset(rects: Rect[], obstacles: Rect[], gap = PASTE_GAP): Point {
  const group: Box[] = rects.map((r) => [r.x, r.y, r.x + r.w, r.y + r.h]);
  const walls: Box[] = obstacles.map((o) => [o.x - gap, o.y - gap, o.x + o.w + gap, o.y + o.h + gap]);
  const blocked: Box[] = [];
  for (const [gx1, gy1, gx2, gy2] of group) {
    for (const [wx1, wy1, wx2, wy2] of walls) blocked.push([wx1 - gx2, wy1 - gy2, wx2 - gx1, wy2 - gy1]);
  }
  if (!blocked.some(([x1, y1, x2, y2]) => x1 < 0 && 0 < x2 && y1 < 0 && 0 < y2)) return { x: 0, y: 0 };

  const nearestOnLine = (regions: Box[], value: number, vertical: boolean): number => {
    const spans = regions
      .filter(([x1, y1, x2, y2]) => (vertical ? x1 < value && value < x2 : y1 < value && value < y2))
      .map(([x1, y1, x2, y2]) => (vertical ? [y1, y2] : [x1, x2]) as [number, number])
      .sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    let span: [number, number] | null = null;
    for (const [start, end] of spans) {
      if (span !== null && start < span[1]) {
        span = [span[0], Math.max(span[1], end)];
        continue;
      }
      if (span !== null && span[0] < 0 && 0 < span[1]) break;
      span = [start, end];
    }
    if (span === null || !(span[0] < 0 && 0 < span[1])) return 0;
    const [low, high] = span;
    return high <= -low ? high : low;
  };

  let best: { key: [number, boolean, boolean, number]; dx: number; dy: number } | null = null;
  const consider = (dx: number, dy: number): void => {
    const key: [number, boolean, boolean, number] = [Math.round(Math.hypot(dx, dy) * 1e6) / 1e6, dx < 0, dy < 0, Math.abs(dy)];
    if (best === null || compareKeys(key, best.key) < 0) best = { key, dx, dy };
  };
  consider(nearestOnLine(blocked, 0, false), 0);
  consider(0, nearestOnLine(blocked, 0, true));
  const first = best as unknown as { key: [number, boolean, boolean, number] };
  const limit = first.key[0] + 1e-6;
  const near = blocked.filter((r) => Math.hypot(Math.max(r[0], -r[2], 0), Math.max(r[1], -r[3], 0)) < limit);
  for (const vertical of [true, false]) {
    const values = new Set<number>();
    for (const r of near) {
      if (vertical) {
        values.add(r[0]);
        values.add(r[2]);
      } else {
        values.add(r[1]);
        values.add(r[3]);
      }
    }
    const lines = [...values].sort((a, b) => Math.abs(a) - Math.abs(b));
    for (const value of lines) {
      const current = best as unknown as { key: [number, boolean, boolean, number] };
      if (Math.abs(value) > current.key[0] + 1e-6) break;
      const other = nearestOnLine(near, value, vertical);
      if (vertical) consider(value, other);
      else consider(other, value);
    }
  }
  const result = best as unknown as { dx: number; dy: number };
  return { x: result.dx, y: result.dy };
}

function compareKeys(a: [number, boolean, boolean, number], b: [number, boolean, boolean, number]): number {
  if (a[0] !== b[0]) return a[0] - b[0];
  if (a[1] !== b[1]) return a[1] ? 1 : -1;
  if (a[2] !== b[2]) return a[2] ? 1 : -1;
  return a[3] - b[3];
}
