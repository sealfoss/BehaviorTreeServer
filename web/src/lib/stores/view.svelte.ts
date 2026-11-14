/**
 * Per-tab view state (design 11.2 `view`): pan, zoom, selection, and the stack of sub-trees
 * zoomed into. Also the measured sizes of the nodes drawn, which the child order, the paste
 * placement and the connection lines need: only a browser knows how wide a node is drawn.
 */

import { SvelteSet } from 'svelte/reactivity';
import { clampZoom } from '../geometry';

/** A level of the view stack: a document of the session, or a tree of a run's bundle (read-only). */
export type Level =
  | { kind: 'doc'; doc: string; via: string | null; prefix: string; zoom?: number; panX?: number; panY?: number }
  | { kind: 'bundle'; exec: string; tree: string; via: string | null; prefix: string; title: string; watched: boolean; zoom?: number; panX?: number; panY?: number };

/** Below this zoom nodes show title and status only (design 11.3). */
export const LOW_DETAIL_ZOOM = 0.4;

export interface NodeSize {
  w: number;
  h: number;
  /** Offset of the Parent label's top, and of the Children label's bottom, from the node's top. */
  parentTop: number;
  childrenBottom: number;
}

export class ViewStore {
  stack = $state<Level[]>([]);
  zoom = $state(1);
  panX = $state(0);
  panY = $state(0);
  width = $state(800);
  height = $state(600);
  selection = new SvelteSet<string>();
  selectedConnection = $state<{ parent: string; child: string } | null>(null);
  sizes = $state<Record<string, NodeSize>>({});

  get lowDetail(): boolean {
    return this.zoom < LOW_DETAIL_ZOOM;
  }

  get level(): Level | undefined {
    return this.stack[this.stack.length - 1];
  }

  /** The instance path of a node of the level shown (design 2.2 / 11.2). */
  pathOf(id: string): string {
    const prefix = this.level?.prefix ?? '';
    return prefix ? `${prefix}/${id}` : id;
  }

  reset(root: Level | null): void {
    this.stack = root ? [root] : [];
    this.selection.clear();
    this.selectedConnection = null;
  }

  push(level: Level): void {
    const current = this.level;
    if (current) Object.assign(current, { zoom: this.zoom, panX: this.panX, panY: this.panY });
    this.stack.push(level);
    this.selection.clear();
    this.selectedConnection = null;
  }

  pop(): Level | undefined {
    if (this.stack.length <= 1) return undefined;
    const top = this.stack.pop();
    const current = this.level;
    if (current?.zoom !== undefined) {
      this.zoom = current.zoom;
      this.panX = current.panX ?? 0;
      this.panY = current.panY ?? 0;
    }
    this.selection.clear();
    this.selectedConnection = null;
    return top;
  }

  /** Show world point (x, y) at the centre of the canvas with `zoom`. */
  centerOn(x: number, y: number, zoom = this.zoom): void {
    this.zoom = clampZoom(zoom);
    this.panX = this.width / 2 - x * this.zoom;
    this.panY = this.height / 2 - y * this.zoom;
  }

  /** The world point at the centre of the canvas (the view saved in a tree file). */
  center(): [number, number] {
    return [Math.round(((this.width / 2 - this.panX) / this.zoom) * 100) / 100, Math.round(((this.height / 2 - this.panY) / this.zoom) * 100) / 100];
  }

  toWorld(screenX: number, screenY: number): { x: number; y: number } {
    return { x: (screenX - this.panX) / this.zoom, y: (screenY - this.panY) / this.zoom };
  }

  /** Zoom by `factor` keeping the world point under (sx, sy) in place. */
  zoomAt(factor: number, sx: number, sy: number): void {
    const zoom = clampZoom(this.zoom * factor);
    const world = this.toWorld(sx, sy);
    this.zoom = zoom;
    this.panX = sx - world.x * zoom;
    this.panY = sy - world.y * zoom;
  }

  select(ids: Iterable<string>, add = false): void {
    if (!add) this.selection.clear();
    for (const id of ids) this.selection.add(id);
    this.selectedConnection = null;
  }

  toggle(id: string): void {
    if (this.selection.has(id)) this.selection.delete(id);
    else this.selection.add(id);
    this.selectedConnection = null;
  }
}
