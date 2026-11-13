// A small stand-in for the gateway, for developing the page without one (a dev tool only).
// It speaks contract 5 over /ws with an in-memory tree store, one executor and a fake run
// that cycles the statuses of the tree's nodes. Start it with `npm run mock`, then `npm run dev`.
//
// It checks little: enough to drive every panel of the page, not a model of the gateway.

import { randomBytes } from 'node:crypto';
import { createServer } from 'node:http';
import { WebSocketServer } from 'ws';

const PORT = Number(process.env.PORT ?? 8765);
const EXEC = '/bt_executor';
const OTHER = '/robot2/bt_executor';
const hex = () => randomBytes(16).toString('hex');
const now = () => new Date().toISOString().replace('T', ' ').slice(0, 23);

const F = (name, kind, def, doc = '') => ({ name, kind, default: def, doc });
const T = (type, title, kind, category, fields = [], special = null, extra = {}) => ({
  type, title, kind, category, fields, leaf_kind: kind === 'leaf' ? 'instant' : null, special,
  waits_for_cancels: false, needs_ros: false, builtin: true, doc: '', ...extra,
});
const CATALOG = [
  T('Root', 'Root', 'root', 'Control'),
  T('Sequence', 'Sequence', 'composite', 'Control'),
  T('Selector', 'Selector', 'composite', 'Control'),
  T('Parallel', 'Parallel', 'composite', 'Control'),
  T('Negation', 'Negation', 'decorator', 'Control'),
  T('Evaluation', 'Evaluation', 'leaf', 'Blackboard', [F('ValueName', 'str', ''), F('CompareTo', 'entry', null), F('LiteralValue', 'other', null), F('Operator', 'str', '=='), F('Wait', 'bool', false), F('WaitTimeout', 'float', 0.0)], 'evaluation'),
  T('Set', 'Set', 'leaf', 'Blackboard', [F('ValueName', 'str', ''), F('CompareTo', 'entry', null), F('LiteralValue', 'other', null)], 'set'),
  T('Restart', 'Restart', 'leaf', 'Execution'),
  T('Pause', 'Pause', 'leaf', 'Execution'),
  T('Stop', 'Stop', 'leaf', 'Execution'),
  T('UDP Message', 'UDP Message', 'leaf', 'Messages', [F('Destination IP', 'str', '127.0.0.1'), F('Destination Port', 'int', 5005), F('Message', 'str', '')]),
  T('Print Message', 'Print Message', 'leaf', 'Messages', [F('Level', 'int', 3), F('Source', 'entry', null), F('Message', 'str', '')], 'print'),
  T('Sub-Tree', 'Sub-Tree', 'leaf', 'Sub-Trees', [F('File', 'str', '')], 'subtree'),
  T('Notes', 'Notes', 'note', 'Annotation'),
  T('Group', 'Group', 'group', 'Annotation'),
  T('CallAction', 'Call Action', 'leaf', 'ROS', [F('Action', 'str', ''), F('Type', 'str', ''), F('Goal', 'other', {}), F('Timeout', 'float', 0.0), F('Feedback Timeout', 'float', 0.0)], 'call_action', { needs_ros: true }),
  T('CallService', 'Call Service', 'leaf', 'ROS', [F('Service', 'str', ''), F('Type', 'str', ''), F('Request', 'other', {}), F('Timeout', 'float', 5.0), F('Success Field', 'str', 'success')], 'call_service', { needs_ros: true }),
  T('Publish', 'Publish', 'leaf', 'ROS', [F('Topic', 'str', ''), F('Type', 'str', ''), F('Message', 'other', {}), F('Wait For Subscribers', 'int', 0), F('Keep For Late Joiners', 'bool', false)], 'publish', { needs_ros: true }),
  T('RemoteSubTree', 'Remote Sub-Tree', 'leaf', 'Sub-Trees', [F('Executor', 'str', ''), F('File', 'str', ''), F('Timeout', 'float', 0.0), F('Watchdog', 'float', 5.0)], 'remote_subtree', { needs_ros: true }),
  T('MoveTo', 'Move To', 'leaf', 'Custom', [F('x', 'float', 0.0), F('y', 'float', 0.0), F('speed', 'int', 5), F('mode', 'choice', ['walk', 'run']), F('avoid_obstacles', 'bool', true)], null, { builtin: false }),
];

