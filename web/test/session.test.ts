import { describe, expect, it, vi } from 'vitest';
import { childIds } from '../src/lib/ops';
import { Session } from '../src/lib/stores/session.svelte';
import type { TreeDocument } from '../src/lib/types';
import { harness } from './fakeSocket';

function doc(): TreeDocument {
  return {
    key: 'robot', path: 'robot_BT.json', rev: 5, version: 1, unsaved: false, config: {}, view: { zoom: 1, center: [0, 0] },
    nodes: [
      { id: 'root', type: 'Root', title: 'Root', x: 0, y: 0, composite: 'Sequence', memory: true },
      { id: 'a', type: 'Stop', title: 'A', x: -100, y: 200 },
      { id: 'b', type: 'Stop', title: 'B', x: 100, y: 200 },
    ],
    connections: [{ parent: 'root', child: 'a' }, { parent: 'root', child: 'b' }],
    problems: [],
  };
}

function attached() {
  const h = harness();
  const session = new Session((fn) => fn());
  session.connect(h.socket, 'ws://gw/ws');
  const socket = h.sockets[0];
  socket.open();
  socket.deliver({ t: 'welcome', protocol: 1, client: 'me', executors: [{ name: '/e', online: true, state: 0 }], time: 0 });
  expect(socket.last()).toEqual({ t: 'attach', executor: '/e' });
  socket.deliver({
    t: 'snapshot', exec: '/e', client: 'me', root: 'robot', docs: { robot: doc() }, blackboard: [], catalog: [], online: true, starting: false,
    run: null, st: null, details: {}, log: { first: 0, lines: [] }, recovery: [], attached: 2, findings: [],
  });
  return { h, session, socket };
}

const title = (session: Session, id: string) => session.docs.get('robot')!.nodes.find((n) => n.id === id)!.title;

