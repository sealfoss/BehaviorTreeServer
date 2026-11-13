/** A WebSocket stand-in for the protocol tests. */

import type { SocketLike } from '../src/lib/protocol';

export class FakeSocket implements SocketLike {
  readyState = 0;
  sent: Record<string, unknown>[] = [];
  onopen: ((event: unknown) => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: ((event: unknown) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;

  constructor(readonly url: string) {}

  send(data: string): void {
    this.sent.push(JSON.parse(data) as Record<string, unknown>);
  }

  close(): void {
    this.readyState = 3;
    this.onclose?.({});
  }

  /** The server side: open, then deliver messages. */
  open(): void {
    this.readyState = 1;
    this.onopen?.({});
  }

  deliver(message: Record<string, unknown>): void {
    this.onmessage?.({ data: JSON.stringify(message) });
  }

  last(): Record<string, unknown> {
    return this.sent[this.sent.length - 1];
  }
}

/** Collects the sockets a connection opens, and timers it schedules. */
export function harness() {
  const sockets: FakeSocket[] = [];
  const timers: { fn: () => void; ms: number }[] = [];
  return {
    sockets,
    timers,
    socket: (url: string) => {
      const socket = new FakeSocket(url);
      sockets.push(socket);
      return socket;
    },
    setTimer: (fn: () => void, ms: number) => {
      timers.push({ fn, ms });
      return timers.length;
    },
    /** Run the timers scheduled so far. */
    runTimers() {
      for (const timer of timers.splice(0)) timer.fn();
    },
  };
}