// ------------------------------------------------------------------------------------ store

function newDoc(key, withExample = false) {
  const root = { id: hex(), type: 'Root', title: 'Root', x: 0, y: 0, composite: 'Sequence', memory: true };
  const doc = {
    key, path: `${key}_BT.json`, rev: 1, version: 1, unsaved: false,
    config: { tick_interval_ms: 100, repeat: false, restore_blackboard: false, default_memory: true },
    view: { zoom: 1, center: [0, 150] }, nodes: [root], connections: [], problems: [],
  };
  if (withExample) {
    const seq = { id: hex(), type: 'Sequence', title: 'Sequence', x: -60, y: 140, memory: true };
    const a = { id: hex(), type: 'Print Message', title: 'Hello', x: -300, y: 300, fields: { Level: 3, Source: null, Message: 'hello' } };
    const b = { id: hex(), type: 'MoveTo', title: 'Move To', x: 0, y: 300, fields: { x: '1.5', y: '0.0', speed: 5, mode: ['walk', 'run'], avoid_obstacles: true }, field_selections: { mode: 0 } };
    const c = { id: hex(), type: 'Evaluation', title: 'Battery ok', x: 320, y: 300, fields: { ValueName: 'battery', CompareTo: null, LiteralValue: { type: 'Double', value: '20.0' }, Operator: '>=', Wait: false, WaitTimeout: '0.0' } };
    const sub = { id: hex(), type: 'Sub-Tree', title: 'patrol', x: 300, y: 140, fields: { File: 'patrol_BT.json' } };
    const note = { id: hex(), type: 'Notes', title: 'Notes', x: 380, y: -40, text: 'Mock gateway tree:\nedit, connect and Execute', width: 220, height: 90 };
    const group = { id: hex(), type: 'Group', title: 'group_001', x: -330, y: 260, width: 580, height: 230, color: '#3d7cc9' };
    doc.nodes.push(seq, a, b, c, sub, note, group);
    doc.connections.push({ parent: root.id, child: seq.id }, { parent: root.id, child: sub.id }, { parent: seq.id, child: a.id }, { parent: seq.id, child: b.id }, { parent: seq.id, child: c.id });
  }
  return doc;
}

const files = new Map(); // key -> saved document (file form is not modelled: wire form kept)
const open = new Map(); // key -> document in memory
files.set('robot', newDoc('robot', true));
files.set('patrol', newDoc('patrol'));
open.set('robot', structuredClone(files.get('robot')));
let rootKey = 'robot';
const entry = (name, type, value, extra = {}) => ({ id: hex(), name, type, value, reset: false, source: null, freshness: null, age: null, truncated: false, ...extra });
let blackboard = [
  entry('battery', 'Double', '80.0'),
  entry('counter', 'Integer', 3),
  entry('mode', 'String', 'patrol'),
  entry('waypoints', 'List', "[1, 2.5, 'dock']"),
  entry('door_open', 'Bool', false, { source: { topic: '/door', type: 'std_msgs/msg/Bool', field: 'data', max_rate_hz: 2, max_age_s: 5 }, freshness: 'nodata' }),
];

// ------------------------------------------------------------------------------------ clients

const clients = new Set();
function send(ws, message) {
  if (ws.readyState === 1) ws.send(JSON.stringify(message));
}
function broadcast(message) {
  for (const client of clients) if (client.attached === EXEC) send(client.ws, message);
}
function presence() {
  broadcast({ t: 'presence', attached: [...clients].filter((c) => c.attached === EXEC).length });
}

