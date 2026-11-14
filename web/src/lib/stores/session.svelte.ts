/**
 * The session as this tab sees it: the stores of design 11.2, filled only by protocol messages
 * and local gestures, and the requests the page sends (contract 5.2).
 */

import { childOrder, sameOrder, type OrderItem } from '../geometry';
import { childIds, findNode, isStructural, parentId, type Op } from '../ops';
import { resolveSubtree, treeKeyOf } from '../paths';
import { ConnectionLost, GatewayConnection, gatewayUrl, type Incoming, type Outgoing, type Reply, type SocketLike } from '../protocol';
import type {
  Bundle, CatalogEntry, Entry, ExecutorInfo, FileQuestion, Finding, LogLine, RunMessage, Snapshot, StBody, TreeDocument, WireNode,
} from '../types';
import { BlackboardStore, type BbChanges } from './blackboard.svelte';
import { CatalogStore } from './catalog.svelte';
import { DocsStore } from './docs.svelte';
import { ExecutorsStore } from './executors.svelte';
import { LogStore } from './log.svelte';
import { RunStore } from './run.svelte';
import { UiStore } from './ui.svelte';
import { ViewStore, type Level } from './view.svelte';

/** Default size of a node not drawn yet (used for order and placement until it is measured). */
export function estimateSize(node: WireNode): { w: number; h: number } {
  if (node.type === 'Notes') return { w: node.width ?? 220, h: node.height ?? 120 };
  if (node.type === 'Group') return { w: node.width ?? 360, h: node.height ?? 240 };
  const fields = node.fields ? Object.keys(node.fields).length : 0;
  return { w: fields ? 240 : 170, h: 100 + fields * 30 };
}

export class Session {
  readonly docs = new DocsStore();
  readonly blackboard = new BlackboardStore();
  readonly run: RunStore;
  readonly log = new LogStore();
  readonly catalog = new CatalogStore();
  readonly executors = new ExecutorsStore();
  readonly view = new ViewStore();
  readonly ui = new UiStore();
  /** Bumped when a refused blackboard edit must be undone on the page. */
  bbVersion = $state(0);
  /** The executor watched read-only (a Remote Sub-Tree's run), if any. */
  watched = $state<string | null>(null);
  /** Nodes this tab is dragging: positions of their broadcasts are not applied meanwhile. */
  readonly dragging = new Set<string>();
  conn: GatewayConnection | null = null;
  private wantedExecutor: string | null = null;
  /** No `attach` was sent on the current connection yet. */
  private needsAttach = true;

  constructor(frame?: (fn: () => void) => void) {
    this.run = new RunStore(frame);
  }

  // ------------------------------------------------------------------------------ connection

  connect(socket?: (url: string) => SocketLike, url = gatewayUrl(location)): void {
    this.wantedExecutor = readWantedExecutor();
    this.conn = new GatewayConnection({
      url,
      socket,
      onMessage: (message) => this.receive(message),
      onOnline: (online) => {
        this.executors.online = online;
        if (!online) this.needsAttach = true;
      },
      onReload: () => {
        if (typeof location !== 'undefined') location.reload();
      },
    });
    this.conn.start();
  }

  /** Join an executor's session (the picker). */
  attach(name: string): void {
    this.wantedExecutor = name;
    rememberExecutor(name);
    if (this.watched) this.unwatch();
    this.conn?.send({ t: 'attach', executor: name });
  }

  receive(message: Incoming): void {
    switch (message.t) {
      case 'welcome':
        return this.onWelcome(message);
      case 'snapshot':
        return this.onSnapshot(message as unknown as Snapshot);
      case 'doc':
        return this.onDoc(message);
      case 'catalog':
        if (message.exec === this.executors.attached) this.catalog.replace(message.types as CatalogEntry[]);
        return;
      case 'op':
        return this.onOp(message);
      case 'st':
        this.run.applySt(String(message.exec), message as unknown as StBody);
        if (message.exec === this.executors.attached && typeof message.starting === 'boolean') this.run.starting = message.starting;
        return;
      case 'bb':
        if (message.exec === undefined || message.exec === this.executors.attached) this.blackboard.apply(message as BbChanges);
        return;
      case 'nd':
        this.run.applyNd(String(message.exec), (message.details ?? {}) as Record<string, Record<string, unknown>>);
        return;
      case 'log':
        if (message.exec === undefined || message.exec === this.executors.attached) {
          this.log.append(Number(message.first ?? 0), (message.lines ?? []) as LogLine[], Number(message.skipped ?? 0));
        }
        return;
      case 'run':
        return this.onRun(message as unknown as RunMessage);
      case 'notice':
        this.ui.notify((message.level as 'info' | 'warning' | 'error') ?? 'info', String(message.text ?? ''));
        return;
      case 'presence':
        this.executors.attachedCount = Number(message.attached ?? 0);
        return;
    }
  }

