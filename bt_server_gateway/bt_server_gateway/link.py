"""The executor link (design 3.3, 9.1; contract 6): how the gateway reaches an executor.

Two implementations: :class:`bt_server_gateway.local_link.LocalLink` (the engine inside the
gateway process) and ``bt_server_gateway.ros_link.RosLink`` (the tree API of design 4 over ROS).
The rest of the gateway sees only these types. Every method runs on the gateway's asyncio loop;
listener callbacks are called on that loop.

Shapes the gateway relies on, beyond the signatures:

* ``blackboard`` arguments of :meth:`ExecutorLink.validate` and :meth:`ExecutorLink.execute` are
  entries in the form of the blackboard file **with values in file encoding**
  (``values.encode_value``) and an ``"id"``: what ``Blackboard.to_list()`` returns. RosLink puts
  them into ``blackboard_json`` with ``values.dumps_json``.
* Everything that comes back carries **decoded** values (Python values): the entries of
  :meth:`RunListener.on_blackboard` updates (``{"entries": [...], "removed": [ids], "order":
  [ids] | None}``; an entry may carry ``"freshness"`` and ``"age"``; an Object entry's value is
  its ``repr`` text), ``LinkResult.blackboard`` and the ``"blackboard"`` of
  :meth:`ExecutorLink.snapshot`.
* Details (:meth:`RunListener.on_details`, ``snapshot()["details"]``) are the items of
  ``NodeDetails.details_json``: ``{"i": index, "kind": kind, "value": value, "stamp": s}``; the
  value of a ``field`` detail is in file encoding, as JSON carries it.
* Operations given to :meth:`ExecutorLink.apply` carry Python values (``node.set_field`` with
  ``tree`` and ``id``); over ROS ``ops_json`` uses the file encoding. An executor that applies a
  ``node.set_field`` reports the new value back as a ``field`` detail of each instance (design
  9.3: the gateway changes its document only then).
* :meth:`ExecutorLink.describe` returns the parsed ``result_json``: ``node_types`` ->
  ``{"types": [catalog entries]}``; ``graph`` -> ``{"actions": [[name, type]], "services":
  [[name, type]], "topics": [[name, type]]}``; ``interface`` -> ``{"interface": <field tree>}``;
  ``sample`` -> ``{"value": <typed wire value>, "message": text}``. A refused question (``ok``
  false) raises :class:`CommandRefused` with the reason.
* :meth:`ExecutorLink.validate` returns findings ``{"path", "severity", "text"}``; a bundle that
  cannot be read at all gives one ``error`` finding saying so.
* Methods that need the executor raise :class:`LinkDown` while it cannot be reached.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

__all__ = [
    "APPLIED",
    "NO_RUN",
    "REJECTED",
    "CommandRefused",
    "ExecutorLink",
    "GoalRejected",
    "LinkDown",
    "LinkFactory",
    "LinkRecord",
    "LinkResult",
    "LinkRun",
    "LinkStatus",
    "RunListener",
]

#: Answers of :meth:`ExecutorLink.apply` (``Apply.srv``).
APPLIED, NO_RUN, REJECTED = 0, 1, 2


@dataclass
class LinkRun:
    """``RunInfo``: what the executor has loaded. ``run_id`` None: nothing loaded."""

    run_id: str | None
    label: str = ""
    requested_by: str = ""
    bundle: dict | None = None
    node_paths: list[str] = field(default_factory=list)


@dataclass
class LinkStatus:
    """``TreeStatus``: the run state and one status byte per node (order of ``node_paths``)."""

    run_id: str | None
    seq: int
    state: int
    loop: int
    cancelling: int
    unsettled: int
    outcome: int
    message: str
    statuses: bytes


@dataclass
class LinkRecord:
    """``LogRecord``, numbered from 0 in each run. ``stamp``: seconds since the epoch."""

    seq: int
    stamp: float
    level: int
    node_index: int
    text: str


@dataclass
class LinkResult:
    """The ``ExecuteTree`` result of a run this gateway started."""

    run_id: str
    outcome: int
    message: str
    loops_completed: int
    unconfirmed_cancels: int
    blackboard: list[dict]  # decoded values, each entry with "id"
    goal_status: str  # "succeeded" | "aborted" | "canceled"


class GoalRejected(Exception):
    """The goal was rejected; ``str(e)`` is the reason (busy: names who started the run)."""


class CommandRefused(Exception):
    """A command (or a question) the executor refuses; ``str(e)`` is the reason."""


class LinkDown(Exception):
    """The executor cannot be reached."""


class RunListener(Protocol):
    """What a link reports (design 4.2). Called on the gateway's asyncio loop."""

    def on_online(self, online: bool) -> None: ...

    def on_run(self, run: LinkRun) -> None: ...

    def on_status(self, status: LinkStatus) -> None: ...

    def on_blackboard(self, run_id: str, seq: int, full: bool, update: dict) -> None: ...

    def on_details(self, run_id: str, seq: int, details: list[dict]) -> None: ...

    def on_log(self, run_id: str, records: list[LinkRecord]) -> None: ...

    def on_result(self, result: LinkResult) -> None: ...

    def on_catalog(self, types: list[dict]) -> None: ...


class ExecutorLink(Protocol):
    """One executor (see the module docs)."""

    name: str  # "/cell1/bt_executor"; LocalLink: "local"
    online: bool

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def describe(self, kind: str, arg: dict) -> dict: ...

    async def validate(self, bundle: dict, blackboard: list[dict]) -> list[dict]: ...

    async def execute(self, bundle: dict, blackboard: list[dict], label: str, requested_by: str) -> str: ...

    async def stop(self, run_id: str) -> None: ...

    async def pause(self, paused: bool) -> None: ...

    async def reset(self) -> None: ...

    async def apply(self, ops: list[dict]) -> tuple[int, str]: ...

    async def snapshot(self) -> dict: ...

    async def get_log(self, run_id: str | None, from_seq: int, max_records: int) -> list[LinkRecord]: ...

    def subscribe(self, listener: RunListener) -> None: ...


class LinkFactory(Protocol):
    """One per gateway: makes the links and finds executors (design 3.4).

    The gateway calls ``await start(on_executors)`` once, then ``link(name)`` for every executor
    it keeps a session for (configured, remembered or found), subscribes to each link and calls
    ``await link.start()`` on it once; at shutdown ``await link.close()`` on each, then
    ``await close()``.
    """

    async def start(self, on_executors: Callable[[list[str]], None]) -> None: ...

    def link(self, name: str) -> ExecutorLink: ...

    async def close(self) -> None: ...