// ------------------------------------------------------------------------------------ run

const run = { id: null, state: 0, loop: 0, cancelling: 0, outcome: 0, message: '', paths: [], v: '', timer: null, step: 0, details: {}, log: [] };

function nodePaths(doc, prefix = '', seen = new Set([doc.key])) {
  const root = doc.nodes.find((n) => n.type === 'Root');
  if (!root) return [];
  const result = [];
  const walk = (id) => {
    const node = doc.nodes.find((n) => n.id === id);
    if (!node) return;
    const path = prefix ? `${prefix}/${id}` : id;
    result.push(path);
    if (node.type === 'Sub-Tree') {
      const key = String(node.fields?.File ?? '').replace(/_BT\.json$/, '');
      const sub = open.get(key) ?? files.get(key);
      if (sub && !seen.has(key)) result.push(...nodePaths(sub, path, new Set([...seen, key])));
      return;
    }
    for (const c of doc.connections) if (c.parent === id) walk(c.child);
  };
  walk(root.id);
  return result;
}

function st() {
  broadcast({ t: 'st', exec: EXEC, run_id: run.id, state: run.state, loop: run.loop, cancelling: run.cancelling, unsettled: run.cancelling, outcome: run.outcome, message: run.message, v: run.v });
}

function logLine(level, text) {
  const line = [now(), level, text];
  run.log.push(line);
  broadcast({ t: 'log', exec: EXEC, first: run.log.length - 1, lines: [line] });
}

function tick() {
  if (run.state !== 1) return;
  const n = run.paths.length;
  const digits = new Array(n).fill('0');
  const k = run.step % (n + 1);
  for (let i = 0; i < n; i++) digits[i] = i < k ? (i === n - 1 && run.loop % 3 === 2 ? '3' : '2') : i === k ? '1' : '0';
  run.v = digits.join('');
  if (k < n) {
    logLine(4, `Node '${run.paths[k].split('/').pop().slice(0, 8)}' started: Running`);
    run.details = { [k]: { waiting: `step ${run.step}` } };
    broadcast({ t: 'nd', exec: EXEC, details: { [k]: { waiting: `step ${run.step}` }, [Math.max(0, k - 1)]: { waiting: null } } });
  }
  st();
  run.step++;
  if (k === n) {
    const config = open.get(rootKey)?.config ?? {};
    if (config.repeat) {
      run.loop++;
      run.step = 0;
    } else finish(1, '');
  }
}

function startRun(client) {
  const doc = open.get(rootKey);
  run.id = hex().slice(0, 16);
  run.paths = nodePaths(doc);
  run.state = 1;
  run.loop = 0;
  run.step = 0;
  run.outcome = 0;
  run.log = [];
  broadcast({ t: 'run', exec: EXEC, event: 'started', run_id: run.id, label: rootKey, requested_by: client.address, node_paths: run.paths, own: true, bundle: null });
  logLine(4, 'Execution started');
  run.timer = setInterval(tick, 500);
  tick();
}

function finish(outcome, message) {
  clearInterval(run.timer);
  run.timer = null;
  run.state = 0;
  run.outcome = outcome;
  run.message = message;
  logLine(4, `Execution finished: ${['', 'Succeeded', 'Failed', 'Stopped', 'Error'][outcome]}`);
  st();
  broadcast({ t: 'run', exec: EXEC, event: 'ended', run_id: run.id, label: rootKey, requested_by: '', node_paths: run.paths, own: true, bundle: null, outcome, message });
}

function stopRun() {
  clearInterval(run.timer);
  run.state = 3;
  run.cancelling = 1;
  const running = run.v.indexOf('1');
  run.v = run.v.replace(/[123]/g, '0');
  if (running >= 0) run.v = run.v.slice(0, running) + '4' + run.v.slice(running + 1);
  st();
  setTimeout(() => {
    run.cancelling = 0;
    run.v = '0'.repeat(run.paths.length);
    finish(3, 'stopped');
  }, 1500);
}