  private onWelcome(message: Incoming): void {
    const list = (message.executors ?? []) as ExecutorInfo[];
    this.executors.setList(list);
    if (typeof message.client === 'string') this.executors.client = message.client;
    if (typeof message.time === 'number') this.executors.clockOffset = message.time - Date.now() / 1000;
    this.executors.local = Boolean(message.local);
    // The first welcome of a connection attaches; a later one (a new list) only when the
    // executor this tab shows is gone from it.
    const names = list.map((item) => item.name);
    const current = this.executors.attached;
    if (this.needsAttach || !current || !names.includes(current)) {
      const wanted = [this.wantedExecutor, current].find((name) => name && names.includes(name)) ?? names[0];
      if (wanted) {
        this.conn?.send({ t: 'attach', executor: wanted });
        this.needsAttach = false;
      }
    }
  }

  private onSnapshot(snap: Snapshot): void {
    const changedExecutor = this.executors.attached !== snap.exec;
    if (changedExecutor && this.executors.attached) this.run.clear(this.executors.attached);
    this.executors.attached = snap.exec;
    this.executors.client = snap.client ?? this.executors.client;
    this.executors.executorOnline = snap.online;
    this.executors.attachedCount = snap.attached ?? 1;
    rememberExecutor(snap.exec);
    this.docs.replaceAll(snap.docs ?? {}, snap.root);
    this.blackboard.replace(snap.blackboard ?? []);
    this.catalog.replace(snap.catalog ?? []);
    this.run.clear(snap.exec);
    this.run.starting = Boolean(snap.starting);
    if (snap.run) this.run.applyRun({ ...snap.run, exec: snap.exec, event: 'started' });
    if (snap.st) {
      this.run.applySt(snap.exec, snap.st);
      this.run.flush();
      const run = this.run.runs[snap.exec];
      if (run) run.active = snap.st.state !== 0;
    }
    if (snap.details) this.run.applyNd(snap.exec, snap.details as Record<string, Record<string, unknown>>);
    this.log.replace(snap.log?.first ?? 0, snap.log?.lines ?? []);
    this.ui.recovery = [...(snap.recovery ?? [])];
    this.ui.findings = [...(snap.findings ?? [])];
    // Keep the levels shown when their documents are still there (a reconnect); else the tree.
    const keep = !changedExecutor && this.view.stack.every((level) => level.kind !== 'doc' || this.docs.get(level.doc));
    if (!keep || !this.view.stack.length || (this.view.stack[0].kind === 'doc' && this.view.stack[0].doc !== snap.root)) {
      this.showRoot();
    }
    const lost = this.conn?.takeLostEdits() ?? [];
    if (lost.length) {
      this.ui.lostEdits = lost;
      this.ui.notify('warning', `The connection broke before these changes were confirmed; they were lost: ${lost.join('; ')}`, 20000);
    }
  }

  /** Show the session's tree, centred on the view saved in it. */
  showRoot(): void {
    const root = this.docs.root;
    this.view.reset(root ? { kind: 'doc', doc: root, via: null, prefix: '' } : null);
    const doc = this.docs.get(root);
    if (doc?.view) this.view.centerOn(doc.view.center?.[0] ?? 0, doc.view.center?.[1] ?? 0, doc.view.zoom ?? 1);
  }

