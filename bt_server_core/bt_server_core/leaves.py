"""Leaf base classes (design 6.1, 6.6) and the context leaf code works through.

Three kinds of leaves, by where their work runs:

* :class:`InstantLeaf` runs inside the tick on the engine thread: :meth:`InstantLeaf.tick`
  returns the result at once (``True``, ``False`` or a :class:`Status`; ``Status.RUNNING`` keeps
  it running, and it is ticked again on the next tick). It is never cancelled and never waits
  for cancels. Evaluation, Set, Print Message, Restart, Pause and Stop are instant leaves.
* :class:`AsyncLeaf` is *started* after the tick, on the engine thread, and must not block:
  :meth:`AsyncLeaf.on_start` sends its request; the leaf calls :meth:`AsyncLeaf.succeed` or
  :meth:`AsyncLeaf.fail` when the answer comes (from any thread; a tick is requested).
  When halted, :meth:`AsyncLeaf.on_cancel` sends the cancel and the leaf calls
  :meth:`AsyncLeaf.settled` once it is confirmed; until then the node shows Cancelling (design
  5.6). :meth:`AsyncLeaf.on_end` is always called last: finished, failed or cancelled.
* :class:`ThreadLeaf` runs :meth:`ThreadLeaf.run`, a plain function, on a reused daemon worker
  thread. When halted, ``ctx.cancel_requested()`` becomes true and the node shows Cancelling
  until the function returns.

An exception in leaf code, or a wrong return value, fails the node: the message goes to its
details and to the log at ERROR (design 5.7). ``raise LeafFailure(reason)`` fails it with a
reason and no ERROR record, as ``AsyncLeaf.fail(reason)`` does.

Class attributes (all optional)::

    TYPE_NAME = "Beep"          # the type name in files; default: the class name (not inherited)
    title = "Beep"              # the default title; default: the type name
    fields = {"times": 1}       # field name -> default: int, float, str, bool, or a list of
                                # texts (a choice; ``ctx.selection(name)`` is the chosen one)
    WAITS_FOR_CANCELS = True    # started leaves: does a start wait for earlier cancels (5.6)?
    ABSTRACT = True             # set on a base class of your own: it is not registered

Leaf objects are made anew for each run. Inside a leaf, ``self.ctx`` is its
:class:`LeafContext` and ``self.get(name)`` a field's value.

:class:`LeafContext` (what ``ctx`` is)::

    ctx.get(name) / ctx.has(name) / ctx.set(name, value)   # the blackboard (ctx.blackboard)
    ctx.fields / ctx.field(name) / ctx.selection(name)     # field values, bindings resolved
    ctx.outputs                                            # {source path: entry name}
    ctx.write_output(path, value) / ctx.write_outputs(data)
    ctx.log(level, text) / ctx.detail(kind, value)         # the tree log, the node's details
    ctx.set_field(name, value) / ctx.set_title(title)      # go out as "field" / "title" details
    ctx.stop() / ctx.pause() / ctx.reset() / ctx.execute() # commands to the tree that runs it
    ctx.cancel_requested()
    ctx.scheduler / ctx.now() / ctx.ros_now() / ctx.request_tick() / ctx.call_soon(fn)
    ctx.run                                                # the runner's run_context
    ctx.node                                               # ctx.run.node (the rclpy node), or None
    ctx.index / ctx.path / ctx.id / ctx.tree / ctx.title / ctx.type_name

``get``, ``has``, ``set``, ``log``, ``detail``, ``set_field``, ``set_title``, the commands and
``cancel_requested`` are safe from any thread; ``set``, ``set_field``, ``set_title`` and the
commands raise :class:`ExecutionCancelled` in a call that was cancelled.

A field bound to a blackboard entry (``bindings`` of the node, design 6.4) takes the entry's
value when the leaf starts; a binding to a missing entry, or to a sourced entry without recent
data, fails the leaf when it starts. ``outputs`` copy values the other way:
:meth:`LeafContext.write_outputs` takes the leaf's data (for example ``{"Result": {...}}``) and
writes the parts the node's ``outputs`` name to their entries, creating missing ones.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar

from ._engine_types import ExecutionCancelled, Status, check_not_cancelled, current_token
from .node_types import FieldSpec
from .values import field_kind_of

if TYPE_CHECKING:  # pragma: no cover
    from .blackboard import Blackboard
    from .scheduler import Scheduler

__all__ = [
    "AsyncLeaf",
    "ExecutionCancelled",
    "InstantLeaf",
    "Leaf",
    "LeafContext",
    "LeafFailure",
    "Status",
    "ThreadLeaf",
    "get_path",
    "set_path",
    "split_path",
]

_MISSING = object()
_PATH_PART = re.compile(r"\.?([^.\[\]]+)|\[(\d+)\]")


class LeafFailure(Exception):  # noqa: N818 - reads as "raise LeafFailure(reason)"
    """Raised by leaf code to fail its node with a reason: the reason goes to the node's
    details and its log line, without the ERROR record and traceback of an unexpected error."""


def split_path(path: str) -> list[str | int]:
    """``"Goal.waypoints[0].x"`` -> ``["Goal", "waypoints", 0, "x"]`` (contract 2.1).

    Raises:
        ValueError: a malformed path.
    """
    if not isinstance(path, str) or not path or path.startswith("."):
        raise ValueError(f"malformed field path {path!r}")
    parts: list[str | int] = []
    position = 0
    for match in _PATH_PART.finditer(path):
        dotted = match.group(0).startswith(".")
        if match.start() != position or (match.group(1) is not None and dotted != bool(parts)):
            raise ValueError(f"malformed field path {path!r}")
        parts.append(match.group(1) if match.group(1) is not None else int(match.group(2)))
        position = match.end()
    if position != len(path) or not parts or isinstance(parts[0], int):
        raise ValueError(f"malformed field path {path!r}")
    return parts


def get_path(data: Any, parts: list[str | int]) -> Any:
    """The value at ``parts`` inside ``data``. Raises KeyError, IndexError or TypeError."""
    for part in parts:
        data = data[part]
    return data


def set_path(data: Any, parts: list[str | int], value: Any) -> Any:
    """``data`` with the value at ``parts`` replaced (dictionaries on the way are created; the
    containers changed are copies). Raises KeyError, IndexError or TypeError."""
    if not parts:
        return value
    head, rest = parts[0], parts[1:]
    if isinstance(head, int):
        if not isinstance(data, list):
            raise TypeError(f"[{head}] needs a list")
        result = list(data)
        result[head] = set_path(result[head], rest, value)
        return result
    result = dict(data) if isinstance(data, dict) else {}
    result[head] = set_path(result.get(head, {}), rest, value)
    return result


class Leaf:
    """The base of every leaf class (see the module docs). Subclass :class:`InstantLeaf`,
    :class:`AsyncLeaf` or :class:`ThreadLeaf`, not this class."""

    ABSTRACT: ClassVar[bool] = True
    KIND: ClassVar[str] = ""
    TYPE_NAME: ClassVar[str | None] = None
    title: ClassVar[str | None] = None
    fields: ClassVar[dict[str, Any]] = {}
    WAITS_FOR_CANCELS: ClassVar[bool] = True

    _ctx: LeafContext | None = None

    # ----------------------------------------------------------------- the class as a type
    @classmethod
    def type_name(cls) -> str:
        """The name of the type: ``TYPE_NAME`` of the class itself, or the class name."""
        return cls.__dict__.get("TYPE_NAME") or cls.__name__

    @classmethod
    def default_title(cls) -> str:
        title = cls.__dict__.get("title") or cls.title
        return title if isinstance(title, str) and title else cls.type_name()

    @classmethod
    def field_defaults(cls) -> dict[str, Any]:
        """The declared fields and their defaults (a copy)."""
        return copy.deepcopy(dict(cls.fields))

    @classmethod
    def field_specs(cls) -> tuple[FieldSpec, ...]:
        """The declared fields as catalog field specs (design 6.4)."""
        return tuple(FieldSpec(name, field_kind_of(default), copy.deepcopy(default))
                     for name, default in cls.field_defaults().items())

    @classmethod
    def leaf_kind(cls) -> str:
        """``instant``, ``async`` or ``thread`` (the catalog's ``leaf_kind``)."""
        return cls.KIND

    @classmethod
    def waits_for_cancels(cls) -> bool:
        return bool(cls.WAITS_FOR_CANCELS)

    # ----------------------------------------------------------------- one leaf of a run
    @property
    def ctx(self) -> LeafContext:
        """The context of this leaf in its run (set before any of its methods is called)."""
        if self._ctx is None:
            raise RuntimeError(f"{type(self).__name__} is not part of a run")
        return self._ctx

    def get(self, name: str, default: Any = _MISSING) -> Any:
        """The value of field ``name`` (bindings resolved)."""
        return self.ctx.field(name, default)

    def timeout(self, ctx: LeafContext) -> float:
        """Seconds the work of a started leaf may take, counted from its real start on the ROS
        clock; 0 for no limit. By default the field ``Timeout`` if the type has one."""
        value = ctx.field("Timeout", 0.0)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0.0
        return max(0.0, float(value))


class InstantLeaf(Leaf):
    """A leaf whose work is done inside the tick (see the module docs)."""

    ABSTRACT = True
    KIND = "instant"
    WAITS_FOR_CANCELS = False

    def on_start(self, ctx: LeafContext) -> None:
        """Called when the node starts anew (it was not running), before :meth:`tick`."""

    def tick(self, ctx: LeafContext) -> Any:
        """Do the work; return ``True``, ``False`` or a :class:`Status`."""
        raise NotImplementedError(f"{type(self).__name__} does not implement tick")

    def on_halt(self, ctx: LeafContext) -> None:
        """Called when the node is halted while it returned Running."""


class AsyncLeaf(Leaf):
    """A started leaf whose work runs on the engine thread without blocking (see the module docs)."""

    ABSTRACT = True
    KIND = "async"

    def on_start(self, ctx: LeafContext) -> None:
        """Start the work (after the tick, on the engine thread). Must not block."""

    def on_cancel(self, ctx: LeafContext) -> None:
        """Cancel the work; call :meth:`settled` once that is confirmed. By default nothing is
        waited for."""
        self.settled()

    def on_end(self, ctx: LeafContext) -> None:
        """Always called last, on the engine thread: finished, failed or cancelled."""

    def succeed(self) -> None:
        """The work succeeded (any thread; requests a tick)."""
        self.ctx._host.leaf_result(Status.SUCCEEDED, None)

    def fail(self, reason: str | None = None) -> None:
        """The work failed; ``reason`` goes to the node's details and the log (any thread)."""
        self.ctx._host.leaf_result(Status.FAILED, reason)

    def settled(self) -> None:
        """The cancel is confirmed (any thread)."""
        self.ctx._host.leaf_settled()


class ThreadLeaf(Leaf):
    """A started leaf whose work is a plain function on a worker thread (see the module docs)."""

    ABSTRACT = True
    KIND = "thread"

    def run(self, ctx: LeafContext) -> Any:
        """Do the work on a worker thread; return ``True``, ``False`` or a :class:`Status`
        (``RUNNING`` runs the function again at the next periodic tick)."""
        raise NotImplementedError(f"{type(self).__name__} does not implement run")


class LeafContext:
    """What leaf code works through (see the module docs). Made by the engine, one per leaf
    node of a run; ``host`` is the engine's node."""

    def __init__(self, host: Any) -> None:
        self._host = host

    # ----------------------------------------------------------------- identity
    @property
    def index(self) -> int:
        return self._host.index

    @property
    def path(self) -> str:
        return self._host.path

    @property
    def id(self) -> str:
        return self._host.node_id

    @property
    def tree(self) -> str:
        """The key of the tree document the node belongs to."""
        return self._host.tree_key

    @property
    def title(self) -> str:
        return self._host.title

    @property
    def type_name(self) -> str:
        return self._host.type_name

    @property
    def run(self) -> Any:
        """The runner's ``run_context`` (the rclpy node, the executor's name, the run id), or None."""
        return self._host.run_context

    @property
    def node(self) -> Any:
        """``run.node`` (the executor's rclpy node), or None."""
        return getattr(self._host.run_context, "node", None)

    @property
    def scheduler(self) -> Scheduler:
        return self._host.scheduler

    def now(self) -> float:
        return self._host.scheduler.now()

    def ros_now(self) -> float:
        return self._host.scheduler.ros_now()

    # ----------------------------------------------------------------- blackboard
    @property
    def blackboard(self) -> Blackboard:
        return self._host.blackboard

    def get(self, name: str) -> Any:
        """An entry's value. Raises KeyError."""
        return self._host.blackboard.get(name)

    def has(self, name: str) -> bool:
        return self._host.blackboard.has(name)

    def set(self, name: str, value: Any) -> None:
        """Set an entry, creating it if needed. Raises OpError for an entry with a source."""
        check_not_cancelled("set")
        self._host.blackboard.set(name, value)

    # ----------------------------------------------------------------- fields
    @property
    def fields(self) -> dict[str, Any]:
        """The node's field values, bindings resolved (a copy)."""
        return copy.deepcopy(self._host.field_values())

    def field(self, name: str, default: Any = _MISSING) -> Any:
        """The value of field ``name`` (a choice field: its list of options). Raises KeyError."""
        values = self._host.field_values()
        if name in values:
            return copy.deepcopy(values[name])
        if default is _MISSING:
            raise KeyError(f"{self._host.type_name} has no field {name!r}")
        return default

    def selection(self, name: str) -> Any:
        """The chosen option of a choice field (None for an empty list)."""
        options = self.field(name)
        if not isinstance(options, list):
            raise TypeError(f"field {name!r} is not a choice")
        if not options:
            return None
        index = self._host.selection_index(name)
        return options[min(max(index, 0), len(options) - 1)]

    def set_field(self, name: str, value: Any) -> None:
        """Change a field of the node; it goes out as a ``field`` detail."""
        check_not_cancelled("set_field")
        self._host.post(lambda: self._host.set_field(name, value))

    def set_title(self, title: str) -> None:
        """Change the node's title; it goes out as a ``title`` detail."""
        check_not_cancelled("set_title")
        self._host.post(lambda: self._host.set_title(str(title)))

    # ----------------------------------------------------------------- outputs
    @property
    def outputs(self) -> dict[str, str]:
        """``{source path: entry name}`` of the node."""
        return dict(self._host.outputs)

    def write_output(self, source_path: str, value: Any) -> bool:
        """Write ``value`` to the entry the node's ``outputs`` name for ``source_path``; False
        if none does. Raises OpError when the entry cannot take the value."""
        entry = self._host.outputs.get(source_path)
        if entry is None:
            return False
        self.set(entry, value)
        return True

    def write_outputs(self, data: dict) -> list[str]:
        """Write every output whose source path is found in ``data`` (``{"Result": {...}}``).
        Returns the problems (a path not found, a value an entry cannot take)."""
        problems = []
        for source_path, entry in self._host.outputs.items():
            try:
                value = get_path(data, split_path(source_path))
            except (KeyError, IndexError, TypeError, ValueError):
                problems.append(f"{source_path} is not in the data")
                continue
            try:
                self.set(entry, value)
            except (ValueError, TypeError) as error:
                problems.append(f"output {source_path} -> {entry}: {error}")
        return problems

    # ----------------------------------------------------------------- log and details
    def log(self, level: int, text: str) -> None:
        """Write to the tree's log at ``level`` (1 ERROR ... 4 INFO); never blocks."""
        text = str(text)
        self._host.post(lambda: self._host.log(int(level), text))

    def detail(self, kind: str, value: Any) -> None:
        """Set a detail of the node (``value`` None clears it)."""
        self._host.post(lambda: self._host.detail(kind, value))

    # ----------------------------------------------------------------- the tree
    def stop(self) -> None:
        """Stop the tree, as the Stop button does."""
        self._command("stop")

    def pause(self) -> None:
        self._command("pause")

    def reset(self) -> None:
        """Restart the tree from the Root (a Reset while it runs)."""
        self._command("reset")

    def execute(self) -> None:
        """Resume a paused tree."""
        self._command("resume")

    def _command(self, name: str) -> None:
        check_not_cancelled(name.capitalize())
        self._host.command(name, current_token())

    def cancel_requested(self) -> bool:
        """True once the current work should stop early (Stop, Reset, a halt). In a call on a
        worker thread: the cancellation of that very call."""
        token = current_token() or self._host.token
        return token is not None and token.is_cancelled()

    def request_tick(self) -> None:
        """Ask for a tick (any thread)."""
        self._host.post(self._host.request_tick)

    def call_soon(self, fn: Callable[[], None]) -> None:
        """Run ``fn`` on the engine thread (any thread)."""
        self._host.scheduler.call_soon(fn)