// ------------------------------------------------------------------------------------ ops

const STRUCTURAL = new Set(['node.add', 'node.remove', 'conn.add', 'conn.remove', 'paste', 'group.promote', 'composite.set_type', 'composite.set_memory', 'composite.set_policy']);

function isAncestor(doc, ancestor, id) {
  for (let current = id, guard = 0; current && guard < 10000; guard++) {
    if (current === ancestor) return true;
    current = doc.connections.find((c) => c.child === current)?.parent;
  }
  return false;
}

function setOrder(doc, parent, ids) {
  const slots = [];
  const mine = [];
  doc.connections.forEach((c, i) => c.parent === parent && (slots.push(i), mine.push(c)));
  mine.sort((a, b) => ids.indexOf(a.child) - ids.indexOf(b.child));
  slots.forEach((slot, i) => (doc.connections[slot] = mine[i]));
}

/** Apply an op to a document; returns a reason when it is refused. */
function applyDocOp(doc, op, rev, reply) {
  const node = op.id ? doc.nodes.find((n) => n.id === op.id) : null;
  if (STRUCTURAL.has(op.op) && run.state !== 0) return 'the structure is locked while the tree executes';
  switch (op.op) {
    case 'node.add':
      if (doc.nodes.some((n) => n.id === op.node.id)) return 'that id is in use';
      doc.nodes.push(op.node);
      break;
    case 'node.remove': {
      const ids = new Set(op.ids);
      if (doc.nodes.some((n) => ids.has(n.id) && n.type === 'Root')) return 'the Root cannot be removed';
      doc.nodes = doc.nodes.filter((n) => !ids.has(n.id));
      doc.connections = doc.connections.filter((c) => !ids.has(c.parent) && !ids.has(c.child));
      break;
    }
    case 'node.move':
      for (const [id, [x, y]] of Object.entries(op.positions ?? {})) {
        const target = doc.nodes.find((n) => n.id === id);
        if (target) Object.assign(target, { x, y });
      }
      if (op.orders && rev !== doc.rev) {
        reply.reorder = Object.keys(op.orders);
        delete op.orders;
      } else for (const [parent, ids] of Object.entries(op.orders ?? {})) setOrder(doc, parent, ids);
      break;
    case 'node.set_title':
    case 'node.set_text':
    case 'node.set_color':
      if (!node) return 'no such node';
      node[{ 'node.set_title': 'title', 'node.set_text': 'text', 'node.set_color': 'color' }[op.op]] = op[{ 'node.set_title': 'title', 'node.set_text': 'text', 'node.set_color': 'color' }[op.op]];
      break;
    case 'node.set_field':
      if (!node) return 'no such node';
      node.fields ??= {};
      if (op.path) return 'nested field edits are not modelled by the mock';
      node.fields[op.field] = op.value;
      break;
    case 'node.set_selection':
      if (!node) return 'no such node';
      node.field_selections = { ...(node.field_selections ?? {}), [op.field]: op.index };
      break;
    case 'node.set_binding':
    case 'node.set_output': {
      if (!node) return 'no such node';
      const key = op.op === 'node.set_binding' ? 'bindings' : 'outputs';
      node[key] = { ...(node[key] ?? {}) };
      if (op.entry === null) delete node[key][op.key];
      else node[key][op.key] = op.entry;
      break;
    }
    case 'node.set_option':
      if (!node) return 'no such node';
      if (op.value === null) delete node[op.name];
      else node[op.name] = op.value;
      break;
    case 'node.resize':
      if (!node) return 'no such node';
      Object.assign(node, { width: op.width, height: op.height }, op.x !== undefined ? { x: op.x, y: op.y } : {});
      break;
    case 'composite.set_type':
      if (!node) return 'no such node';
      if (node.type === 'Root') {
        if (op.composite === 'Parallel') return 'the Root is a Sequence or a Selector';
        node.composite = op.composite;
      } else {
        if (node.title === node.type) node.title = op.composite;
        node.type = op.composite;
        if (op.composite === 'Parallel') (delete node.memory, (node.policy ??= 'all'));
        else (delete node.policy, (node.memory ??= true));
      }
      break;
    case 'composite.set_memory':
      node.memory = op.memory;
      break;
    case 'composite.set_policy':
      node.policy = op.policy;
      break;
    case 'conn.add': {
      if (isAncestor(doc, op.child, op.parent)) return 'that connection would make a cycle';
      doc.connections = doc.connections.filter((c) => c.child !== op.child);
      if (doc.nodes.find((n) => n.id === op.parent)?.type === 'Negation') doc.connections = doc.connections.filter((c) => c.parent !== op.parent);
      doc.connections.push({ parent: op.parent, child: op.child });
      if (op.order) setOrder(doc, op.parent, op.order);
      break;
    }
    case 'conn.remove':
      doc.connections = doc.connections.filter((c) => !(c.parent === op.parent && c.child === op.child));
      break;
    case 'paste': {
      let highest = Math.max(0, ...doc.nodes.map((n) => (n.type === 'Group' ? Number(/^group_(\d+)$/.exec(n.title)?.[1] ?? 0) : 0)));
      for (const n of op.nodes) {
        if (n.type === 'Group' && /^group_\d+$/.test(n.title)) n.title = `group_${String(++highest).padStart(3, '0')}`;
        doc.nodes.push(n);
      }
      doc.connections.push(...op.connections);
      break;
    }
    case 'doc.set_config':
      Object.assign(doc.config, op.config);
      break;
    case 'group.promote':
      return 'Promote to Sub-Tree is not modelled by the mock';
    default:
      return `unknown operation ${op.op}`;
  }
  doc.rev += 1;
  doc.unsaved = true;
  return null;
}