  private onDoc(message: Incoming): void {
    const key = String(message.doc);
    const document = message.document as TreeDocument | null;
    const event = String(message.event);
    if (event === 'recovered' || event === 'recovery_declined') {
      // Another tab answered the recovery offer (contract 5.6): it is no longer open here.
      this.ui.recovery = this.ui.recovery.filter((item) => item !== key);
      if (this.ui.question?.testid === 'dialog-recovery' && this.ui.questionKey === key) this.ui.answer(null);
      if (document) this.docs.set(document);
      return;
    }
    if (event === 'closed' || !document) {
      this.docs.remove(key);
      this.dropLevelsOf(key);
      return;
    }
    if (event === 'renamed' && document.key !== key) {
      this.docs.remove(key);
      for (const level of this.view.stack) if (level.kind === 'doc' && level.doc === key) level.doc = document.key;
      if (this.docs.root === key) this.docs.root = document.key;
    }
    this.docs.set(document);
    if (event === 'reverted') this.ui.notify('info', `${document.key} was reverted to its file.`);
    if (event === 'opened' && this.docs.root === null) this.docs.root = document.key;
  }

  private dropLevelsOf(key: string): void {
    const index = this.view.stack.findIndex((level) => level.kind === 'doc' && level.doc === key);
    if (index > 0) while (this.view.stack.length > index) this.view.pop();
    else if (index === 0) this.showRoot();
  }

  private onOp(message: Incoming): void {
    const key = String(message.doc);
    const op = message.op as Op;
    const rev = typeof message.rev === 'number' ? message.rev : undefined;
    let applied = op;
    if (op.op === 'node.move' && this.dragging.size && op.positions) {
      // This tab drags these nodes: their positions here are newer than the broadcast's.
      const positions = { ...(op.positions as Record<string, unknown>) };
      for (const id of this.dragging) delete positions[id];
      applied = { ...op, positions };
    }
    const result = this.docs.apply(key, applied, rev, message.by !== 'executor');
    if (result === 'resync') this.conn?.send({ t: 'resync', doc: key });
  }

  private onRun(message: RunMessage): void {
    this.run.applyRun(message);
    if (message.exec === this.executors.attached && message.event === 'started') this.log.clear();
    if (message.exec === this.executors.attached && message.event === 'ended') {
      const outcome = ['', 'Succeeded', 'Failed', 'Stopped', 'Error'][message.outcome ?? 0] ?? '';
      const detail = message.message && message.message.toLowerCase() !== outcome.toLowerCase() ? ` (${message.message})` : '';
      if (outcome) this.ui.notify(message.outcome === 4 ? 'error' : 'info', `Execution finished: ${outcome}${detail}`);
    }
  }

  // ------------------------------------------------------------------------------ state

  /** The executor whose run the shown level reads statuses from. */
  get statusExec(): string | null {
    const level = this.view.level;
    return level?.kind === 'bundle' ? level.exec : this.executors.attached;
  }

  /** The document shown, or for a run's tree a read-only copy built from the bundle. */
  get shownDoc(): TreeDocument | undefined {
    const level = this.view.level;
    if (!level) return undefined;
    if (level.kind === 'doc') return this.docs.get(level.doc);
    const bundle = this.run.run(level.exec).bundle;
    return bundle ? bundleDoc(bundle, level.tree) : undefined;
  }

  get readOnly(): boolean {
    return this.view.level?.kind === 'bundle';
  }

  /**
   * The outcome of the last run, when it ran the tree shown now: after New or Load the outcome
   * of the earlier tree's run is not this tree's (the gateway names a run after its tree).
   */
  get lastOutcome(): number {
    const run = this.run.run(this.executors.attached);
    return !run.label || run.label === this.docs.root ? run.outcome : 0;
  }

  /** Edits need the gateway: none while offline, none on a run's tree. */
  get canEdit(): boolean {
    return this.executors.online && !this.readOnly;
  }

  /** The structure is locked while the tree executes or starts (design 9.3, 9.5). */
  get locked(): boolean {
    const run = this.run.run(this.executors.attached);
    return this.run.starting || run.state !== 0;
  }

  // ------------------------------------------------------------------------------ requests

  request(message: Outgoing): Promise<Reply> {
    if (!this.conn) return Promise.reject(new ConnectionLost(message.t));
    return this.conn.request(message);
  }

