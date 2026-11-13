"""Leaves written for the widget (design 6.6): :class:`LeafNodeWidget` with the widget's names.

Supported: ``_title``, ``_fields``, ``TYPE_NAME``, ``RUN_IN_THREAD``, ``OnRun``, ``OnStart``,
``OnTerminate``, the field getters and setters (``GetField``, ``SetField``, ``GetFields``,
``SetFields``, ``GetFieldSelection``, ``GetFieldSelectionIndex``, ``SetFieldSelectionIndex``),
``GetTitle``, ``SetTitle``, ``GetId``, ``GetTypeName``, ``CancelRequested``, ``GetTree``, and on
``tree``: ``GetEntry``, ``SetEntry``, ``HasEntry``, ``GetEntryType``, ``Log``, ``Execute``,
``Pause``, ``Stop``, ``Reset``.

Not supported: ``UI_FILE`` (the browser builds the node from its fields; a warning is logged
when such a class is registered), anything that touches Qt, ``THREAD_WAIT`` (ignored: a finished
call requests a tick at once).

How such a leaf runs, as in the widget:

* ``RUN_IN_THREAD = True`` (the default): ``OnStart`` runs on the engine thread when the leaf
  starts, ``OnRun`` on a reused daemon worker thread. A halted call is abandoned at once: the
  node goes back to Ready (never Cancelling, it never holds up the tree), the call runs on with
  ``CancelRequested()`` true, its result is ignored, and a change it tries to make to the tree
  raises :class:`ExecutionCancelled`. The node does not start again until that call returned.
* ``RUN_IN_THREAD = False``: ``OnStart`` and ``OnRun`` run on the engine thread inside the tick.
  While ``OnRun`` returns Running it is called again once per tick interval (on the periodic
  ticks only), as in the widget. It must not block.
* ``OnTerminate(tree, status)`` runs on the engine thread once per start: ``NodeStatus.SUCCEEDED``
  or ``FAILED`` when it finished, ``NodeStatus.READY`` when it was halted.
* ``OnRun`` returns ``True`` / ``False`` or :class:`Status` ``SUCCESS`` / ``FAILURE`` /
  ``RUNNING`` (py_trees's statuses work too). Anything else, or an exception, fails the node.
* Leaf objects are made anew for each run: state kept on ``self`` does not survive a run.
"""

from __future__ import annotations

import functools
import inspect
from enum import Enum, StrEnum
from typing import Any, ClassVar

from ._engine_types import ExecutionCancelled, check_not_cancelled
from .leaves import Leaf, LeafContext
from .treelog import LogLevel

__all__ = ["ExecutionCancelled", "LeafNodeWidget", "LogLevel", "NodeStatus", "Status", "WidgetTree"]


class Status(Enum):
    """py_trees's statuses, under the names the widget's leaves return."""

    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    RUNNING = "RUNNING"
    INVALID = "INVALID"


class NodeStatus(StrEnum):
    """The status ``OnTerminate`` receives (the widget's names)."""

    READY = "Ready"
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"


@functools.cache
def _on_run_takes_tree(cls: type) -> bool:
    """True if ``cls.OnRun`` accepts the tree argument (``OnRun(self, tree)``)."""
    try:
        signature = inspect.signature(cls.OnRun)
    except (TypeError, ValueError):
        return True
    positional = 0
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            return True
        if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
            positional += 1
    return positional >= 2


class WidgetTree:
    """The ``tree`` argument of ``OnRun``, ``OnStart`` and ``OnTerminate``: the blackboard, the
    log and the commands of the tree that runs the leaf. Thread-safe."""

    def __init__(self, ctx: LeafContext) -> None:
        self._ctx = ctx

    def GetEntry(self, name: str) -> Any:  # noqa: N802 - the widget's names
        return self._ctx.get(name)

    def SetEntry(self, value: Any, name: str) -> None:  # noqa: N802
        check_not_cancelled("SetEntry")
        self._ctx.set(name, value)

    def HasEntry(self, name: str) -> bool:  # noqa: N802
        return self._ctx.has(name)

    def GetEntryType(self, name: str) -> str:  # noqa: N802
        return self._ctx.blackboard.type_of(name)

    def Log(self, level: Any, message: Any) -> bool:  # noqa: N802
        level = LogLevel.coerce(level)
        if level is LogLevel.NONE:
            return False
        self._ctx.log(int(level), str(message))
        return True

    def Execute(self) -> None:  # noqa: N802
        self._ctx.execute()

    def Pause(self) -> None:  # noqa: N802
        self._ctx.pause()

    def Stop(self) -> None:  # noqa: N802
        self._ctx.stop()

    def Reset(self) -> None:  # noqa: N802
        self._ctx.reset()


