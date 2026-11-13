"""One browser tab's connection (design 10.2).

Messages that must arrive in order and each once (answers, operations, document events, run
events, notices) wait in a queue of their own. Statuses, details and blackboard values are
**latest-value streams**: a connection only notes *what* changed, and its sender loop builds
one message from the *current* state when the socket has taken the previous one, then pauses
until the next frame is due (33 ms). A slow browser gets fewer, current messages and the
gateway's memory per browser stays bounded (G-11). The log is a queue of at most 10,000
lines; older lines are dropped and the next batch says how many (G-12).
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from .wire import dumps, st_body

if TYPE_CHECKING:
    from .gateway import Gateway
    from .session import Session

__all__ = ["FRAME", "LOG_QUEUE", "LOG_BATCH", "MAX_QUEUE", "Client"]

log = logging.getLogger("bt_server_gateway")

#: Pause between two frames of the latest-value streams (design 10.2).
FRAME = 0.033
#: Lines a browser may fall behind before the oldest are dropped (G-12).
LOG_QUEUE = 10_000
#: Lines sent in one ``log`` message.
LOG_BATCH = 2_000
#: Ordered messages a browser may fall behind before its connection is closed (it reconnects
#: and gets a snapshot): the bound on what an ordered queue can hold.
MAX_QUEUE = 20_000

_ids = itertools.count(1)


class Client:
    """A browser tab. ``send(text)`` sends one message and returns when the socket took it;
    ``close()`` closes the connection."""

    def __init__(self, gateway: Gateway, send: Callable[[str], Awaitable[None]], close: Callable[[], Awaitable[Any]],
                 address: str) -> None:
        self.id = f"c{next(_ids)}"
        self.gateway = gateway
        self.address = address
        self.session: Session | None = None
        self.watching: str | None = None
        self.shown: set[str] = set()
        self.greeted = False
        self._send = send
        self._close = close
        self._queue: deque[str] = deque()
        self._st: set[str] = set()
        self._nd: dict[str, set[tuple[int, str]]] = {}
        self._bb_changed: set[str] = set()
        self._bb_removed: set[str] = set()
        self._bb_order = False
        self._log: deque[tuple[int, list]] = deque()
        self._log_exec: str | None = None
        self._skipped = 0
        self._wake = asyncio.Event()
        self._frame_due = 0.0
        self._frame_timer: asyncio.TimerHandle | None = None
        self._closed = False
        self._task: asyncio.Task | None = None

    # ----------------------------------------------------------------- what is due
    def push(self, message: dict) -> None:
        """Queue an ordered message."""
        if self._closed:
            return
        if len(self._queue) >= MAX_QUEUE:
            log.warning("client %s (%s) fell %d messages behind: closing its connection", self.id, self.address,
                        MAX_QUEUE)
            self._closed = True
            self._queue.clear()
            asyncio.get_running_loop().create_task(self._close())
            return
        self._queue.append(dumps(message))
        self._wake.set()

    def mark_st(self, executor: str) -> None:
        self._st.add(executor)
        self._wake.set()

    def mark_nd(self, executor: str, keys: list[tuple[int, str]]) -> None:
        self._nd.setdefault(executor, set()).update(keys)
        self._wake.set()

    def mark_bb(self, changed: list[str] = (), removed: list[str] = (), order: bool = False) -> None:
        for entry_id in removed:
            self._bb_changed.discard(entry_id)
            self._bb_removed.add(entry_id)
        for entry_id in changed:
            self._bb_removed.discard(entry_id)
            self._bb_changed.add(entry_id)
        self._bb_order = self._bb_order or order
        self._wake.set()

    def add_log(self, executor: str, lines: list[tuple[int, list]]) -> None:
        """Queue log lines ``(seq, line)``; beyond 10,000 the oldest are dropped and counted."""
        if executor != self._log_exec:
            self._log.clear()
            self._log_exec = executor
        for item in lines:
            if len(self._log) >= LOG_QUEUE:
                self._log.popleft()
                self._skipped += 1
            self._log.append(item)
        self._wake.set()

    def reset_streams(self) -> None:
        """A snapshot was queued: what it holds need not be sent again."""
        self._st.clear()
        self._nd.clear()
        self._bb_changed.clear()
        self._bb_removed.clear()
        self._bb_order = False
        self._log.clear()
        self._skipped = 0

    def clear_log(self) -> None:
        """A new run: lines of the earlier one still queued are not sent."""
        self._log.clear()
        self._skipped = 0

    @property
    def backlog(self) -> int:
        """What this connection holds for its browser (tests: G-11)."""
        return (len(self._queue) + len(self._st) + sum(map(len, self._nd.values())) + len(self._bb_changed)
                + len(self._bb_removed) + len(self._log))

    # ----------------------------------------------------------------- the sender loop
    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        self._closed = True
        if self._frame_timer is not None:
            self._frame_timer.cancel()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def close_socket(self) -> None:
        """Close the connection (shutdown)."""
        self._closed = True
        await self._close()

    def _streams_due(self) -> bool:
        return bool(self._st or self._nd or self._bb_changed or self._bb_removed or self._bb_order or self._log)

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            while not self._closed:
                await self._wake.wait()
                self._wake.clear()
                while self._queue:
                    await self._send(self._queue.popleft())
                if not self._streams_due():
                    continue
                now = loop.time()
                if now < self._frame_due:
                    if self._frame_timer is None:
                        self._frame_timer = loop.call_later(self._frame_due - now, self._frame)
                    continue
                for message in self._stream_messages():
                    await self._send(dumps(message))
                    while self._queue:  # an answer is not held up behind a long frame
                        await self._send(self._queue.popleft())
                self._frame_due = loop.time() + FRAME
                if self._streams_due():
                    self._wake.set()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - the socket broke: the reader ends the connection
            log.debug("client %s: sending failed: %s", self.id, error)
            self._closed = True
            try:
                await self._close()
            except Exception:  # noqa: BLE001
                pass

    def _frame(self) -> None:
        self._frame_timer = None
        self._wake.set()

    def _stream_messages(self) -> list[dict]:
        """One message per stream, from the current state."""
        messages: list[dict] = []
        gateway = self.gateway
        for executor in sorted(self._st):
            session = gateway.sessions.get(executor)
            if session is not None:
                messages.append(st_body(executor, session.status, session.starting))
        self._st.clear()
        for executor, keys in self._nd.items():
            session = gateway.sessions.get(executor)
            if session is None:
                continue
            details: dict[str, dict[str, Any]] = {}
            for index, kind in sorted(keys):
                details.setdefault(str(index), {})[kind] = session.details.get(index, {}).get(kind)
            messages.append({"t": "nd", "exec": executor, "details": details})
        self._nd.clear()
        session = self.session
        if session is not None and (self._bb_changed or self._bb_removed or self._bb_order):
            entries = [entry for entry in (session.board.wire_entry(entry_id) for entry_id in sorted(self._bb_changed))
                       if entry is not None]
            ids = session.board.ids()
            message = {"t": "bb", "exec": session.name, "entries": entries, "removed": sorted(self._bb_removed),
                       "order": ids if self._bb_order else None}
            messages.append(message)
        self._bb_changed.clear()
        self._bb_removed.clear()
        self._bb_order = False
        if self._log:
            count = min(LOG_BATCH, len(self._log))
            batch = [self._log.popleft() for _ in range(count)]
            message = {"t": "log", "exec": self._log_exec, "first": batch[0][0], "lines": [line for _, line in batch]}
            if self._skipped:
                message["skipped"] = self._skipped
                self._skipped = 0
            messages.append(message)
        return messages
