"""Types the engine, the leaf base classes and the compatibility layer share (internal; the
public names are re-exported by :mod:`bt_server_core.engine` and :mod:`bt_server_core.leaves`).

Also the cancellation tokens of started leaves: every start of a thread leaf owns a
:class:`RunToken`, bound to the worker thread that runs its call, so a cancelled call sees its
own cancellation even after the node started again, and can no longer change the tree
(:class:`ExecutionCancelled`), as in the widget (``_runctx.py``).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from enum import IntEnum
from typing import Any

__all__ = [
    "BundleError",
    "CommandRefused",
    "ExecutionCancelled",
    "Outcome",
    "RunToken",
    "State",
    "Status",
    "bound",
    "check_not_cancelled",
    "current_token",
    "describe_error",
    "result_status",
]


class State(IntEnum):
    """Run states (contract 2.2)."""

    IDLE = 0
    RUNNING = 1
    PAUSED = 2
    STOPPING = 3
    RESTARTING = 4


class Status(IntEnum):
    """Node statuses (design 5.1); the byte of ``TreeStatus.node_status``."""

    READY = 0
    RUNNING = 1
    SUCCEEDED = 2
    FAILED = 3
    CANCELLING = 4

    @property
    def text(self) -> str:
        """``"Succeeded"``: how the log and the page name the status."""
        return self.name.capitalize()


class Outcome(IntEnum):
    """How a run ended (contract 2.2, ``ExecuteTree`` result)."""

    NONE = 0
    SUCCEEDED = 1
    FAILED = 2
    STOPPED = 3
    ERROR = 4


class BundleError(ValueError):
    """The bundle cannot be read: the goal is rejected (R-13)."""


class CommandRefused(Exception):  # noqa: N818 - the contract's name
    """A command the run state refuses (design 4.4)."""


class ExecutionCancelled(RuntimeError):  # noqa: N818 - the widget's name
    """Raised when a cancelled call (Stop, Reset, a halt) tries to change the tree: the
    blackboard, its node's fields or title, or the run state."""


def describe_error(error: BaseException) -> str:
    """``"ValueError: boom"``: the type and text of an exception (never raises)."""
    try:
        text = str(error)
    except Exception:  # noqa: BLE001 - an exception whose __str__ fails
        text = "<unprintable error>"
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


_FOREIGN_STATUS = {"SUCCESS": Status.SUCCEEDED, "FAILURE": Status.FAILED, "RUNNING": Status.RUNNING}


def result_status(result: Any) -> tuple[Status, str | None]:
    """The status a leaf's return value stands for, and an error message for a wrong one.

    Accepted: ``True`` / ``False``; :class:`Status` ``RUNNING``, ``SUCCEEDED``, ``FAILED``;
    a status enum with the names ``SUCCESS``, ``FAILURE``, ``RUNNING`` (py_trees's, and
    ``compat.Status``).
    """
    if isinstance(result, bool):
        return (Status.SUCCEEDED if result else Status.FAILED), None
    if isinstance(result, Status):
        if result in (Status.RUNNING, Status.SUCCEEDED, Status.FAILED):
            return result, None
        return Status.FAILED, f"returned Status.{result.name}; expected RUNNING, SUCCEEDED or FAILED"
    name = getattr(result, "name", None)
    if isinstance(name, str) and type(result).__name__ == "Status":
        status = _FOREIGN_STATUS.get(name)
        if status is not None:
            return status, None
        return Status.FAILED, f"returned Status.{name}"
    suffix = " (missing return statement?)" if result is None else ""
    return Status.FAILED, f"must return True, False or a Status, not {type(result).__name__}{suffix}"


class RunToken:
    """The cancellation flag of one start of a leaf. ``generation`` names the run segment it
    belongs to (a command issued by a call of an earlier segment is dropped)."""

    __slots__ = ("_event", "generation")

    def __init__(self, generation: int = -1) -> None:
        self._event = threading.Event()
        self.generation = generation

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()


_local = threading.local()


def current_token() -> RunToken | None:
    """The token bound to the calling (worker) thread, if any."""
    return getattr(_local, "token", None)


@contextmanager
def bound(token: RunToken) -> Iterator[None]:
    """Bind ``token`` to the calling thread for the duration of the block."""
    previous = current_token()
    _local.token = token
    try:
        yield
    finally:
        _local.token = previous


def check_not_cancelled(action: str) -> None:
    """Raise :class:`ExecutionCancelled` if the calling thread runs a cancelled call."""
    token = current_token()
    if token is not None and token.is_cancelled():
        raise ExecutionCancelled(f"{action} refused: this call was cancelled (Stop, Reset or a halt)")
