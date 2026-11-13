"""The engine's :class:`~bt_server_core.scheduler.Scheduler` on rclpy (design 5.8, 8.2).

* ``now()`` is the host's steady clock (``time.monotonic``, the clock rcl's steady timers use);
  ``ros_now()`` is the node's ROS clock, which follows ``/clock`` with ``use_sim_time`` (5.4).
* ``call_soon(fn)`` is safe from any thread: ``fn`` goes on a queue and the executor is woken
  to drain it on its thread. This is how worker threads and rclpy callbacks of other threads
  hand work to the engine thread. It takes a lock, so a signal handler must not call it (the
  handler may interrupt the engine thread while that holds the lock): the executor's handlers
  only count signals, and its main loop acts on them. The wake-up is ``executor.create_task``,
  which is thread-safe in both executors: the ``EventsExecutor`` of Lyrical refuses guard
  conditions ("Guard conditions not supported"), so the guard condition of design 8.2 is the
  executor's own task queue here.
* ``call_at(when, fn, ros=False)`` keeps the deadlines in a heap of its own per clock and arms
  one rclpy timer for the earliest of each, instead of one rclpy timer per deadline: the
  ``EventsExecutor`` slows down with many timers (8.2).

Everything but ``call_soon`` is meant for the engine thread (the thread that spins the
executor). Callbacks run in time order; one that raises is logged and does not stop the others.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
from collections import deque
from collections.abc import Callable

from rclpy.clock import Clock, ClockType
from rclpy.executors import Executor
from rclpy.node import Node

__all__ = ["RclpyScheduler"]

log = logging.getLogger("bt_server_executor")

#: A timer never fires sooner than this after it is armed (rcl rounds to nanoseconds; a period
#: of 0 would make a busy loop).
_MIN_DELAY_NS = 1000


class _Deadline:
    __slots__ = ("cancelled", "fn", "when")

    def __init__(self, when: float, fn: Callable[[], None]) -> None:
        self.when = when
        self.fn = fn
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class _Deadlines:
    """The deadlines on one clock and the one rclpy timer armed for the earliest."""

    def __init__(self, node: Node, clock: Clock, now: Callable[[], float], run: Callable[[Callable[[], None]], None]) -> None:
        self._now = now
        self._run = run
        self._heap: list[tuple[float, int, _Deadline]] = []
        self._order = itertools.count()
        self._armed_for: float | None = None
        self._timer = node.create_timer(1.0, self._on_timer, clock=clock, autostart=False)

    def add(self, when: float, fn: Callable[[], None]) -> _Deadline:
        deadline = _Deadline(when, fn)
        heapq.heappush(self._heap, (when, next(self._order), deadline))
        if self._armed_for is None or when < self._armed_for:
            self._arm()
        return deadline

    def _arm(self) -> None:
        while self._heap and self._heap[0][2].cancelled:
            heapq.heappop(self._heap)
        if not self._heap:
            if self._armed_for is not None:
                self._timer.cancel()
                self._armed_for = None
            return
        when = self._heap[0][0]
        delay_ns = max(_MIN_DELAY_NS, int((when - self._now()) * 1e9))
        self._timer.cancel()
        self._timer.timer_period_ns = delay_ns
        self._timer.reset()
        self._armed_for = when

    def _on_timer(self) -> None:
        self._timer.cancel()
        self._armed_for = None
        now = self._now()
        due = []
        while self._heap and self._heap[0][0] <= now:
            _, _, deadline = heapq.heappop(self._heap)
            if not deadline.cancelled:
                due.append(deadline)
        for deadline in due:
            if not deadline.cancelled:
                deadline.cancelled = True
                self._run(deadline.fn)
        if self._armed_for is None:
            self._arm()

    def destroy(self, node: Node) -> None:
        self._heap.clear()
        node.destroy_timer(self._timer)


class RclpyScheduler:
    """The scheduler of the executor node (see the module docs). Make it on the engine thread,
    after the node has been added to ``executor``, before spinning."""

    def __init__(self, node: Node, executor: Executor) -> None:
        self._node = node
        self._executor = executor
        self._queue: deque[Callable[[], None]] = deque()
        self._lock = threading.Lock()
        self._woken = False
        self._steady = _Deadlines(node, Clock(clock_type=ClockType.STEADY_TIME), time.monotonic, self._call)
        self._ros = _Deadlines(node, node.get_clock(), self.ros_now, self._call)

    # ----------------------------------------------------------------- the Scheduler protocol
    def now(self) -> float:
        return time.monotonic()

    def ros_now(self) -> float:
        return self._node.get_clock().now().nanoseconds / 1e9

    def call_soon(self, fn: Callable[[], None]) -> None:
        self._queue.append(fn)
        with self._lock:
            if self._woken:
                return
            self._woken = True
        try:
            self._executor.create_task(self._drain)
        except Exception:  # noqa: BLE001 - the executor is shut down: nothing runs any more
            with self._lock:
                self._woken = False

    def call_at(self, when: float, fn: Callable[[], None], ros: bool = False) -> _Deadline:
        return (self._ros if ros else self._steady).add(float(when), fn)

    def call_later(self, delay: float, fn: Callable[[], None]) -> _Deadline:
        """``fn`` after ``delay`` seconds on the steady clock (engine thread)."""
        return self._steady.add(time.monotonic() + delay, fn)

    # ----------------------------------------------------------------- plumbing
    def _drain(self) -> None:
        with self._lock:
            self._woken = False
        # Only what is queued now: callbacks that queue more get the next wake-up, so a
        # callback that re-queues itself cannot starve the executor.
        for _ in range(len(self._queue)):
            try:
                fn = self._queue.popleft()
            except IndexError:
                break
            self._call(fn)

    @staticmethod
    def _call(fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 - one failing callback must not stop the others
            log.exception("a scheduled callback raised")

    def destroy(self) -> None:
        """Drop the timers (when the node is destroyed)."""
        self._steady.destroy(self._node)
        self._ros.destroy(self._node)
