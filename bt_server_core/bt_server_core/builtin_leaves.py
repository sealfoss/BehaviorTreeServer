"""The built-in leaves that run without ROS (design 6.2, 6.3): Evaluation, Set, Print Message,
UDP Message, Restart, Pause and Stop. Field names and defaults come from :mod:`.node_types`.

Sub-Tree is run by the engine itself (design 6.7); Call Action, Call Service, Publish and Remote
Sub-Tree come from ``bt_server_executor`` (:meth:`Registry.register_ros_leaf`).
"""

from __future__ import annotations

import socket
from typing import Any

from .blackboard import OpError
from .leaves import InstantLeaf, LeafContext, LeafFailure, Status, ThreadLeaf
from .node_types import spec
from .treelog import LogLevel
from .values import OPERATORS, NotOrderable, compare, convert_literal, safe_repr

__all__ = [
    "BUILTIN_LEAF_CLASSES",
    "EvaluationLeaf",
    "PauseLeaf",
    "PrintMessageLeaf",
    "RestartLeaf",
    "SetLeaf",
    "StopLeaf",
    "UdpMessageLeaf",
    "value_text",
]

LITERAL_TYPES = ("Integer", "Double", "String", "Bool", "List", "Dictionary", "Set")


def _defaults(type_name: str) -> dict[str, Any]:
    node_type = spec(type_name)
    assert node_type is not None, type_name
    return node_type.defaults()


def value_text(value: Any) -> str:
    """How Print Message prints a value: text as it is, anything else as a Python literal."""
    return value if isinstance(value, str) else safe_repr(value)


class _Soft(Exception):  # noqa: N818
    """A failure that waiting can fix: an entry missing or without recent data."""


def _entry_name(ctx: LeafContext, field_name: str) -> str | None:
    value = ctx.field(field_name, None)
    return value if isinstance(value, str) and value else None