function applyBbOp(op) {
  const changed = [];
  const find = (id) => blackboard.find((e) => e.id === id);
  switch (op.op) {
    case 'bb.add':
      if (blackboard.some((e) => e.name === op.entry.name)) return 'an entry of that name exists';
      blackboard.push(entry(op.entry.name, op.entry.type, op.entry.value, { id: op.entry.id ?? hex(), reset: !!op.entry.reset }));
      changed.push(blackboard.at(-1));
      break;
    case 'bb.remove':
      blackboard = blackboard.filter((e) => !op.ids.includes(e.id));
      broadcast({ t: 'bb', exec: EXEC, entries: [], removed: op.ids, order: null });
      return null;
    case 'bb.rename':
    case 'bb.retype':
    case 'bb.set_value':
    case 'bb.set_reset':
    case 'bb.set_source': {
      const target = find(op.id);
      if (!target) return 'no such entry';
      if (op.op === 'bb.set_value' && target.source) return 'the topic owns the value of this entry';
      if (op.op === 'bb.set_value' && target.type === 'Double' && Number.isNaN(Number(op.value)) && !/^-?(nan|inf)$/i.test(op.value)) return 'not a number';
      const field = { 'bb.rename': 'name', 'bb.retype': 'type', 'bb.set_value': 'value', 'bb.set_reset': 'reset', 'bb.set_source': 'source' }[op.op];
      target[field] = op[field === 'value' ? 'value' : field];
      if (op.op === 'bb.set_source') target.freshness = op.source ? 'nodata' : null;
      changed.push(target);
      break;
    }
    case 'bb.move': {
      const rank = new Map(op.order.map((id, i) => [id, i]));
      blackboard.sort((a, b) => (rank.get(a.id) ?? 1e9) - (rank.get(b.id) ?? 1e9));
      broadcast({ t: 'bb', exec: EXEC, entries: [], removed: [], order: op.order });
      return null;
    }
    default:
      return `unknown operation ${op.op}`;
  }
  broadcast({ t: 'bb', exec: EXEC, entries: changed, removed: [], order: null });
  return null;
}