describe('Session', () => {
  it('shows its own edit at once and sends it with the revision it was made at', async () => {
    const { session, socket } = attached();
    const reply = session.sendOp('robot', { op: 'node.set_title', id: 'a', title: 'Mine' });
    expect(title(session, 'a')).toBe('Mine');
    expect(socket.last()).toMatchObject({ t: 'op', doc: 'robot', rev: 5, cid: 1, op: { op: 'node.set_title', title: 'Mine' } });
    socket.deliver({ t: 'ok', cid: 1, rev: 6 });
    await reply;
  });

  it('applies every broadcast in order, its own included (G-06)', () => {
    const { session, socket } = attached();
    void session.sendOp('robot', { op: 'node.set_title', id: 'a', title: 'One' });
    // The gateway received the other browser's change first, then ours.
    socket.deliver({ t: 'op', doc: 'robot', rev: 6, op: { op: 'node.set_title', id: 'a', title: 'Two' }, by: 'other', cid: 4 });
    expect(title(session, 'a')).toBe('Two');
    socket.deliver({ t: 'op', doc: 'robot', rev: 7, op: { op: 'node.set_title', id: 'a', title: 'One' }, by: 'me', cid: 1 });
    expect(title(session, 'a')).toBe('One');
    expect(session.docs.get('robot')!.rev).toBe(7);
  });

  it('asks for the document again after a refused operation', async () => {
    const { session, socket } = attached();
    const reply = session.sendOp('robot', { op: 'conn.add', parent: 'a', child: 'root' });
    socket.deliver({ t: 'no', cid: 1, reason: 'that connection would make a cycle' });
    await reply;
    expect(socket.last()).toEqual({ t: 'resync', doc: 'robot' });
    expect(session.ui.notices.at(-1)?.text).toContain('cycle');
    socket.deliver({ t: 'doc', doc: 'robot', event: 'resync', document: doc() });
    expect(session.docs.get('robot')!.connections).toHaveLength(2);
  });

  it('works the order out again when the gateway dropped it (reorder)', async () => {
    const { session, socket } = attached();
    session.docs.get('robot')!.nodes.find((n) => n.id === 'a')!.x = 400; // a now right of b
    const reply = session.sendOp('robot', { op: 'node.move', positions: { a: [400, 200] }, orders: { root: ['b', 'a'] } });
    expect(childIds(session.docs.get('robot')!, 'root')).toEqual(['b', 'a']);
    socket.deliver({ t: 'ok', cid: 1, rev: 6, reorder: ['root'] });
    await reply;
    await Promise.resolve();
    expect(socket.last()).toMatchObject({ t: 'op', op: { op: 'node.move', orders: { root: ['b', 'a'] } } });
  });

  it('keeps the positions of nodes it drags against older broadcasts', () => {
    const { session, socket } = attached();
    session.dragging.add('a');
    session.docs.get('robot')!.nodes.find((n) => n.id === 'a')!.x = 50;
    socket.deliver({ t: 'op', doc: 'robot', rev: 6, op: { op: 'node.move', positions: { a: [10, 200], b: [120, 200] } }, by: 'me', cid: 1 });
    const nodes = session.docs.get('robot')!.nodes;
    expect(nodes.find((n) => n.id === 'a')!.x).toBe(50);
    expect(nodes.find((n) => n.id === 'b')!.x).toBe(120);
  });

  it('names the edits lost by a broken connection after the reconnect (G-10)', async () => {
    vi.useFakeTimers();
    const { h, session, socket } = attached();
    const pending = session.sendOp('robot', { op: 'node.set_title', id: 'b', title: 'Lost' });
    socket.close();
    await pending;
    expect(session.executors.online).toBe(false);
    vi.advanceTimersByTime(600); // the reconnect delay
    const again = h.sockets[1];
    again.open();
    again.deliver({ t: 'welcome', protocol: 1, client: 'me2', executors: [{ name: '/e', online: true, state: 0 }], time: 0 });
    expect(again.last()).toEqual({ t: 'attach', executor: '/e' });
    again.deliver({
      t: 'snapshot', exec: '/e', client: 'me2', root: 'robot', docs: { robot: doc() }, blackboard: [], catalog: [], online: true, starting: false,
      run: null, st: null, details: {}, log: { first: 0, lines: [] }, recovery: [], attached: 1, findings: [],
    });
    expect(title(session, 'b')).toBe('B'); // the server's state
    expect(session.ui.lostEdits).toHaveLength(1);
    expect(session.ui.lostEdits[0]).toContain('node.set_title');
    vi.useRealTimers();
  });

  it('reads statuses under the instance path of the Sub-Tree node zoomed through', () => {
    const { session, socket } = attached();
    socket.deliver({ t: 'run', exec: '/e', event: 'started', run_id: 'r', label: '', requested_by: '', node_paths: ['root', 'sub', 'sub/root2', 'sub/x'], own: true, bundle: null });
    socket.deliver({ t: 'st', exec: '/e', run_id: 'r', state: 1, loop: 0, cancelling: 0, unsettled: 0, outcome: 0, message: '', v: '1113' });
    session.view.push({ kind: 'doc', doc: 'robot', via: 'sub', prefix: 'sub' });
    expect(session.view.pathOf('x')).toBe('sub/x');
    expect(session.run.status(session.statusExec, session.view.pathOf('x'))).toBe(3);
  });

  it('attaches once per connection; a later welcome with a new list does not attach again', () => {
    const { session, socket } = attached();
    const sent = socket.sent.length;
    socket.deliver({ t: 'welcome', protocol: 1, client: 'me', executors: [{ name: '/e', online: true, state: 0 }, { name: '/f', online: true, state: 0 }], time: 0 });
    expect(socket.sent.length).toBe(sent);
    expect(session.executors.list.map((e) => e.name)).toEqual(['/e', '/f']);
  });

  it('counts the attached browsers and passes notices on', () => {
    const { session, socket } = attached();
    expect(session.executors.attachedCount).toBe(2);
    socket.deliver({ t: 'presence', attached: 3 });
    socket.deliver({ t: 'notice', level: 'warning', text: 'the blackboard file could not be written' });
    expect(session.executors.attachedCount).toBe(3);
    expect(session.ui.notices.at(-1)).toMatchObject({ level: 'warning' });
  });

  it('closes the recovery offer when another tab answered it, and keeps the document (contract 5.6)', async () => {
    const { session, socket } = attached();
    session.ui.recovery = ['robot'];
    const offer = session.ui.ask({ title: 'Recover', text: '', buttons: [{ label: 'Restore', value: 'yes' }], testid: 'dialog-recovery' }, 'robot');
    socket.deliver({ t: 'doc', doc: 'robot', event: 'recovery_declined', document: null });
    await expect(offer).resolves.toBeNull();
    expect(session.ui.recovery).toEqual([]);
    expect(session.docs.get('robot')).toBeDefined(); // not closed
    const recovered = { ...doc(), rev: 9, unsaved: true };
    socket.deliver({ t: 'doc', doc: 'robot', event: 'recovered', document: recovered });
    expect(session.docs.get('robot')!.rev).toBe(9);
  });

  it('forgets the last run outcome when New or Load shows another tree, and says the outcome once', () => {
    const { session, socket } = attached();
    socket.deliver({ t: 'run', exec: '/e', event: 'started', run_id: 'r', label: 'robot', requested_by: '', node_paths: ['root'], own: true, bundle: null });
    socket.deliver({ t: 'st', exec: '/e', run_id: 'r', state: 0, loop: 0, cancelling: 0, unsettled: 0, outcome: 1, message: 'succeeded', v: '2' });
    socket.deliver({ t: 'run', exec: '/e', event: 'ended', run_id: 'r', label: 'robot', requested_by: '', node_paths: ['root'], own: true, bundle: null, outcome: 1, message: 'Succeeded' });
    expect(session.ui.notices.at(-1)?.text).toBe('Execution finished: Succeeded');
    expect(session.lastOutcome).toBe(1);
    socket.deliver({
      t: 'snapshot', exec: '/e', client: 'me', root: 'other', docs: { other: { ...doc(), key: 'other' } }, blackboard: [], catalog: [], online: true,
      starting: false, run: { exec: '/e', event: 'started', run_id: 'r', label: 'robot', requested_by: '', node_paths: ['root'], own: true, bundle: null },
      st: { run_id: 'r', state: 0, loop: 0, cancelling: 0, unsettled: 0, outcome: 1, message: 'succeeded', v: '2' },
      details: {}, log: { first: 0, lines: [] }, recovery: [], attached: 1, findings: [],
    });
    expect(session.lastOutcome).toBe(0); // the run was robot's, the tree is other now
  });

  it('creates a missing sub-tree file when asked to (choose_file, contract 5.4)', async () => {
    const { session, socket } = attached();
    const done = session.file('zoom_in', { doc: 'robot', node: 'a', path: 'a' });
    socket.deliver({ t: 'no', cid: 1, reason: 'missing', question: { kind: 'choose_file', node: 'a', file: 'patrol_BT.json' } });
    await Promise.resolve();
    await Promise.resolve();
    expect(socket.last()).toMatchObject({ t: 'file', action: 'list' });
    socket.deliver({ t: 'ok', cid: socket.last().cid as number, trees: [{ key: 'robot' }], dirs: [] });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(session.ui.question?.input?.value).toBe('patrol'); // a tree name, as the file list has them
    session.ui.answer({ button: 'use', input: 'patrol' });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(socket.last()).toMatchObject({ t: 'file', action: 'zoom_in', file: 'patrol', create: true });
    socket.deliver({ t: 'ok', cid: socket.last().cid as number, doc: 'patrol', document: { ...doc(), key: 'patrol' } });
    await expect(done).resolves.toMatchObject({ t: 'ok', doc: 'patrol' });
  });
});