class EvaluationLeaf(InstantLeaf):
    """Compares an entry with another entry or a literal; with Wait, runs until it holds (6.3)."""

    TYPE_NAME = "Evaluation"
    title = "Evaluation"
    fields = _defaults("Evaluation")

    def on_start(self, ctx: LeafContext) -> None:
        self._wait_start: float | None = None
        self._timer: Any = None
        self._timed_out = False
        self._waiting_shown = False

    def tick(self, ctx: LeafContext) -> Any:
        try:
            return self._check(ctx)
        except LeafFailure:
            self._stop_waiting(ctx)
            raise

    def _check(self, ctx: LeafContext) -> Any:
        wait = ctx.field("Wait", False) is True
        operator = ctx.field("Operator", "==") or "=="
        if operator not in OPERATORS:
            raise LeafFailure(f"unknown operator {operator!r}; expected one of {' '.join(OPERATORS)}")
        try:
            holds, now_text = self._compare(ctx, operator)
        except _Soft as soft:
            if not wait:
                raise LeafFailure(str(soft)) from None
            return self._keep_waiting(ctx, operator, str(soft))
        if holds:
            self._stop_waiting(ctx)
            return True
        if not wait:
            return False
        return self._keep_waiting(ctx, operator, f"now {now_text}")

    def on_halt(self, ctx: LeafContext) -> None:
        self._stop_waiting(ctx)

    def _compare(self, ctx: LeafContext, operator: str) -> tuple[bool, str]:
        name = _entry_name(ctx, "ValueName")
        if name is None:
            raise LeafFailure("no blackboard value is selected (Value Name)")
        other_name = _entry_name(ctx, "CompareTo")
        board = ctx.blackboard
        with board.lock:
            if not board.has(name):
                raise _Soft(f"the blackboard has no entry named {name!r}")
            type_name = board.type_of(name)
            if other_name is None and type_name not in LITERAL_TYPES:
                raise LeafFailure(f"{type_name} entries cannot be used with a Literal Value; "
                                  "choose an entry in Compare To")
            if other_name is not None and not board.has(other_name):
                raise _Soft(f"the blackboard has no entry named {other_name!r} (Compare To)")
            for entry in (name, other_name):
                if entry is not None and board.freshness(entry) in ("nodata", "stale"):
                    raise _Soft("no recent data")
            value = board.get(name)
            other = board.get(other_name) if other_name is not None else convert_literal(
                ctx.field("LiteralValue", None), type_name)
        try:
            return compare(operator, value, other), safe_repr(value)
        except NotOrderable:
            raise LeafFailure(f"{type_name} values cannot be ordered ({operator})") from None

    def _keep_waiting(self, ctx: LeafContext, operator: str, seen: str) -> Status:
        now = ctx.ros_now()
        timeout = ctx.field("WaitTimeout", 0.0) or 0.0
        if self._wait_start is None:
            self._wait_start = now
            if timeout > 0:
                self._timer = ctx.scheduler.call_at(now + timeout, self._on_timeout, ros=True)
        elif timeout > 0 and (self._timed_out or now >= self._wait_start + timeout):
            self._stop_waiting(ctx)
            raise LeafFailure("timed out")
        other_name = _entry_name(ctx, "CompareTo")
        target = other_name if other_name is not None else safe_repr(self._literal(ctx))
        ctx.detail("waiting", f"waiting for {_entry_name(ctx, 'ValueName')} {operator} {target} ({seen})")
        self._waiting_shown = True
        return Status.RUNNING

    @staticmethod
    def _literal(ctx: LeafContext) -> Any:
        literal = ctx.field("LiteralValue", None)
        name = _entry_name(ctx, "ValueName")
        board = ctx.blackboard
        if name is not None and board.has(name):
            type_name = board.type_of(name)
            if type_name in LITERAL_TYPES:
                return convert_literal(literal, type_name)
        return literal

    def _on_timeout(self) -> None:
        """The wait timer fired: the next tick fails the node (no float comparison to miss)."""
        self._timer = None
        self._timed_out = True
        self.ctx.request_tick()

    def _stop_waiting(self, ctx: LeafContext) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._timed_out = False
        self._wait_start = None
        if self._waiting_shown:
            ctx.detail("waiting", None)
            self._waiting_shown = False


class SetLeaf(InstantLeaf):
    """Sets the Value Name entry to the Set To entry or the literal; refuses sourced entries."""

    TYPE_NAME = "Set"
    title = "Set"
    fields = _defaults("Set")

    def tick(self, ctx: LeafContext) -> Any:
        name = _entry_name(ctx, "ValueName")
        if name is None:
            raise LeafFailure("no blackboard value is selected (Value Name)")
        source_name = _entry_name(ctx, "CompareTo")
        board = ctx.blackboard
        with board.lock:
            if not board.has(name):
                raise LeafFailure(f"the blackboard has no entry named {name!r}")
            type_name = board.type_of(name)
            if source_name is not None:
                if not board.has(source_name):
                    raise LeafFailure(f"the blackboard has no entry named {source_name!r} (Set To)")
                value = board.get(source_name)
            elif type_name not in LITERAL_TYPES:
                raise LeafFailure(f"{type_name} entries cannot be set to a Literal Value; choose an entry in Set To")
            else:
                value = convert_literal(ctx.field("LiteralValue", None), type_name)
            try:
                ctx.set(name, value)
            except OpError as error:
                raise LeafFailure(str(error)) from None
        return True


class PrintMessageLeaf(InstantLeaf):
    """Writes the Message, or ``<entry> = <value>``, to the tree's log at Level."""

    TYPE_NAME = "Print Message"
    title = "Print Message"
    fields = _defaults("Print Message")

    def tick(self, ctx: LeafContext) -> Any:
        try:
            level = LogLevel.coerce(ctx.field("Level", int(LogLevel.DEBUG)))
        except (TypeError, ValueError):
            level = LogLevel.DEBUG
        if level is LogLevel.NONE:
            return True
        source = _entry_name(ctx, "Source")
        if source is None:
            message = ctx.field("Message", "")
            text = message if isinstance(message, str) else str(message)
        else:
            try:
                value = ctx.get(source)
            except KeyError:
                raise LeafFailure(f"the blackboard has no entry named {source!r} (Source)") from None
            text = f"{source} = {value_text(value)}"
        ctx.log(int(level), text)
        return True