// ------------------------------------------------------------------------------------ files

function keyOf(name) {
  return String(name).replace(/\\/g, '/').replace(/(_BT|_BB)?\.json$/, '');
}

function fileAction(client, m) {
  const ok = (extra = {}) => send(client.ws, { t: 'ok', cid: m.cid, ...extra });
  const no = (reason, extra = {}) => send(client.ws, { t: 'no', cid: m.cid, reason, ...extra });
  switch (m.action) {
    case 'list':
      return ok({ trees: [...files.keys()].map((key) => ({ key, dirs: [], mtime: Date.now() / 1000, open_in: open.has(key) ? EXEC : null })), dirs: ['cells'] });
    case 'new':
    case 'load': {
      const key = keyOf(m.name);
      if (run.state !== 0) return no('a tree executes');
      if (m.action === 'load' && !files.has(key)) return no(`there is no tree named ${key}`);
      if (m.action === 'new' && files.has(key) && !m.replace) return no('the file exists', { question: { kind: 'replace', name: key } });
      const unsaved = [...open.values()].filter((d) => d.unsaved).map((d) => d.key);
      if (unsaved.length && !m.choice) return no('unsaved changes', { question: { kind: 'unsaved', docs: unsaved } });
      if (m.choice === 'save') for (const k of unsaved) files.set(k, { ...structuredClone(open.get(k)), unsaved: false });
      if (m.action === 'new') files.set(key, newDoc(key));
      open.clear();
      open.set(key, structuredClone(files.get(key)));
      rootKey = key;
      blackboard = m.action === 'new' ? [] : blackboard;
      ok();
      for (const c of clients) if (c.attached === EXEC && c !== client) send(c.ws, snapshot(c));
      return;
    }
    case 'save':
    case 'save_as': {
      const doc = open.get(m.doc);
      if (!doc) return no('that document is not open');
      if (m.view) doc.view = m.view;
      doc.unsaved = false;
      if (m.action === 'save_as') {
        const key = keyOf(m.name);
        if (files.has(key) && !m.replace) return no('the file exists', { question: { kind: 'replace', name: key } });
        open.delete(doc.key);
        doc.key = key;
        doc.path = `${key}_BT.json`;
        open.set(key, doc);
        if (rootKey === m.doc) rootKey = key;
        broadcast({ t: 'doc', doc: m.doc, event: 'renamed', document: doc });
      }
      files.set(doc.key, structuredClone(doc));
      broadcast({ t: 'doc', doc: doc.key, event: 'saved', document: doc });
      return ok({ warnings: [] });
    }
    case 'rename':
      if (!files.has(keyOf(m.name))) return no('no such tree');
      files.set(keyOf(m.new_name), files.get(keyOf(m.name)));
      files.delete(keyOf(m.name));
      return ok();
    case 'delete':
      if (open.has(keyOf(m.name))) return no('the tree is open');
      files.delete(keyOf(m.name));
      return ok();
    case 'zoom_in': {
      const doc = open.get(m.doc);
      const node = doc?.nodes.find((n) => n.id === m.node);
      if (!node) return no('no such node');
      const key = keyOf(m.file ?? node.fields?.File ?? '');
      if (!files.has(key) && !open.has(key)) {
        if (!m.file) return no('the file is missing', { question: { kind: 'choose_file', node: m.node, file: String(node.fields?.File ?? '') } });
        files.set(key, newDoc(key));
      }
      if (!open.has(key)) open.set(key, structuredClone(files.get(key)));
      return ok({ doc: key, document: open.get(key) });
    }
    case 'go_back':
      return ok();
    case 'revert': {
      const saved = files.get(m.doc);
      if (!saved) return no('no file');
      open.set(m.doc, structuredClone(saved));
      broadcast({ t: 'doc', doc: m.doc, event: 'reverted', document: open.get(m.doc) });
      return ok();
    }
    case 'recover':
      return ok();
    default:
      return no(`unknown file action ${m.action}`);
  }
}