  /**
   * Send an edit operation; it is shown at once (`local`) and applied again when the gateway
   * sends it back. A refused operation is reported and the document asked for again.
   */
  async sendOp(doc: string, op: Op, local = true): Promise<Reply | null> {
    const document = this.docs.get(doc);
    if (!document || this.readOnly) return null;
    if (isStructural(op) && this.locked) {
      this.ui.notify('warning', 'The structure cannot be changed while the tree executes.');
      return null;
    }
    const rev = document.rev;
    if (local) this.docs.apply(doc, op);
    let reply: Reply;
    try {
      reply = await this.request({ t: 'op', doc, rev, op });
    } catch {
      return null; // reported after the reconnect
    }
    if (reply.t === 'no') {
      this.ui.notify('warning', `Not applied: ${reply.reason ?? op.op}`);
      this.conn?.send({ t: 'resync', doc });
    } else if (Array.isArray(reply.reorder) && reply.reorder.length) {
      void this.sendOrders(doc, reply.reorder.map(String));
    }
    return reply;
  }

  /** A blackboard operation (contract 5.3); its effect comes back as `bb` messages. */
  async bbOp(op: Op): Promise<boolean> {
    try {
      const reply = await this.request({ t: 'op', doc: '@blackboard', rev: 0, op });
      if (reply.t === 'no') {
        this.ui.notify('warning', `Not applied: ${reply.reason ?? op.op}`);
        this.bbVersion++;
        return false;
      }
      return true;
    } catch {
      this.bbVersion++;
      return false;
    }
  }

  async ask(kind: string, args: Record<string, unknown> = {}): Promise<Reply | null> {
    try {
      return await this.request({ t: 'ask', kind, ...args });
    } catch {
      return null;
    }
  }

  /** Execute, Pause, Resume, Stop, Reset. Execute with findings asks for the go-ahead. */
  async command(name: 'execute' | 'pause' | 'resume' | 'stop' | 'reset', confirm = false): Promise<void> {
    if (name === 'execute') this.run.starting = true;
    let reply: Reply;
    try {
      reply = await this.request(confirm ? { t: 'cmd', name, confirm } : { t: 'cmd', name });
    } catch {
      this.run.starting = false;
      return;
    }
    if (name === 'execute') this.run.starting = false;
    if (reply.t === 'no') {
      const findings = (reply.findings ?? []) as Finding[];
      if (name === 'execute' && findings.length) {
        this.ui.findings = findings;
        this.ui.findingsNeedConfirm = true;
        this.ui.dialog = 'findings';
      } else {
        this.ui.notify('warning', `${name}: ${reply.reason ?? 'refused'}`);
      }
    } else if (name === 'execute') {
      this.ui.findingsNeedConfirm = false;
    }
  }

  /**
   * A file action (contract 5.4). A question in the answer is put to the user and the action
   * sent again with the answer; null when the user backs out or it fails.
   */
  async file(action: string, args: Record<string, unknown> = {}): Promise<Reply | null> {
    let extra: Record<string, unknown> = {};
    for (let round = 0; round < 6; round++) {
      let reply: Reply;
      try {
        reply = await this.request({ t: 'file', action, ...args, ...extra });
      } catch {
        return null;
      }
      if (reply.t === 'ok') return reply;
      const question = reply.question as FileQuestion | undefined;
      if (!question) {
        this.ui.notify('warning', `${action}: ${reply.reason ?? 'refused'}`);
        return null;
      }
      const answer = await this.answerQuestion(question, reply.reason, args);
      if (!answer) return null;
      extra = { ...extra, ...answer };
    }
    return null;
  }

