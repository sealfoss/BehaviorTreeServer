/**
 * The browser end of the WebSocket (contract 5, design 10).
 *
 * One connection per tab. Requests carry a `cid` and are answered by exactly one `ok` or `no`
 * with that `cid`; {@link GatewayConnection.request} returns a promise for that answer. When the
 * connection breaks, requests still unanswered are given up (not sent again, design 9.3) and
 * remembered, so the page can name the changes that were lost after the reconnect (G-10).
 */

export const PROTOCOL = 1;

/** Messages the page sends (contract 5.2). `cid` is filled in by the connection. */
export type Outgoing =
  | { t: 'hello'; protocol: number }
  | { t: 'attach'; executor: string }
  | { t: 'op'; doc: string; rev: number; op: Record<string, unknown> }
  | { t: 'cmd'; name: 'execute' | 'pause' | 'resume' | 'stop' | 'reset'; confirm?: boolean }
  | { t: 'file'; action: string; [key: string]: unknown }
  | { t: 'ask'; kind: string; [key: string]: unknown }
  | { t: 'watch'; executor: string }
  | { t: 'unwatch' }
  | { t: 'resync'; doc: string };

/** Every message from the gateway is an object with `t`. */
export interface Incoming {
  t: string;
  [key: string]: unknown;
}

/** The answer to a request: `ok` or `no` (`reason`, and maybe `question` or `findings`). */
export interface Reply extends Incoming {
  t: 'ok' | 'no';
  cid: number;
  reason?: string;
}

/** A request given up because the connection broke before its answer arrived. */
export class ConnectionLost extends Error {
  constructor(readonly description: string) {
    super(`connection lost before the answer to: ${description}`);
  }
}

/** The part of a WebSocket the connection uses (a fake one in tests). */
export interface SocketLike {
  readonly readyState: number;
  send(data: string): void;
  close(): void;
  onopen: ((event: unknown) => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
  onclose: ((event: unknown) => void) | null;
  onerror: ((event: unknown) => void) | null;
}

export interface ConnectionOptions {
  url: string;
  socket?: (url: string) => SocketLike;
  /** Every message that is not an answer to a request, in arrival order. */
  onMessage: (message: Incoming) => void;
  /** Online after `welcome`; offline when the socket closes. */
  onOnline?: (online: boolean) => void;
  /** The gateway's protocol differs: the page must be loaded again. */
  onReload?: () => void;
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (handle: unknown) => void;
}

interface Pending {
  resolve: (reply: Reply) => void;
  reject: (error: Error) => void;
  description: string;
  /** Ops are edits: losing one is reported to the user. */
  edit: boolean;
}

const OPEN = 1;

export class GatewayConnection {
  private socket: SocketLike | null = null;
  private nextCid = 1;
  private readonly pending = new Map<number, Pending>();
  private lostEdits: string[] = [];
  private retryDelay = 500;
  private retryHandle: unknown = null;
  private stopped = false;
  online = false;

  constructor(private readonly options: ConnectionOptions) {}

  /** Open the socket (and keep reopening it after it closes). */
  start(): void {
    this.stopped = false;
    this.open();
  }

  /** Close for good (tests, page unload). */
  stop(): void {
    this.stopped = true;
    if (this.retryHandle !== null) this.options.clearTimer?.(this.retryHandle);
    this.socket?.close();
  }

  /** Changes sent before the last break and never answered; cleared by reading. */
  takeLostEdits(): string[] {
    const lost = this.lostEdits;
    this.lostEdits = [];
    return lost;
  }

  get pendingCount(): number {
    return this.pending.size;
  }

  /** Send a message that has no answer (`attach`, `watch`, `resync`, `hello`). */
  send(message: Outgoing): boolean {
    if (!this.socket || this.socket.readyState !== OPEN) return false;
    this.socket.send(JSON.stringify(message));
    return true;
  }