// ------------------------------------------------------------------------------------ questions

const INTERFACE = {
  goal: [{ name: 'pose', kind: 'message', fields: [
    { name: 'header', kind: 'message', fields: [{ name: 'frame_id', kind: 'string' }] },
    { name: 'pose', kind: 'message', fields: [
      { name: 'position', kind: 'message', fields: [{ name: 'x', kind: 'float64' }, { name: 'y', kind: 'float64' }, { name: 'z', kind: 'float64' }] },
      { name: 'orientation', kind: 'message', fields: [{ name: 'w', kind: 'float64' }] },
    ] },
  ] }, { name: 'behavior_tree', kind: 'string' }],
  result: [{ name: 'error_code', kind: 'uint16' }],
  feedback: [{ name: 'distance_remaining', kind: 'float32' }, { name: 'number_of_recoveries', kind: 'int16' }],
};

function ask(client, m) {
  const ok = (extra = {}) => send(client.ws, { t: 'ok', cid: m.cid, ...extra });
  const no = (reason) => send(client.ws, { t: 'no', cid: m.cid, reason });
  switch (m.kind) {
    case 'graph':
      return ok({ graph: { actions: [['/robot1/navigate_to_pose', 'nav2_msgs/action/NavigateToPose']], services: [['/robot1/dock', 'std_srvs/srv/Trigger']], topics: [['/door', 'std_msgs/msg/Bool'], ['/robot1/battery_state', 'sensor_msgs/msg/BatteryState']] } });
    case 'interface':
      return ok({ interface: INTERFACE });
    case 'sample':
      return ok({ value: { type: 'Bool', value: true }, message: 'data: true' });
    case 'parse_literal': {
      const text = String(m.text ?? '').trim();
      const balanced = [...text].reduce((depth, ch) => depth + ('[{('.includes(ch) ? 1 : ')}]'.includes(ch) ? -1 : 0), 0) === 0;
      if (!text || !balanced || /__|import|\bos\b/.test(text)) return no('not a Python literal');
      return ok({ value: text });
    }
    case 'entry_value':
      return ok({ value: blackboard.find((e) => e.id === m.id)?.value ?? '' });
    case 'log_before': {
      const end = Math.max(0, Math.min(Number(m.before), run.log.length));
      const start = Math.max(0, end - Number(m.count ?? 100));
      return ok({ first: start, lines: run.log.slice(start, end) });
    }
    case 'executors':
      return ok({ executors: executors() });
    default:
      return no(`unknown question ${m.kind}`);
  }
}

// ------------------------------------------------------------------------------------ protocol

function executors() {
  return [{ name: EXEC, online: true, state: run.state }, { name: OTHER, online: false, state: 0 }];
}

function snapshot(client) {
  return {
    t: 'snapshot', exec: EXEC, client: client.id, root: rootKey, docs: Object.fromEntries(open), blackboard, catalog: CATALOG,
    online: true, starting: false,
    run: run.id ? { exec: EXEC, event: 'started', run_id: run.id, label: rootKey, requested_by: '', node_paths: run.paths, own: true, bundle: null } : null,
    st: { run_id: run.id, state: run.state, loop: run.loop, cancelling: run.cancelling, unsettled: run.cancelling, outcome: run.outcome, message: run.message, v: run.v },
    details: run.details, log: { first: Math.max(0, run.log.length - 500), lines: run.log.slice(-500) },
    recovery: [], attached: [...clients].filter((c) => c.attached === EXEC).length, findings: [],
  };
}