  private async answerQuestion(question: FileQuestion, reason?: string, args: Record<string, unknown> = {}): Promise<Record<string, unknown> | null> {
    if (question.kind === 'unsaved') {
      const answer = await this.ui.ask({
        title: 'Unsaved changes',
        text: `${reason ? reason + '\n' : ''}Save the changes to ${question.docs.join(', ')} first?`,
        buttons: [
          { label: 'Save', value: 'save', primary: true },
          { label: 'Discard', value: 'discard' },
          { label: 'Cancel', value: 'cancel' },
        ],
        testid: 'dialog-unsaved',
      });
      return answer && answer.button !== 'cancel' ? { choice: answer.button } : null;
    }
    if (question.kind === 'replace') {
      const answer = await this.ui.ask({
        title: 'Replace file',
        text: `${question.name} exists. Replace it?`,
        buttons: [
          { label: 'Replace', value: 'yes', primary: true },
          { label: 'Cancel', value: 'cancel' },
        ],
        testid: 'dialog-replace',
      });
      return answer?.button === 'yes' ? { replace: true } : null;
    }
    const list = await this.request({ t: 'file', action: 'list' }).catch(() => null);
    const trees = list?.t === 'ok' ? ((list.trees ?? []) as { key: string }[]).map((tree) => tree.key) : [];
    const answer = await this.ui.ask({
      title: 'Choose the file',
      text: `The file ${question.file || '(none)'} of this node cannot be found. Open an existing tree, or create the tree named below holding only a Root.`,
      buttons: [
        { label: 'Open', value: 'use', primary: true },
        { label: 'Create New Tree', value: 'create' },
        { label: 'Cancel', value: 'cancel' },
      ],
      // Tree names as the file list has them (relative to the trees folder), not as File holds them.
      input: { label: 'Tree', value: (typeof args.doc === 'string' && resolveSubtree(args.doc, question.file)) || treeKeyOf(question.file) },
      choices: trees,
      testid: 'dialog-choose-file',
    });
    const name = answer?.input.trim() ?? '';
    if (!answer || answer.button === 'cancel' || !name) return null;
    // A name that is not a tree yet is created (the widget's offer), never asked about again.
    if (answer.button === 'create' || !trees.includes(treeKeyOf(name))) return { file: name, create: true };
    return { file: name };
  }

  // ------------------------------------------------------------------------------ child order

  /** Centre x and top of a node as drawn (the measured size, or an estimate before drawing). */
  orderItem(node: WireNode): OrderItem {
    const size = this.view.sizes[node.id] ?? estimateSize(node);
    return { id: node.id, cx: node.x + size.w / 2, top: node.y };
  }

  /** The child order of `parent` as the canvas shows it (design 11.4). */
  canvasOrder(doc: TreeDocument, parent: string): string[] {
    const items = childIds(doc, parent)
      .map((id) => findNode(doc, id))
      .filter((node): node is WireNode => !!node)
      .map((node) => this.orderItem(node));
    return childOrder(items);
  }

  /** New orders of the parents of `ids` whose children are no longer in canvas order. */
  ordersFor(doc: TreeDocument, ids: Iterable<string>): Record<string, string[]> {
    const parents = new Set<string>();
    for (const id of ids) {
      const parent = parentId(doc, id);
      if (parent) parents.add(parent);
    }
    const orders: Record<string, string[]> = {};
    for (const parent of parents) {
      const order = this.canvasOrder(doc, parent);
      if (!sameOrder(order, childIds(doc, parent))) orders[parent] = order;
    }
    return orders;
  }

  /** Work the order of `parents` out again and send it (an `ok` with `reorder`). */
  async sendOrders(docKey: string, parents: string[]): Promise<void> {
    const doc = this.docs.get(docKey);
    if (!doc) return;
    const positions: Record<string, [number, number]> = {};
    const orders: Record<string, string[]> = {};
    for (const parent of parents) {
      orders[parent] = this.canvasOrder(doc, parent);
      for (const id of orders[parent]) {
        const node = findNode(doc, id);
        if (node) positions[id] = [node.x, node.y];
      }
    }
    await this.sendOp(docKey, { op: 'node.move', positions, orders });
  }

  // ------------------------------------------------------------------------------ zooming