  /**
   * Send a request with a new `cid`; resolves with its `ok` or `no`. Rejects with
   * {@link ConnectionLost} when the connection is down or breaks before the answer.
   */
  request(message: Outgoing, description = describe(message)): Promise<Reply> {
    const edit = message.t === 'op';
    if (!this.socket || this.socket.readyState !== OPEN || !this.online) {
      if (edit) this.lostEdits.push(description);
      return Promise.reject(new ConnectionLost(description));
    }
    const cid = this.nextCid++;
    return new Promise<Reply>((resolve, reject) => {
      this.pending.set(cid, { resolve, reject, description, edit });
      this.socket!.send(JSON.stringify({ ...message, cid }));
    });
  }

  /** The `cid` the next request will get (the page matches its own op broadcasts by it). */
  peekCid(): number {
    return this.nextCid;
  }

  private open(): void {
    const factory = this.options.socket ?? ((url: string) => new WebSocket(url) as unknown as SocketLike);
    let socket: SocketLike;
    try {
      socket = factory(this.options.url);
    } catch {
      this.scheduleRetry();
      return;
    }
    this.socket = socket;
    socket.onopen = () => {
      socket.send(JSON.stringify({ t: 'hello', protocol: PROTOCOL }));
    };
    socket.onmessage = (event) => this.receive(event.data);
    socket.onerror = () => {
      // A close event follows.
    };
    socket.onclose = () => {
      if (this.socket !== socket) return;
      this.socket = null;
      this.dropPending();
      this.setOnline(false);
      if (!this.stopped) this.scheduleRetry();
    };
  }

  private receive(data: unknown): void {
    let message: Incoming;
    try {
      message = JSON.parse(String(data)) as Incoming;
    } catch {
      return;
    }
    if (!message || typeof message !== 'object' || typeof message.t !== 'string') return;
    if ((message.t === 'ok' || message.t === 'no') && typeof message.cid === 'number') {
      const waiting = this.pending.get(message.cid);
      if (waiting) {
        this.pending.delete(message.cid);
        waiting.resolve(message as Reply);
        return;
      }
    }
    if (message.t === 'reload') {
      this.options.onReload?.();
      return;
    }
    if (message.t === 'welcome') {
      this.retryDelay = 500;
      this.setOnline(true);
    }
    this.options.onMessage(message);
  }

  private dropPending(): void {
    for (const waiting of this.pending.values()) {
      if (waiting.edit) this.lostEdits.push(waiting.description);
      waiting.reject(new ConnectionLost(waiting.description));
    }
    this.pending.clear();
  }

  private setOnline(online: boolean): void {
    if (this.online === online) return;
    this.online = online;
    this.options.onOnline?.(online);
  }

  private scheduleRetry(): void {
    const setTimer = this.options.setTimer ?? ((fn: () => void, ms: number) => setTimeout(fn, ms));
    const delay = this.retryDelay;
    this.retryDelay = Math.min(this.retryDelay * 2, 5000);
    this.retryHandle = setTimer(() => {
      this.retryHandle = null;
      if (!this.stopped) this.open();
    }, delay);
  }
}

/** A short text naming a request, for the list of lost changes. */
export function describe(message: Outgoing): string {
  if (message.t === 'op') {
    const op = message.op as Record<string, unknown>;
    const what = String(op.op ?? 'edit');
    const detail = op.field ?? op.title ?? op.name ?? op.text ?? '';
    return detail ? `${what} (${String(detail).slice(0, 40)}) in ${message.doc}` : `${what} in ${message.doc}`;
  }
  if (message.t === 'cmd') return `command ${message.name}`;
  if (message.t === 'file') return `file ${message.action}`;
  if (message.t === 'ask') return `question ${message.kind}`;
  return message.t;
}

/** The WebSocket URL of the page's own gateway. */
export function gatewayUrl(location: { protocol: string; host: string }): string {
  return `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`;
}