class LeafNodeWidget(Leaf):
    """The base class of leaves written for the widget (see the module docs)."""

    ABSTRACT = True
    UI_FILE: ClassVar[str] = ""
    RUN_IN_THREAD: ClassVar[bool] = True
    THREAD_WAIT: ClassVar[float] = 0.02
    _title: ClassVar[str | None] = None
    _fields: ClassVar[dict[str, Any]] = {}

    _tree: WidgetTree | None = None

    # ----------------------------------------------------------------- the class as a type
    @classmethod
    def default_title(cls) -> str:
        title = cls._title
        return title if isinstance(title, str) and title else cls.type_name()

    @classmethod
    def field_defaults(cls) -> dict[str, Any]:
        import copy

        return copy.deepcopy(dict(cls._fields))

    @classmethod
    def leaf_kind(cls) -> str:
        return "thread" if cls.RUN_IN_THREAD else "instant"

    # ----------------------------------------------------------------- what the engine calls
    def widget_tree(self) -> WidgetTree:
        """The ``tree`` passed to the hooks."""
        if self._tree is None:
            self._tree = WidgetTree(self.ctx)
        return self._tree

    def timeout(self, ctx: LeafContext) -> float:
        """No engine deadline: a widget leaf's field named Timeout means what its own code says."""
        return 0.0

    def call_on_run(self) -> Any:
        """``OnRun(tree)`` (or ``OnRun()`` for overrides without the argument)."""
        if _on_run_takes_tree(type(self)):
            return self.OnRun(self.widget_tree())
        return self.OnRun()

    # ----------------------------------------------------------------- the widget's hooks
    def OnRun(self, tree: WidgetTree) -> Any:  # noqa: N802
        """Do the work. Override in subclasses."""
        raise NotImplementedError(f"{type(self).__name__} does not implement OnRun")

    def OnStart(self, tree: WidgetTree) -> None:  # noqa: N802
        """Called on the engine thread when the node starts, before ``OnRun``."""

    def OnTerminate(self, tree: WidgetTree, status: NodeStatus) -> None:  # noqa: N802
        """Called on the engine thread once when the node stops running."""

    # ----------------------------------------------------------------- the widget's node API
    def GetTree(self) -> WidgetTree:  # noqa: N802
        return self.widget_tree()

    def GetId(self) -> str:  # noqa: N802
        return self.ctx.id

    def GetTypeName(self) -> str:  # noqa: N802
        return self.type_name()

    def GetTitle(self) -> str:  # noqa: N802
        return self.ctx.title

    def SetTitle(self, title: str) -> None:  # noqa: N802
        self.ctx.set_title(str(title))

    def GetFields(self) -> dict:  # noqa: N802
        return self.ctx.fields

    def GetField(self, key: str) -> Any:  # noqa: N802
        return self.ctx.field(key)

    def SetField(self, key: str, value: Any) -> None:  # noqa: N802
        self.ctx.set_field(key, value)

    def SetFields(self, fields: dict) -> None:  # noqa: N802
        if not isinstance(fields, dict):
            raise TypeError(f"fields must be a dict, not {type(fields).__name__}")
        for key, value in fields.items():
            self.ctx.set_field(key, value)

    def GetFieldSelectionIndex(self, key: str) -> int:  # noqa: N802
        options = self.ctx.field(key)
        if not isinstance(options, list):
            raise TypeError(f"field {key!r} is not a list")
        if not options:
            return -1
        return min(max(self.ctx._host.selection_index(key), 0), len(options) - 1)

    def GetFieldSelection(self, key: str) -> Any:  # noqa: N802
        return self.ctx.selection(key)

    def SetFieldSelectionIndex(self, key: str, index: int) -> None:  # noqa: N802
        check_not_cancelled("SetFieldSelectionIndex")
        options = self.ctx.field(key)
        if not isinstance(options, list):
            raise TypeError(f"field {key!r} is not a list")
        if not 0 <= index < len(options):
            raise IndexError(f"index {index} out of range for field {key!r}")
        self.ctx._host.set_selection(key, index)

    def CancelRequested(self) -> bool:  # noqa: N802
        return self.ctx.cancel_requested()