class RestartLeaf(InstantLeaf):
    """Restarts the tree from the Root, as Reset does while it runs."""

    TYPE_NAME = "Restart"
    title = "Restart"

    def tick(self, ctx: LeafContext) -> Any:
        ctx.reset()
        return Status.RUNNING


class PauseLeaf(InstantLeaf):
    """Pauses the tree; when the tree is resumed, the node succeeds."""

    TYPE_NAME = "Pause"
    title = "Pause"

    def on_start(self, ctx: LeafContext) -> None:
        self._paused = False

    def tick(self, ctx: LeafContext) -> Any:
        if self._paused:
            return True
        self._paused = True
        ctx.pause()
        return Status.RUNNING


class StopLeaf(InstantLeaf):
    """Stops the tree, as the Stop button does."""

    TYPE_NAME = "Stop"
    title = "Stop"

    def tick(self, ctx: LeafContext) -> Any:
        ctx.stop()
        return Status.RUNNING


MIN_PORT = 1
MAX_PORT = 65535


def resolve_udp_address(host: str, port: int) -> tuple[int, int, tuple]:
    """``(family, protocol, address)`` to send UDP datagrams to ``host`` and ``port`` (IPv4 first).

    Raises:
        OSError: ``host`` cannot be resolved.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
    except (socket.gaierror, UnicodeError) as error:
        raise OSError(f"Destination IP {host!r} is not a valid address ({error})") from error
    if not infos:
        raise OSError(f"Destination IP {host!r} has no address")
    infos.sort(key=lambda info: info[0] != socket.AF_INET)
    family, _, protocol, _, address = infos[0]
    return family, protocol, address


class UdpMessageLeaf(ThreadLeaf):
    """Sends Message as one UDP datagram to Destination IP and Destination Port (the widget's)."""

    TYPE_NAME = "UDP Message"
    title = "UDP Message"
    fields = _defaults("UDP Message")
    SEND_TIMEOUT = 5.0

    def run(self, ctx: LeafContext) -> Any:
        host = ctx.field("Destination IP")
        port = ctx.field("Destination Port")
        message = ctx.field("Message")
        if not isinstance(host, str):
            raise TypeError(f"Destination IP must be a string, not {type(host).__name__}")
        if isinstance(port, bool) or not isinstance(port, int):
            raise TypeError(f"Destination Port must be an integer, not {type(port).__name__}")
        if not isinstance(message, str):
            raise TypeError(f"Message must be a string, not {type(message).__name__}")
        host = host.strip()
        if not host:
            raise ValueError("Destination IP is empty")
        if not MIN_PORT <= port <= MAX_PORT:
            raise ValueError(f"Destination Port must be between {MIN_PORT} and {MAX_PORT}, not {port}")
        data = message.encode("utf-8")
        family, protocol, address = resolve_udp_address(host, port)
        if ctx.cancel_requested():
            return False  # halted while the name was looked up: send nothing
        with socket.socket(family, socket.SOCK_DGRAM, protocol) as sock:
            sock.settimeout(self.SEND_TIMEOUT)
            if family == socket.AF_INET:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sent = sock.sendto(data, address)
        if sent != len(data):
            raise OSError(f"only {sent} of {len(data)} bytes were sent")
        return True


#: The classes of the built-in leaf types that run without ROS, by type name.
BUILTIN_LEAF_CLASSES: dict[str, type] = {
    cls.type_name(): cls
    for cls in (EvaluationLeaf, SetLeaf, PrintMessageLeaf, RestartLeaf, PauseLeaf, StopLeaf, UdpMessageLeaf)
}
