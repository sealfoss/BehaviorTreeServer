<script lang="ts">
  // The canvas (design 11.3, 11.4; contract 4: our own layer, not Svelte Flow): one world layer
  // with a CSS transform for pan and zoom, stacking groups < notes < lines < nodes, and the
  // widget's gestures.
  import { canvasState, type LabelRef } from '../lib/canvasState.svelte';
  import { copyText, planPaste, readClipboard } from '../lib/clipboard';
  import { childOrder, connectionPath, normaliseRect, rectContains, rectsIntersect, type Point, type Rect } from '../lib/geometry';
  import { newId } from '../lib/ids';
  import { childIds, findNode, parentId } from '../lib/ops';
  import { isEditable, primary } from '../lib/platform';
  import { estimateSize, session } from '../lib/stores/session.svelte';
  import type { MenuItem } from '../lib/stores/ui.svelte';
  import type { CatalogEntry, TreeDocument, WireNode } from '../lib/types';
  import { doubleToWire, fileValueToPy } from '../lib/wire';
  import Connections from './Connections.svelte';
  import GroupView from './GroupView.svelte';
  import NodeView from './NodeView.svelte';
  import NoteView from './NoteView.svelte';

  const view = session.view;
  const CULL_FROM = 150; // nodes; smaller trees are drawn whole (design 11.3)
  const GROUP_COLORS: [string, string][] = [
    ['Blue', '#3d7cc9'], ['Green', '#2e9d4f'], ['Red', '#d04545'], ['Orange', '#e08a1e'], ['Purple', '#8a5cc2'], ['Teal', '#1f9e9e'], ['Gray', '#7a7a7a'],
  ];

  let el: HTMLDivElement;
  const doc = $derived(session.shownDoc);
  const readOnly = $derived(!session.canEdit);
  const groups = $derived(doc?.nodes.filter((n) => n.type === 'Group') ?? []);
  const notes = $derived(doc?.nodes.filter((n) => n.type === 'Notes') ?? []);
  const treeNodes = $derived(doc?.nodes.filter((n) => n.type !== 'Group' && n.type !== 'Notes') ?? []);
  const visibleNodes = $derived.by(() => {
    if (treeNodes.length < CULL_FROM) return treeNodes;
    const margin = 300;
    const viewport: Rect = { x: -view.panX / view.zoom - margin, y: -view.panY / view.zoom - margin, w: view.width / view.zoom + 2 * margin, h: view.height / view.zoom + 2 * margin };
    return treeNodes.filter((node) => rectsIntersect(viewport, rectOf(node)));
  });

  function rectOf(node: WireNode): Rect {
    const size = view.sizes[node.id] ?? estimateSize(node);
    return { x: node.x, y: node.y, w: size.w, h: size.h };
  }

  // ------------------------------------------------------------------------------ gestures

  type Gesture =
    | { kind: 'pan'; sx: number; sy: number; panX: number; panY: number }
    | { kind: 'band'; start: Point; current: Point; add: boolean; moved: boolean; sx: number; sy: number }
    | { kind: 'ctrl'; id: string; sx: number; sy: number; panX: number; panY: number; moved: boolean }
    | {
        kind: 'move'; ids: string[]; starts: Map<string, Point>; start: Point; sx: number; sy: number; moved: boolean;
        clickId: string; clickSelectsOnly: boolean; noteText: boolean; groupName: boolean; lastSent: number; timer: ReturnType<typeof setTimeout> | null;
      }
    | { kind: 'connect'; from: LabelRef; cursor: Point; over: LabelRef | null; target: 'label' | 'node' | 'empty' }
    | { kind: 'resize'; id: string; edges: string; start: Point; rect: Rect; min: [number, number] };

  let gesture = $state<Gesture | null>(null);
  const DRAG_THRESHOLD = 3;

  function local(event: { clientX: number; clientY: number }): Point {
    const box = el.getBoundingClientRect();
    return { x: event.clientX - box.left, y: event.clientY - box.top };
  }

  function world(event: { clientX: number; clientY: number }): Point {
    const point = local(event);
    return view.toWorld(point.x, point.y);
  }

  function interactive(target: Element): boolean {
    return isEditable(target) || !!target.closest('button, select, input, textarea, a, label.check');
  }

  function onPointerDown(event: PointerEvent): void {
    const target = event.target as Element;
    if (event.button === 1) {
      event.preventDefault();
      startPan(event);
      return;
    }
    if (event.button !== 0) return;
    if (interactive(target) && !target.closest('[data-drag-area]')) return;
    el.focus({ preventScroll: true });
    if (document.activeElement !== el) el.focus();
    event.preventDefault();
    const label = target.closest<HTMLElement>('[data-label]');
    const resize = target.closest<HTMLElement>('[data-resize]');
    const connection = target.closest<Element>('[data-conn]');
    const nodeElement = target.closest<HTMLElement>('[data-node-id]');
    if (label && !readOnly && !primary(event)) {
      startConnect({ id: label.dataset.nodeId!, kind: label.dataset.label as 'parent' | 'children' }, event);
    } else if (resize && !readOnly) {
      startResize(resize.dataset.nodeId!, resize.dataset.resize!, event);
    } else if (nodeElement && !connection) {
      const id = nodeElement.dataset.nodeId!;
      if (primary(event)) {
        gesture = { kind: 'ctrl', id, sx: event.clientX, sy: event.clientY, panX: view.panX, panY: view.panY, moved: false };
      } else {
        startMove(id, event, !!target.closest('[data-note-text]'), !!target.closest('[data-group-name]'));
      }
    } else if (primary(event)) {
      startPan(event);
    } else {
      if (connection) {
        const [parent, child] = (connection.getAttribute('data-conn') ?? '').split(' ');
        view.selection.clear();
        view.selectedConnection = { parent, child };
      } else {
        view.selectedConnection = null;
      }
      const start = world(event);
      gesture = { kind: 'band', start, current: start, add: event.shiftKey, moved: false, sx: event.clientX, sy: event.clientY };
    }
    if (gesture) el.setPointerCapture(event.pointerId);
  }

  function startPan(event: PointerEvent): void {
    gesture = { kind: 'pan', sx: event.clientX, sy: event.clientY, panX: view.panX, panY: view.panY };
    el.setPointerCapture(event.pointerId);
  }

  function groupContents(group: WireNode): string[] {
    if (!doc) return [];
    const area = rectOf(group);
    return doc.nodes.filter((node) => node.id !== group.id && rectContains(area, rectOf(node))).map((node) => node.id);
  }

  function withGroupContents(ids: string[]): string[] {
    const result = new Set(ids);
    for (const id of ids) {
      const node = doc && findNode(doc, id);
      if (node?.type === 'Group') for (const inner of groupContents(node)) result.add(inner);
    }
    return [...result];
  }

  function startMove(id: string, event: PointerEvent, noteText: boolean, groupName: boolean): void {
    let clickSelectsOnly = false;
    if (event.shiftKey) view.selection.add(id);
    else if (!view.selection.has(id)) view.select([id]);
    else clickSelectsOnly = view.selection.size > 1;
    view.selectedConnection = null;
    const ids = readOnly ? [] : withGroupContents([...view.selection]);
    const starts = new Map<string, Point>();
    for (const nodeId of ids) {
      const node = doc && findNode(doc, nodeId);
      if (node) starts.set(nodeId, { x: node.x, y: node.y });
    }
    gesture = {
      kind: 'move', ids, starts, start: world(event), sx: event.clientX, sy: event.clientY, moved: false,
      clickId: id, clickSelectsOnly, noteText, groupName, lastSent: 0, timer: null,
    };
  }

  function startConnect(from: LabelRef, event: PointerEvent): void {
    canvasState.connectFrom = from;
    gesture = { kind: 'connect', from, cursor: world(event), over: null, target: 'node' };
  }

  function startResize(id: string, edges: string, event: PointerEvent): void {
    const node = doc && findNode(doc, id);
    if (!node) return;
    const min: [number, number] = node.type === 'Group' ? [120, 60] : [60, 30];
    gesture = { kind: 'resize', id, edges, start: world(event), rect: rectOf(node), min };
  }

  function onPointerMove(event: PointerEvent): void {
    const g = gesture;
    if (!g) return;
    if (g.kind === 'pan') {
      view.panX = g.panX + event.clientX - g.sx;
      view.panY = g.panY + event.clientY - g.sy;
    } else if (g.kind === 'ctrl') {
      if (g.moved || Math.hypot(event.clientX - g.sx, event.clientY - g.sy) > DRAG_THRESHOLD) {
        g.moved = true;
        view.panX = g.panX + event.clientX - g.sx;
        view.panY = g.panY + event.clientY - g.sy;
      }
    } else if (g.kind === 'band') {
      g.current = world(event);
      if (!g.moved && Math.hypot(event.clientX - g.sx, event.clientY - g.sy) > DRAG_THRESHOLD) g.moved = true;
    } else if (g.kind === 'move') {
      if (!g.moved && Math.hypot(event.clientX - g.sx, event.clientY - g.sy) <= DRAG_THRESHOLD) return;
      if (!g.moved) {
        g.moved = true;
        for (const id of g.ids) session.dragging.add(id);
      }
      const point = world(event);
      const dx = point.x - g.start.x;
      const dy = point.y - g.start.y;
      for (const [id, start] of g.starts) {
        const node = doc && findNode(doc, id);
        if (node) {
          node.x = Math.round((start.x + dx) * 100) / 100;
          node.y = Math.round((start.y + dy) * 100) / 100;
        }
      }
      sendMove(g, false);
    } else if (g.kind === 'connect') {
      g.cursor = world(event);
      const hit = document.elementFromPoint(event.clientX, event.clientY);
      const label = hit?.closest<HTMLElement>('[data-label]');
      g.over = null;
      g.target = hit?.closest('[data-node-id]:not([data-group])') ? 'node' : 'empty';
      if (label) {
        const candidate = { id: label.dataset.nodeId!, kind: label.dataset.label as 'parent' | 'children' };
        if (compatible(g.from, candidate)) {
          g.over = candidate;
          g.target = 'label';
        }
      }
      canvasState.connectOver = g.over;
    } else if (g.kind === 'resize') {
      const point = world(event);
      const node = doc && findNode(doc, g.id);
      if (!node) return;
      const next = resized(g, point.x - g.start.x, point.y - g.start.y);
      node.x = next.x;
      node.y = next.y;
      node.width = next.w;
      node.height = next.h;
    }
  }

  function resized(g: Extract<Gesture, { kind: 'resize' }>, dx: number, dy: number): Rect {
    let { x, y, w, h } = g.rect;
    const right = x + w;
    const bottom = y + h;
    const clamp = (value: number, min: number): number => Math.min(Math.max(Math.round(value), min), 2000);
    if (g.edges.includes('r')) w = clamp(w + dx, g.min[0]);
    if (g.edges.includes('b')) h = clamp(h + dy, g.min[1]);
    if (g.edges.includes('l')) {
      w = clamp(w - dx, g.min[0]);
      x = right - w;
    }
    if (g.edges.includes('t')) {
      h = clamp(h - dy, g.min[1]);
      y = bottom - h;
    }
    return { x: Math.round(x * 100) / 100, y: Math.round(y * 100) / 100, w, h };
  }

  /** Positions at most 20 times a second while dragging, and on release (design 10.2). */
  function sendMove(g: Extract<Gesture, { kind: 'move' }>, final: boolean): void {
    if (!doc) return;
    const now = performance.now();
    if (!final && now - g.lastSent < 50) {
      g.timer ??= setTimeout(() => {
        g.timer = null;
        if (gesture === g) sendMove(g, false);
      }, 50);
      return;
    }
    if (g.timer) clearTimeout(g.timer);
    g.timer = null;
    g.lastSent = now;
    const positions: Record<string, [number, number]> = {};
    for (const id of g.ids) {
      const node = findNode(doc, id);
      if (node) positions[id] = [node.x, node.y];
    }
    const orders = session.ordersFor(doc, g.ids);
    const op = Object.keys(orders).length ? { op: 'node.move', positions, orders } : { op: 'node.move', positions };
    const sent = session.sendOp(doc.key, op);
    if (final) {
      const ids = g.ids;
      void sent.finally(() => {
        // Nodes that a newer drag holds stay held.
        const held = gesture?.kind === 'move' ? new Set(gesture.ids) : new Set<string>();
        for (const id of ids) if (!held.has(id)) session.dragging.delete(id);
      });
    }
  }

  function onPointerUp(event: PointerEvent): void {
    const g = gesture;
    gesture = null;
    if (el.hasPointerCapture(event.pointerId)) el.releasePointerCapture(event.pointerId);
    if (!g) return;
    if (g.kind === 'ctrl' && !g.moved) {
      view.toggle(g.id);
    } else if (g.kind === 'band') {
      if (!g.moved) {
        if (!g.add) view.selection.clear();
        return;
      }
      const band = normaliseRect(g.start, g.current);
      const hits = (doc?.nodes ?? []).filter((node) => {
        const rect = rectOf(node);
        return rectsIntersect(band, node.type === 'Group' ? { ...rect, h: 28 } : rect);
      });
      view.select(hits.map((node) => node.id), g.add);
    } else if (g.kind === 'move') {
      if (g.moved) {
        sendMove(g, true);
      } else {
        if (g.clickSelectsOnly) view.select([g.clickId]);
        if (g.noteText && !readOnly) canvasState.editingNote = g.clickId;
        if (g.groupName && !readOnly) canvasState.editingGroup = g.clickId;
      }
    } else if (g.kind === 'connect') {
      finishConnect(g, event);
    } else if (g.kind === 'resize') {
      const node = doc && findNode(doc, g.id);
      if (node && doc) {
        void session.sendOp(doc.key, { op: 'node.resize', id: g.id, width: node.width, height: node.height, x: node.x, y: node.y });
      }
    }
  }

  function onPointerCancel(event: PointerEvent): void {
    const g = gesture;
    if (g?.kind === 'move' && g.moved) sendMove(g, true);
    gesture = null;
    canvasState.connectFrom = null;
    canvasState.connectOver = null;
    if (el.hasPointerCapture(event.pointerId)) el.releasePointerCapture(event.pointerId);
  }

  // ------------------------------------------------------------------------------ connecting

  function canHaveChildren(node: WireNode | undefined): boolean {
    if (!node) return false;
    const kind = node.type === 'Root' ? 'root' : session.catalog.get(node.type)?.kind;
    return kind === 'root' || kind === 'composite' || kind === 'decorator' || ['Sequence', 'Selector', 'Parallel', 'Negation'].includes(node.type);
  }

  function isAncestor(doc: TreeDocument, ancestor: string, id: string): boolean {
    let current: string | undefined = id;
    const seen = new Set<string>();
    while (current && !seen.has(current)) {
      if (current === ancestor) return true;
      seen.add(current);
      current = parentId(doc, current);
    }
    return false;
  }

  /** Parent and child of a connection between two labels, or null (no cycles, design 9.3). */
  function pairOf(from: LabelRef, to: LabelRef): { parent: string; child: string } | null {
    if (!doc || from.id === to.id || from.kind === to.kind) return null;
    const pair = from.kind === 'children' ? { parent: from.id, child: to.id } : { parent: to.id, child: from.id };
    if (isAncestor(doc, pair.child, pair.parent)) return null;
    return pair;
  }

  function compatible(from: LabelRef, to: LabelRef): boolean {
    return pairOf(from, to) !== null;
  }

  function orderWith(doc: TreeDocument, parent: string, child: string): string[] {
    const ids = childIds(doc, parent).filter((id) => id !== child);
    if (findNode(doc, parent)?.type === 'Negation') return [child];
    const nodes = [...ids, child].map((id) => findNode(doc, id)).filter((n): n is WireNode => !!n);
    return childOrder(nodes.map((node) => session.orderItem(node)));
  }

  function connect(parent: string, child: string): void {
    if (!doc) return;
    void session.sendOp(doc.key, { op: 'conn.add', parent, child, order: orderWith(doc, parent, child) });
  }

  function finishConnect(g: Extract<Gesture, { kind: 'connect' }>, event: PointerEvent): void {
    canvasState.connectFrom = null;
    canvasState.connectOver = null;
    if (g.over) {
      const pair = pairOf(g.from, g.over);
      if (pair) connect(pair.parent, pair.child);
    } else if (g.target === 'empty') {
      openAddMenu(event.clientX, event.clientY, g.cursor, g.from);
    }
  }

  // ------------------------------------------------------------------------------ adding nodes

  function nextGroupName(doc: TreeDocument): string {
    let highest = 0;
    for (const node of doc.nodes) {
      const match = node.type === 'Group' ? /^group_(\d+)$/.exec(node.title) : null;
      if (match) highest = Math.max(highest, Number(match[1]));
    }
    return `group_${String(highest + 1).padStart(3, '0')}`;
  }

  /** A new node of a catalog type, its fields at their defaults in wire form. */
  function makeNode(spec: CatalogEntry, x: number, y: number, doc: TreeDocument): WireNode {
    const node: WireNode = { id: newId(), type: spec.type, title: spec.title, x, y };
    if (spec.kind === 'composite') {
      if (spec.type === 'Parallel') node.policy = 'all';
      else node.memory = doc.config.default_memory !== false;
    } else if (spec.kind === 'note') {
      Object.assign(node, { text: '', width: 220, height: 120 });
    } else if (spec.kind === 'group') {
      Object.assign(node, { title: nextGroupName(doc), width: 360, height: 240, color: '#3d7cc9' });
    } else if (spec.fields.length) {
      node.fields = {};
      for (const field of spec.fields) {
        const value = field.default;
        if (field.kind === 'float') node.fields[field.name] = typeof value === 'number' ? doubleToWire(value) : value;
        else if (field.kind === 'other') node.fields[field.name] = value === null || value === undefined ? null : fileValueToPy(value);
        else node.fields[field.name] = $state.snapshot(value); // the catalog is reactive state: a plain copy
        if (field.kind === 'choice') (node.field_selections ??= {})[field.name] = 0;
      }
    }
    return node;
  }

  function addNode(spec: CatalogEntry, at: Point, connectTo: LabelRef | null): void {
    if (!doc) return;
    const node = makeNode(spec, Math.round(at.x * 100) / 100, Math.round(at.y * 100) / 100, doc);
    const size = estimateSize(node);
    let pair: { parent: string; child: string } | null = null;
    if (connectTo && spec.kind !== 'note' && spec.kind !== 'group') {
      if (connectTo.kind === 'children') {
        pair = { parent: connectTo.id, child: node.id };
        node.x = Math.round((at.x - size.w / 2) * 100) / 100;
      } else if (canHaveChildren(node)) {
        pair = { parent: node.id, child: connectTo.id };
        node.x = Math.round((at.x - size.w / 2) * 100) / 100;
        node.y = Math.round((at.y - size.h) * 100) / 100;
      }
    }
    void session.sendOp(doc.key, { op: 'node.add', node });
    if (pair) connect(pair.parent, pair.child);
    view.select([node.id]);
  }

  function openAddMenu(clientX: number, clientY: number, at: Point, connectTo: LabelRef | null): void {
    if (readOnly) return;
    const locked = session.locked;
    const items: MenuItem[] = [{ label: 'Add Node', heading: true }];
    session.catalog.menu().forEach((group, index) => {
      if (index > 0) items.push({ label: '', separator: true });
      for (const spec of group.types) items.push({ label: spec.title, disabled: locked, action: () => addNode(spec, at, connectTo) });
    });
    if (locked) items.push({ label: '', separator: true }, { label: 'Stop execution to add nodes', disabled: true });
    session.ui.openMenu(clientX, clientY, items);
  }

  // ------------------------------------------------------------------------------ menus

  function onContextMenu(event: MouseEvent): void {
    const target = event.target as Element;
    if (isEditable(target)) return; // the browser's own menu for text fields
    event.preventDefault();
    if (gesture) return;
    const connection = target.closest('[data-conn]');
    const nodeElement = target.closest<HTMLElement>('[data-node-id]');
    if (connection) {
      const [parent, child] = (connection.getAttribute('data-conn') ?? '').split(' ');
      view.selectedConnection = { parent, child };
      session.ui.openMenu(event.clientX, event.clientY, [
        { label: 'Delete Connection', disabled: readOnly || session.locked, action: () => doc && void session.sendOp(doc.key, { op: 'conn.remove', parent, child }) },
      ]);
      return;
    }
    view.selectedConnection = null;
    const node = nodeElement && doc ? findNode(doc, nodeElement.dataset.nodeId!) : undefined;
    if (node) {
      if (!view.selection.has(node.id)) view.select([node.id]);
      session.ui.openMenu(event.clientX, event.clientY, nodeMenu(node));
    } else {
      openAddMenu(event.clientX, event.clientY, world(event), null);
    }
  }

  function nodeMenu(node: WireNode): MenuItem[] {
    if (!doc) return [];
    const key = doc.key;
    const locked = session.locked;
    const items: MenuItem[] = [];
    const send = (op: { op: string; [k: string]: unknown }) => void session.sendOp(key, op);
    if (node.type === 'Group') {
      items.push(
        { label: 'Rename...', disabled: readOnly, action: () => (canvasState.editingGroup = node.id) },
        { label: 'Select Contents', action: () => view.select([node.id, ...groupContents(node)]) },
        {
          label: 'Border Color',
          disabled: readOnly,
          submenu: [
            ...GROUP_COLORS.map(([name, color]) => ({ label: name, color, checked: node.color === color, action: () => send({ op: 'node.set_color', id: node.id, color }) })),
            { label: '', separator: true },
            { label: 'Custom...', checked: !GROUP_COLORS.some(([, c]) => c === node.color), action: () => pickColor(node) },
          ],
        },
        { label: '', separator: true },
        { label: locked ? 'Promote to Sub-Tree (stop execution first)' : 'Promote to Sub-Tree', disabled: readOnly || locked, action: () => void promote(node) },
      );
    } else if (node.type === 'Notes') {
      items.push({ label: 'Edit Text', disabled: readOnly, action: () => (canvasState.editingNote = node.id) });
    } else {
      items.push({ label: 'Rename...', disabled: readOnly, action: () => (canvasState.renaming = node.id) });
    }
    if (node.type === 'Sub-Tree' || node.type === 'RemoteSubTree') {
      const file = String(node.fields?.File ?? '').trim();
      items.push({ label: file ? 'Zoom In' : 'Zoom In (choose a file first)', disabled: !file, action: () => void session.zoomIn(node) });
    }
    if (canHaveChildren(node)) {
      items.push({ label: 'Select Children', disabled: !childIds(doc, node.id).length, action: () => view.select(subtree(node.id)) });
    }
    const composite = node.type === 'Root' ? (node.composite ?? 'Sequence') : ['Sequence', 'Selector', 'Parallel'].includes(node.type) ? node.type : null;
    if (composite) {
      items.push({ label: '', separator: true });
      const types = node.type === 'Root' ? ['Sequence', 'Selector'] : ['Sequence', 'Selector', 'Parallel'];
      for (const type of types) {
        items.push({ label: type, checked: composite === type, disabled: readOnly || locked, action: () => send({ op: 'composite.set_type', id: node.id, composite: type }) });
      }
      if (composite === 'Parallel') {
        items.push(
          { label: 'Policy: All', checked: (node.policy ?? 'all') === 'all', disabled: readOnly || locked, action: () => send({ op: 'composite.set_policy', id: node.id, policy: 'all' }) },
          { label: 'Policy: Any', checked: node.policy === 'any', disabled: readOnly || locked, action: () => send({ op: 'composite.set_policy', id: node.id, policy: 'any' }) },
        );
      } else {
        items.push({ label: 'Memory', checked: node.memory !== false, disabled: readOnly || locked, action: () => send({ op: 'composite.set_memory', id: node.id, memory: node.memory === false }) });
      }
    }
    if (!readOnly && node.type !== 'Notes' && node.type !== 'Group') {
      items.push({ label: 'Inspector', action: () => (session.ui.panelTab = 'inspector') });
    }
    items.push({ label: '', separator: true });
    if (node.type === 'Root') items.push({ label: 'Delete (the root node cannot be deleted)', disabled: true });
    else if (locked) items.push({ label: 'Delete (stop execution first)', disabled: true });
    else items.push({ label: 'Delete', disabled: readOnly, action: () => send({ op: 'node.remove', ids: [node.id] }) });
    return items;
  }

  function subtree(id: string): string[] {
    if (!doc) return [id];
    const result = [id];
    for (const child of childIds(doc, id)) result.push(...subtree(child));
    return result;
  }

  function pickColor(node: WireNode): void {
    const input = document.createElement('input');
    input.type = 'color';
    input.value = node.color ?? '#3d7cc9';
    input.addEventListener('change', () => doc && void session.sendOp(doc.key, { op: 'node.set_color', id: node.id, color: input.value }));
    input.click();
  }

  async function promote(node: WireNode): Promise<void> {
    if (!doc) return;
    // The new file goes next to the tree's own file (the widget's rule).
    const folder = doc.key.includes('/') ? doc.key.slice(0, doc.key.lastIndexOf('/') + 1) : '';
    // Done by the gateway, which sends the resulting ordinary operations back (contract 5.3).
    // The sizes as drawn let the gateway order the new tree's children as the canvas does.
    const sizes = Object.fromEntries(doc.nodes.map((item) => [item.id, [Math.round(rectOf(item).w), Math.round(rectOf(item).h)]]));
    const key = doc.key;
    const before = new Set(doc.nodes.map((item) => item.id));
    const reply = await session.sendOp(key, { op: 'group.promote', id: node.id, name: folder + node.title, sizes }, false);
    // The operations come before the answer: the new Sub-Tree node is there now, and is selected
    // (the widget: "titled with the group name and selected").
    const added = reply?.t === 'ok' ? (session.docs.get(key)?.nodes ?? []).filter((item) => !before.has(item.id) && item.type === 'Sub-Tree') : [];
    if (added.length) view.select(added.map((item) => item.id));
  }

  // ------------------------------------------------------------------------------ keyboard

  function onKeyDown(event: KeyboardEvent): void {
    if (isEditable(event.target as Element)) return;
    if (event.key === 'Delete' || event.key === 'Backspace') {
      event.preventDefault(); // Backspace never navigates back
      deleteSelection();
    } else if (primary(event) && event.key.toLowerCase() === 'a') {
      event.preventDefault();
      view.select(doc?.nodes.map((node) => node.id) ?? []);
    } else if (event.key === 'Escape') {
      view.selection.clear();
      view.selectedConnection = null;
      session.ui.closeMenu();
    } else if (primary(event) && event.key.toLowerCase() === 'c') {
      ensureCopyTarget();
    }
  }

  function deleteSelection(): void {
    if (!doc || readOnly) return;
    if (session.locked) {
      session.ui.notify('warning', 'Stop execution to delete nodes or connections.');
      return;
    }
    const selected = view.selectedConnection;
    if (selected) {
      void session.sendOp(doc.key, { op: 'conn.remove', parent: selected.parent, child: selected.child });
      view.selectedConnection = null;
      return;
    }
    const ids = [...view.selection].filter((id) => findNode(doc!, id));
    const root = ids.find((id) => findNode(doc!, id)?.type === 'Root');
    if (root) {
      // Delete with the Root selected deletes its connections instead (the widget's rule).
      for (const child of childIds(doc, root)) void session.sendOp(doc.key, { op: 'conn.remove', parent: root, child });
    }
    const removed = ids.filter((id) => id !== root);
    if (removed.length) void session.sendOp(doc.key, { op: 'node.remove', ids: removed });
    for (const id of removed) view.selection.delete(id);
  }

  /** Some browsers fire `copy` only when something is selected: select a hidden character. */
  let copyProxy: HTMLSpanElement;
  function ensureCopyTarget(): void {
    const selection = window.getSelection();
    if (selection && selection.isCollapsed && copyProxy) {
      const range = document.createRange();
      range.selectNodeContents(copyProxy);
      selection.removeAllRanges();
      selection.addRange(range);
    }
  }

  // ------------------------------------------------------------------------------ copy and paste

  function canvasHasKeyboard(): boolean {
    const active = document.activeElement;
    return !!el && (active === el || (el.contains(active) && !isEditable(active)));
  }

  function onCopy(event: ClipboardEvent): void {
    if (!doc || !canvasHasKeyboard() || !event.clipboardData) return;
    const text = copyText(doc.nodes, doc.connections, view.selection, (id) => view.sizes[id]);
    window.getSelection()?.removeAllRanges();
    if (!text) return;
    event.clipboardData.setData('text/plain', text);
    event.preventDefault();
  }

  function onPaste(event: ClipboardEvent): void {
    if (!doc || readOnly || !canvasHasKeyboard() || !event.clipboardData) return;
    const data = readClipboard(event.clipboardData.getData('text/plain'));
    if (!data) return;
    event.preventDefault();
    if (session.locked) {
      session.ui.notify('warning', 'Stop execution to paste nodes.');
      return;
    }
    const obstacles = doc.nodes.map(rectOf);
    const plan = planPaste(data, obstacles, newId, (type) => session.catalog.get(type), estimateSize);
    if (!plan.nodes.length) return;
    void session.sendOp(doc.key, { op: 'paste', nodes: plan.nodes, connections: plan.connections, encoding: 'wire', sizes: plan.sizes });
    view.select(plan.nodes.map((node) => node.id));
    revealNodes(plan.nodes);
  }

  /** Scroll the view to show nodes that lie outside it. */
  function revealNodes(nodes: WireNode[]): void {
    const viewport: Rect = { x: -view.panX / view.zoom, y: -view.panY / view.zoom, w: view.width / view.zoom, h: view.height / view.zoom };
    if (nodes.every((node) => rectContains(viewport, rectOf(node)))) return;
    const xs = nodes.map((node) => node.x);
    const ys = nodes.map((node) => node.y);
    view.centerOn((Math.min(...xs) + Math.max(...xs)) / 2 + 80, (Math.min(...ys) + Math.max(...ys)) / 2 + 50);
  }

  // ------------------------------------------------------------------------------ wheel

  $effect(() => {
    const wheel = (event: WheelEvent) => {
      const target = event.target as Element;
      if (target.closest('select:focus, textarea:focus')) return;
      event.preventDefault(); // non-passive: Ctrl+wheel must not zoom the page (P-04)
      const scale = event.deltaMode === 1 ? 33 : event.deltaMode === 2 ? view.height : 1;
      if (primary(event) || event.ctrlKey) {
        const steps = (-event.deltaY * scale) / 100;
        if (steps) {
          const point = local(event);
          view.zoomAt(Math.pow(1.15, steps), point.x, point.y);
        }
      } else if (event.shiftKey && !event.deltaX) {
        view.panX -= event.deltaY * scale;
      } else {
        view.panX -= event.deltaX * scale;
        view.panY -= event.deltaY * scale;
      }
    };
    el.addEventListener('wheel', wheel, { passive: false });
    document.addEventListener('copy', onCopy);
    document.addEventListener('paste', onPaste);
    return () => {
      el.removeEventListener('wheel', wheel);
      document.removeEventListener('copy', onCopy);
      document.removeEventListener('paste', onPaste);
    };
  });

  const dragLine = $derived.by(() => {
    const g = gesture;
    if (g?.kind !== 'connect' || !doc) return null;
    const node = findNode(doc, g.from.id);
    if (!node) return null;
    const size = view.sizes[node.id] ?? { ...estimateSize(node), parentTop: 0, childrenBottom: estimateSize(node).h };
    const anchor = { x: node.x + size.w / 2, y: node.y + (g.from.kind === 'children' ? size.childrenBottom : size.parentTop) };
    return g.from.kind === 'children' ? { from: anchor, to: g.cursor, valid: !!g.over } : { from: g.cursor, to: anchor, valid: !!g.over };
  });

  const band = $derived(gesture?.kind === 'band' && gesture.moved ? normaliseRect(gesture.start, gesture.current) : null);
