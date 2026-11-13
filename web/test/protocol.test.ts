import { describe, expect, it } from 'vitest';
import { ConnectionLost, GatewayConnection, gatewayUrl, type Incoming } from '../src/lib/protocol';
import { harness } from './fakeSocket';

function connect() {
  const h = harness();
  const received: Incoming[] = [];
  const online: boolean[] = [];
  let reloads = 0;
  const conn = new GatewayConnection({
    url: 'ws://gw/ws',
    socket: h.socket,
    setTimer: h.setTimer,
    onMessage: (m) => received.push(m),
    onOnline: (o) => online.push(o),
    onReload: () => reloads++,
  });
  conn.start();
  const socket = h.sockets[0];
  socket.open();
  socket.deliver({ t: 'welcome', protocol: 1, client: 'c1', executors: [], time: 0, local: true });
  return { h, conn, socket, received, online, reloads: () => reloads };
}

describe('GatewayConnection', () => {
  it('says hello with protocol 1 when the socket opens', () => {
    const { socket, online } = connect();
    expect(socket.sent[0]).toEqual({ t: 'hello', protocol: 1 });
    expect(online).toEqual([true]);
  });

  it('gives each request a new cid and resolves it with its ok or no', async () => {
    const { conn, socket } = connect();
    const first = conn.request({ t: 'op', doc: 'robot', rev: 1, op: { op: 'node.set_title', id: 'a', title: 'x' } });
    const second = conn.request({ t: 'ask', kind: 'graph' });
    const cids = socket.sent.slice(1).map((m) => m.cid);
    expect(cids).toEqual([1, 2]);
    socket.deliver({ t: 'no', cid: 2, reason: 'offline executor' });
    socket.deliver({ t: 'ok', cid: 1, rev: 2 });
    await expect(first).resolves.toMatchObject({ t: 'ok', cid: 1, rev: 2 });
    await expect(second).resolves.toMatchObject({ t: 'no', reason: 'offline executor' });
    expect(conn.pendingCount).toBe(0);
  });

  it('passes every other message on in arrival order', () => {
    const { socket, received } = connect();
    socket.deliver({ t: 'op', doc: 'robot', rev: 2, op: { op: 'a' } });
    socket.deliver({ t: 'st', exec: 'e', v: '1' });
    socket.deliver({ t: 'op', doc: 'robot', rev: 3, op: { op: 'b' } });
    socket.deliver({ t: 'ok', cid: 99 }); // nobody waits for it: passed on too
    expect(received.map((m) => m.t)).toEqual(['welcome', 'op', 'st', 'op', 'ok']);
    expect(received.filter((m) => m.t === 'op').map((m) => m.rev)).toEqual([2, 3]);
  });

  it('gives up unanswered requests when the connection breaks and names the lost edits', async () => {
    const { conn, socket, online } = connect();
    const edit = conn.request({ t: 'op', doc: 'robot', rev: 4, op: { op: 'node.set_field', id: 'n', field: 'Message', value: 'hi' } });
    const question = conn.request({ t: 'ask', kind: 'graph' });
    socket.close();
    await expect(edit).rejects.toBeInstanceOf(ConnectionLost);
    await expect(question).rejects.toBeInstanceOf(ConnectionLost);
    expect(online).toEqual([true, false]);
    const lost = conn.takeLostEdits();
    expect(lost).toHaveLength(1); // questions are not edits
    expect(lost[0]).toContain('node.set_field');
    expect(lost[0]).toContain('Message');
    expect(conn.takeLostEdits()).toEqual([]);
  });

  it('refuses requests while offline and remembers refused edits', async () => {
    const { conn, socket } = connect();
    socket.close();
    await expect(conn.request({ t: 'op', doc: 'robot', rev: 1, op: { op: 'node.remove', ids: ['x'] } })).rejects.toBeInstanceOf(ConnectionLost);
    expect(conn.takeLostEdits()).toEqual(['node.remove in robot']);
  });

  it('reconnects with a growing delay and says hello again', () => {
    const { h, socket } = connect();
    socket.close();
    expect(h.timers.map((t) => t.ms)).toEqual([500]);
    h.runTimers();
    expect(h.sockets).toHaveLength(2);
    h.sockets[1].close(); // never opened
    expect(h.timers.map((t) => t.ms)).toEqual([1000]);
    h.runTimers();
    h.sockets[2].open();
    expect(h.sockets[2].sent[0]).toEqual({ t: 'hello', protocol: 1 });
    h.sockets[2].deliver({ t: 'welcome', protocol: 1, client: 'c2', executors: [] });
    h.sockets[2].close();
    expect(h.timers.map((t) => t.ms)).toEqual([500]); // reset by the welcome
  });

  it('asks the page to load again on reload', () => {
    const { socket, reloads, received } = connect();
    socket.deliver({ t: 'reload' });
    expect(reloads()).toBe(1);
    expect(received.some((m) => m.t === 'reload')).toBe(false);
  });

  it('stops for good', () => {
    const { h, conn } = connect();
    conn.stop();
    expect(h.timers).toHaveLength(0);
  });

  it('builds the WebSocket address of the page', () => {
    expect(gatewayUrl({ protocol: 'http:', host: 'bt.robot.local' })).toBe('ws://bt.robot.local/ws');
    expect(gatewayUrl({ protocol: 'https:', host: '10.0.0.2:8443' })).toBe('wss://10.0.0.2:8443/ws');
  });
});