  /** Zoom In on a Sub-Tree (or Remote Sub-Tree) node of the level shown. */
  async zoomIn(node: WireNode): Promise<void> {
    const level = this.view.level;
    if (!level) return;
    const path = this.view.pathOf(node.id);
    if (level.kind === 'bundle') {
      // Inside a run's tree: follow the bundle.
      const tree = typeof node.tree === 'string' ? node.tree : null;
      const bundle = this.run.run(level.exec).bundle;
      if (tree && bundle?.trees[tree] && node.type === 'Sub-Tree') {
        this.view.push({ kind: 'bundle', exec: level.exec, tree, via: node.id, prefix: path, title: tree, watched: level.watched });
        this.fitShown();
      }
      return;
    }
    const exec = this.executors.attached;
    const details = this.run.detailsOf(exec, path);
    const remote = details?.remote_run as { executor?: string; run_id?: string } | undefined;
    if (node.type === 'RemoteSubTree' && this.locked && remote?.executor) {
      // During a run: the remote run, read-only (design 6.8).
      this.watch(remote.executor);
      this.view.push({ kind: 'bundle', exec: remote.executor, tree: '', via: node.id, prefix: '', title: `${remote.executor} (remote run)`, watched: true });
      return;
    }
    const reply = await this.file('zoom_in', { doc: level.doc, node: node.id, path });
    if (!reply) return;
    const key = String(reply.doc);
    if (reply.document) this.docs.set(reply.document as TreeDocument);
    this.view.push({ kind: 'doc', doc: key, via: node.id, prefix: node.type === 'RemoteSubTree' ? '' : path });
    const doc = this.docs.get(key);
    if (doc?.view) this.view.centerOn(doc.view.center?.[0] ?? 0, doc.view.center?.[1] ?? 0, doc.view.zoom ?? 1);
  }

  /** Go Back: only this tab's view changes; nothing is discarded (design 9.2). */
  goBack(): void {
    const level = this.view.level;
    if (!level || this.view.stack.length <= 1) return;
    if (level.kind === 'doc') void this.request({ t: 'file', action: 'go_back', doc: level.doc }).catch(() => null);
    const below = this.view.stack[this.view.stack.length - 2];
    if (level.kind === 'bundle' && level.watched && !(below.kind === 'bundle' && below.watched)) this.unwatch();
    this.view.pop();
  }

  /** Show a foreign run (one the session did not start) read-only (design 4.5). */
  showRun(exec: string): void {
    const bundle = this.run.run(exec).bundle;
    if (!bundle) return;
    this.view.push({ kind: 'bundle', exec, tree: bundle.root, via: null, prefix: '', title: `run ${this.run.run(exec).label || ''}`.trim(), watched: false });
    this.fitShown();
  }

  watch(executor: string): void {
    this.watched = executor;
    this.conn?.send({ t: 'watch', executor });
  }

  unwatch(): void {
    if (this.watched) this.run.clear(this.watched);
    this.watched = null;
    this.conn?.send({ t: 'unwatch' });
  }

  /** Centre the view on the nodes shown. */
  fitShown(): void {
    const doc = this.shownDoc;
    if (!doc?.nodes.length) {
      this.view.centerOn(0, 0, 1);
      return;
    }
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const node of doc.nodes) {
      const size = this.view.sizes[node.id] ?? estimateSize(node);
      minX = Math.min(minX, node.x);
      minY = Math.min(minY, node.y);
      maxX = Math.max(maxX, node.x + size.w);
      maxY = Math.max(maxY, node.y + size.h);
    }
    this.view.centerOn((minX + maxX) / 2, (minY + maxY) / 2, this.view.zoom);
  }

  /** The entries a blackboard-aware editor lists. */
  get entries(): Entry[] {
    return this.blackboard.entries;
  }
}

/** A read-only document for one tree of a run's bundle. */
export function bundleDoc(bundle: Bundle, key: string): TreeDocument | undefined {
  const tree = bundle.trees[key || bundle.root];
  if (!tree) return undefined;
  return {
    key: key || bundle.root, path: '', rev: 0, version: 1, unsaved: false, config: tree.config ?? {},
    view: { zoom: 1, center: [0, 0] }, nodes: tree.nodes ?? [], connections: tree.connections ?? [], problems: [],
  };
}

function readWantedExecutor(): string | null {
  const fromHash = typeof location === 'undefined' ? undefined : /(?:^|[#&])exec=([^&]+)/.exec(location.hash)?.[1];
  if (fromHash) return decodeURIComponent(fromHash);
  try {
    return localStorage.getItem('bt.executor');
  } catch {
    return null;
  }
}

function rememberExecutor(name: string): void {
  try {
    localStorage.setItem('bt.executor', name);
  } catch {
    // A private window: the executor is asked for again next time.
  }
}

export const session = new Session();

export type { Level };