</script>

<!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
<div
  class="canvas"
  class:panning={gesture?.kind === 'pan' || (gesture?.kind === 'ctrl' && gesture.moved)}
  class:read-only={readOnly}
  tabindex="0"
  role="application"
  aria-label="Behavior tree canvas"
  data-testid="canvas"
  style="background-size: {20 * view.zoom}px {20 * view.zoom}px; background-position: {view.panX}px {view.panY}px"
  bind:this={el}
  bind:clientWidth={view.width}
  bind:clientHeight={view.height}
  onpointerdown={onPointerDown}
  onpointermove={onPointerMove}
  onpointerup={onPointerUp}
  onpointercancel={onPointerCancel}
  onmousedown={(e) => {
    if (e.button === 1) e.preventDefault(); // no auto-scroll
  }}
  onauxclick={(e) => e.preventDefault()}
  oncontextmenu={onContextMenu}
  onkeydown={onKeyDown}
>
  <span class="copy-proxy" bind:this={copyProxy}>&#8203;</span>
  {#if doc}
    <div
      class="world"
      class:lod-low={view.lowDetail}
      data-testid="world"
      style="transform: translate({view.panX}px, {view.panY}px) scale({view.zoom})"
    >
      <div class="layer layer-groups">
        {#each groups as node (node.id)}<GroupView {node} {doc} {readOnly} />{/each}
      </div>
      <div class="layer layer-notes">
        {#each notes as node (node.id)}<NoteView {node} {doc} {readOnly} />{/each}
      </div>
      <Connections {doc} />
      <div class="layer layer-nodes">
        {#each visibleNodes as node (node.id)}<NodeView {node} {doc} {readOnly} />{/each}
      </div>
      {#if dragLine}
        <!-- Above the nodes, so the line stays visible while it crosses them. -->
        <svg class="connections drag-layer" aria-hidden="true">
          <path class="drag-line" class:valid={dragLine.valid} d={connectionPath(dragLine.from, dragLine.to)} data-testid="drag-line" />
        </svg>
      {/if}
      {#if band}
        <div class="band" style="transform: translate({band.x}px, {band.y}px); width: {band.w}px; height: {band.h}px"></div>
      {/if}
    </div>
  {:else}
    <div class="empty">{session.executors.online ? 'No tree is open. Use New or Load.' : 'Connecting…'}</div>
  {/if}
  {#if session.readOnly}
    <div class="read-only-banner" data-testid="read-only-banner">Read-only: {session.view.level?.kind === 'bundle' ? session.view.level.title : ''}</div>
  {/if}
</div>
