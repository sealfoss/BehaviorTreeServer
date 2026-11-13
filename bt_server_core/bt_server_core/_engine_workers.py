"""Reused daemon worker threads for thread leaves (internal; ported from the widget's
``_workers.py``).

Each worker runs one call at a time. :meth:`WorkerPool.submit` hands a call to an idle worker or
starts a new one, so a call never waits for another: a call that never returns occupies one
thread and delays nothing else (design 6.6). A worker idle for ``idle_timeout`` seconds ends.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

__all__ = ["WorkerPool", "leaf_pool"]


def _run(future: Future, function: Callable[[], Any]) -> None:
    if not future.set_running_or_notify_cancel():
        return
    try:
        result = function()
    except BaseException as error:  # noqa: BLE001 - reported through the future
        future.set_exception(error)
    else:
        future.set_result(result)


class WorkerPool:
    """Daemon threads that run calls and are reused from one call to the next."""

    IDLE_NAME = "bt_server_core:idle"

    def __init__(self, idle_timeout: float = 60.0) -> None:
        self.idle_timeout = idle_timeout
        self._lock = threading.Lock()
        self._tasks: queue.Queue = queue.Queue()
        self._idle = 0  # waiting workers no queued call was promised to
        self._workers = 0

    def submit(self, function: Callable[[], Any], name: str) -> Future:
        """Run ``function()`` on a worker named ``name``; returns a Future for its result.

        Raises:
            RuntimeError: a new worker was needed and could not be started.
        """
        future: Future = Future()
        task = (future, function, name)
        with self._lock:
            reuse = self._idle > 0
            if reuse:
                self._idle -= 1
            else:
                self._workers += 1
        if reuse:
            self._tasks.put(task)
            return future
        try:
            threading.Thread(target=self._work, args=(task,), name=name, daemon=True).start()
        except BaseException:
            with self._lock:
                self._workers -= 1
            raise
        return future

    def worker_count(self) -> int:
        with self._lock:
            return self._workers

    def _work(self, task: tuple | None) -> None:
        thread = threading.current_thread()
        while task is not None:
            future, function, name = task
            task = None
            thread.name = name
            _run(future, function)
            future = function = None
            thread.name = self.IDLE_NAME
            task = self._next_task()

    def _next_task(self) -> tuple | None:
        with self._lock:
            self._idle += 1
        while True:
            try:
                return self._tasks.get(timeout=self.idle_timeout)
            except queue.Empty:
                with self._lock:
                    if self._idle > 0:
                        self._idle -= 1
                        self._workers -= 1
                        return None


_pool: WorkerPool | None = None
_pool_lock = threading.Lock()


def leaf_pool() -> WorkerPool:
    """The pool running the calls of thread leaves (created when first needed)."""
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = WorkerPool()
        return _pool
