"""The engine's only link to an event loop (design 5.8): the :class:`Scheduler` protocol and two
implementations.

* :class:`AsyncioScheduler` runs on a running asyncio loop (``LocalLink`` in the gateway).
* :class:`FakeScheduler` is a clock the tests control: :meth:`FakeScheduler.advance` runs the
  due callbacks in time order, and callbacks handed to :meth:`~FakeScheduler.call_soon` run
  before time advances.

The executor's rclpy scheduler lives in ``bt_server_executor``.

Two clocks (design 5.4): :meth:`Scheduler.now` is the host's steady clock, used for the
plumbing (tick interval, cancel timeouts, ages of fed entries); :meth:`Scheduler.ros_now` is the
ROS clock, used for the timeouts a user sets on a node. Without ROS they are the same clock.
``call_at(..., ros=True)`` takes a time on the ROS clock.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Protocol

__all__ = ["AsyncioScheduler", "Cancelable", "FakeScheduler", "Scheduler"]


class Cancelable(Protocol):
    """A scheduled call that can be cancelled before it runs."""

    def cancel(self) -> None: ...


class Scheduler(Protocol):
    """What the engine needs from an event loop. Everything but :meth:`call_soon` is called on
    the engine thread only."""

    def now(self) -> float:
        """The steady clock, in seconds."""
        ...

    def ros_now(self) -> float:
        """The ROS clock, in seconds; equal to :meth:`now` without ROS."""
        ...

    def call_soon(self, fn: Callable[[], None]) -> None:
        """Run ``fn`` on the engine thread as soon as possible. Safe from any thread."""
        ...

    def call_at(self, when: float, fn: Callable[[], None], ros: bool = False) -> Cancelable:
        """Run ``fn`` on the engine thread at ``when`` (on the ROS clock with ``ros``)."""
        ...


class _FakeTimer:
    __slots__ = ("cancelled", "fn", "ros_when", "when")

    def __init__(self, when: float, fn: Callable[[], None], ros_when: float | None = None) -> None:
        self.when = when
        self.fn = fn
        self.ros_when = ros_when  # the time asked for on the ROS clock
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class FakeScheduler:
    """A clock and event loop for tests.

    Time stands still until :meth:`advance` or :meth:`advance_to` moves it. Timers due at the
    same time run in the order they were set. :meth:`call_soon` is safe from any thread; its
    callbacks run whenever the test lets time pass (or calls :meth:`run_pending`), always before
    a later timer. ``ros_now`` equals ``now`` plus ``ros_offset``.
    """

    #: Timers due within this much of the target time run when time is advanced to it, so
    #: sums such as ``1.4 + 0.5`` hit ``1.9``.
    EPSILON = 1e-9

    def __init__(self, start: float = 0.0, ros_offset: float = 0.0) -> None:
        self._now = float(start)
        self.ros_offset = float(ros_offset)
        self._timers: list[tuple[float, int, _FakeTimer]] = []
        self._order = itertools.count()
        self._soon: deque[Callable[[], None]] = deque()
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)

    def now(self) -> float:
        return self._now

    def ros_now(self) -> float:
        return self._now + self.ros_offset

    def call_soon(self, fn: Callable[[], None]) -> None:
        with self._wake:
            self._soon.append(fn)
            self._wake.notify_all()

    def call_at(self, when: float, fn: Callable[[], None], ros: bool = False) -> Cancelable:
        ros_when = float(when) if ros else None
        if ros:
            when -= self.ros_offset
        timer = _FakeTimer(float(when), fn, ros_when)
        heapq.heappush(self._timers, (timer.when, next(self._order), timer))
        return timer

    def run_pending(self) -> int:
        """Run the callbacks handed to :meth:`call_soon` (also those they add). Returns how many ran."""
        count = 0
        while True:
            with self._lock:
                if not self._soon:
                    return count
                fn = self._soon.popleft()
            fn()
            count += 1

    def advance(self, seconds: float) -> None:
        """Let ``seconds`` pass, running every callback that falls due, in time order."""
        self.advance_to(self._now + seconds)

    def advance_to(self, when: float) -> None:
        """Let time pass until ``when``, running every callback that falls due, in time order."""
        self.run_pending()
        while True:
            timer = self._next_timer(when)
            if timer is None:
                break
            self._now = max(self._now, min(timer.when, float(when)))
            timer.fn()
            self.run_pending()
        self._now = max(self._now, float(when))
        self.run_pending()

    def _next_timer(self, until: float) -> _FakeTimer | None:
        while self._timers:
            when, _, timer = self._timers[0]
            if timer.cancelled:
                heapq.heappop(self._timers)
                continue
            if when > until + self.EPSILON and not self._ros_due(timer, until):
                return None
            heapq.heappop(self._timers)
            return timer
        return None

    def _ros_due(self, timer: _FakeTimer, until: float) -> bool:
        """A ROS-clock timer is due when its ROS time is reached; compared on the ROS clock, so a
        large clock offset (sim time near the epoch) does not shift it by a rounding error."""
        if timer.ros_when is None:
            return False
        target = until + self.ros_offset
        return timer.ros_when <= target + self.EPSILON + 4 * math.ulp(target)

    def wait_for(self, predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
        """Run :meth:`call_soon` callbacks (from worker threads) as they arrive until
        ``predicate()`` is true or ``timeout`` real seconds passed. Time does not advance."""
        deadline = time.monotonic() + timeout
        while True:
            self.run_pending()
            if predicate():
                return True
            with self._wake:
                left = deadline - time.monotonic()
                if left <= 0:
                    return predicate()
                if not self._soon:
                    self._wake.wait(min(left, 0.05))

    def pending_timers(self) -> list[float]:
        """The times of the timers not yet run (for tests)."""
        return sorted(when for when, _, timer in self._timers if not timer.cancelled)


class AsyncioScheduler:
    """A scheduler on a running asyncio loop (``LocalLink``). The steady clock is
    ``time.monotonic``; without ROS the ROS clock is the same clock."""

    def __init__(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._loop = loop if loop is not None else asyncio.get_running_loop()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    def now(self) -> float:
        return time.monotonic()

    def ros_now(self) -> float:
        return time.monotonic()

    def call_soon(self, fn: Callable[[], None]) -> None:
        try:
            self._loop.call_soon_threadsafe(fn)
        except RuntimeError:  # the loop is closed: nothing will run any more
            pass

    def call_at(self, when: float, fn: Callable[[], None], ros: bool = False) -> Cancelable:
        return self._loop.call_later(max(0.0, when - time.monotonic()), fn)