function receive(client, m) {
  switch (m.t) {
    case 'hello':
      if (m.protocol !== 1) return send(client.ws, { t: 'reload' });
      return send(client.ws, { t: 'welcome', protocol: 1, client: client.id, executors: executors(), time: Date.now() / 1000, local: true });
    case 'attach':
      client.attached = m.executor;
      if (m.executor !== EXEC) return send(client.ws, { t: 'notice', level: 'warning', text: `${m.executor} is offline (mock)` });
      send(client.ws, snapshot(client));
      return presence();
    case 'op': {
      if (m.doc === '@blackboard') {
        const reason = applyBbOp(m.op);
        return send(client.ws, reason ? { t: 'no', cid: m.cid, reason } : { t: 'ok', cid: m.cid });
      }
      const doc = open.get(m.doc);
      if (!doc) return send(client.ws, { t: 'no', cid: m.cid, reason: 'that document is not open' });
      const reply = { t: 'ok', cid: m.cid };
      const op = structuredClone(m.op);
      const reason = applyDocOp(doc, op, m.rev, reply);
      if (reason) return send(client.ws, { t: 'no', cid: m.cid, reason });
      reply.rev = doc.rev;
      send(client.ws, reply);
      return broadcast({ t: 'op', doc: m.doc, rev: doc.rev, op, by: client.id, cid: m.cid });
    }
    case 'cmd': {
      const ok = () => send(client.ws, { t: 'ok', cid: m.cid });
      const no = (reason, extra = {}) => send(client.ws, { t: 'no', cid: m.cid, reason, ...extra });
      if (m.name === 'execute') {
        if (run.state !== 0) return no('a tree executes');
        const doc = open.get(rootKey);
        const findings = doc.nodes.filter((n) => n.type === 'Sub-Tree' && !n.fields?.File).map((n) => ({ path: `tree:${rootKey}/${n.id}`, severity: 'warning', text: `'${n.title}' has no file` }));
        if (findings.length && !m.confirm) return no('validation found problems', { findings });
        ok();
        return setTimeout(() => startRun(client), 300);
      }
      if (m.name === 'pause' && run.state === 1) (run.state = 2, st());
      else if (m.name === 'resume' && run.state === 2) (run.state = 1, st());
      else if (m.name === 'stop' && (run.state === 1 || run.state === 2)) stopRun();
      else if (m.name === 'reset') ((run.step = 0), (run.v = '0'.repeat(run.paths.length)), st());
      else return no(`${m.name} is refused in this state`);
      return ok();
    }
    case 'file':
      return fileAction(client, m);
    case 'ask':
      return ask(client, m);
    case 'resync':
      return send(client.ws, { t: 'doc', doc: m.doc, event: 'resync', document: open.get(m.doc) ?? null });
    case 'watch':
    case 'unwatch':
      return;
    default:
      return send(client.ws, { t: 'notice', level: 'warning', text: `unknown message ${m.t}` });
  }
}

const server = createServer((req, res) => {
  if (req.method === 'POST' && req.url === '/upload') {
    req.resume();
    req.on('end', () => (res.writeHead(200), res.end('ok')));
    return;
  }
  res.writeHead(404, { 'content-type': 'text/plain' });
  res.end('the mock gateway serves only /ws');
});
const wss = new WebSocketServer({ server, path: '/ws', perMessageDeflate: false });
let nextClient = 1;
wss.on('connection', (ws, req) => {
  const client = { ws, id: `c${nextClient++}`, attached: null, address: req.socket.remoteAddress };
  clients.add(client);
  ws.on('message', (data) => {
    let message;
    try {
      message = JSON.parse(String(data));
    } catch {
      return;
    }
    receive(client, message);
  });
  ws.on('close', () => {
    clients.delete(client);
    presence();
  });
});
server.listen(PORT, () => console.log(`mock gateway on http://localhost:${PORT} (ws /ws)`));
