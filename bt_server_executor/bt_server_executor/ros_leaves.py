"""The ROS leaves (design 6.5, 6.8): Call Action, Call Service, Publish and Remote Sub-Tree.

All four are :class:`~bt_server_core.leaves.AsyncLeaf` classes: their work runs on the engine
thread, which is also where rclpy delivers replies, feedback and results, and nothing blocks.
Their fields are those of ``bt_server_core.node_types``; bound fields (``Goal.pose.pose``) are
filled from the blackboard by the engine when the leaf starts, which also fails the node with
the reason when a binding names a missing or stale entry, before anything is sent (L-14).
Giving up while active (*Timeout*, *Feedback Timeout*, a lost server) goes through the engine's
``cancel_and_fail``: the node shows Cancelling while the goal is cancelled, then Failed.

**Type names** are checked by :mod:`.ros_types` before anything is imported (L-15). Goals,
requests and messages are built with ``rosidl_runtime_py.set_message_fields`` after every key of
the field values is checked to be a field of the interface.

**Run setup.** ``prepare(ctx)``, called by the executor before the first tick, creates the
action client, service client or publisher through the run's client cache, so the ROS network
has time to connect them (8.2); a leaf whose literal fields do not name a usable target skips
this and fails with the reason when it starts.

**Call Action** (the table of 6.5):

* Start: the leaf looks for the server every 100 ms, for 2 s at most ("server not available"),
  then journals the goal (8.4) and sends it.
* Goal reply within 5 s ("no reply to the goal"); rejected: "goal rejected". A goal accepted
  after the leaf gave up on it, or after it was halted, is cancelled at once (L-04, L-08).
* Running: each feedback message renews the liveness time; the node's ``feedback`` detail and
  its ``Feedback.`` outputs are updated at most 10 times a second (the latest message is never
  dropped). Once a second the leaf checks that the server is still on the network. *Feedback
  Timeout* (ROS clock) gives up with "no feedback", a server gone from the network with "server
  lost" (its cancel is then confirmed by nobody: it times out, and the journal keeps sending it).
* Result: succeeded -> the ``Result.`` outputs are written and the node succeeds; aborted ->
  "aborted"; cancelled without a halt -> "cancelled by the server".
* Halted: a cancel is sent (once the goal reply is there), and sent again every second while
  it has no reply, until the tree's cancel timeout (a server that has not matched this client
  yet drops its reply); the leaf settles when the result arrives, whatever its status, or at
  once when the server says it does not know the goal. A refused cancel leaves it Cancelling until the result comes or
  the engine's cancel timeout makes the goal unsettled; the journal then sends the cancel again
  until the goal ends, also after the run.
* *Timeout* is the engine's deadline: the node shows Cancelling, then Failed "timed out".

**Call Service**: the service is looked for as an action server is ("service not available"
after 2 s); the reply's bool *Success Field* (default ``success``) false fails the node with the
reply's ``message`` as the reason; ``Response.`` outputs are written on success. A halt (also
the *Timeout*) drops the pending request (``Client.remove_pending_request``), ignores a late
reply and settles at once (L-11).

**Publish**: reliable, history 10, transient local with *Keep For Late Joiners*. With *Wait For
Subscribers* above 0 the leaf runs until that many subscriptions are matched, and fails if they
are not within 5 s.

**Remote Sub-Tree** (6.8): sends the tree of its *File* (with the trees it uses, from the run's
bundle, Loop Execution off) as an ``ExecuteTree`` goal to *Executor*, with the remote tree's
blackboard file whose bound entries hold this tree's current values, and names this executor's
``~/status`` and run id as the watchdog (*Watchdog* seconds; 0: none). Before sending it looks at
the other executor's ``~/status``: a run there fails the node "executor busy" and sends nothing.
A rejected goal: "the tree was refused". While it runs it behaves as Call Action does, but fails
"executor lost" at once when that executor's ``~/status`` has been silent for 3 s (or its action
server left the network), without waiting for a cancel nobody can confirm (M-07); the remote
tree's watchdog stops it there. Bound values come from ``ctx.bound_values()``. The
``remote_run`` detail names the remote run. When the remote run ends,
succeeded or not, the ``outputs`` (remote entry -> local entry) are written from its final
blackboard and the remote Root's result is the node's.

A late result, reply or feedback of an earlier goal of the same leaf is recognised by its goal
object and never taken for the current one's.
"""

from __future__ import annotations

import time
import uuid
import weakref
from collections.abc import Callable
from typing import Any, ClassVar

from action_msgs.msg import GoalStatus
from action_msgs.srv import CancelGoal
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from unique_identifier_msgs.msg import UUID

from bt_server_core.bundle import sub_bundle
from bt_server_core.files import PathError, subtree_key
from bt_server_core.leaves import AsyncLeaf, LeafContext, LeafFailure, get_path, split_path
from bt_server_core.node_types import spec
from bt_server_core.treelog import LogLevel
from bt_server_core.values import decode_value, dumps_json, encode_value, loads_json

from bt_server_interfaces.action import ExecuteTree
from bt_server_interfaces.msg import TreeStatus

from . import ros_types
from .qos import WATCHDOG_QOS

__all__ = [
    "CallActionLeaf",
    "CallServiceLeaf",
    "PublishLeaf",
    "RemoteSubTreeLeaf",
    "register",
]

#: Fixed limits of design 6.5 and 6.8 (steady clock).
SERVER_WAIT_S = 2.0
SERVER_POLL_S = 0.1
GOAL_REPLY_S = 5.0
LIVENESS_PERIOD_S = 1.0
FEEDBACK_PERIOD_S = 0.1
SUBSCRIBERS_WAIT_S = 5.0
EXECUTOR_SILENCE_S = 3.0
#: A cancel without a reply is sent again after this long, until it is answered or the tree's
#: cancel timeout passes (then the journal goes on): a server that has not matched this client
#: yet drops its reply, and cancelling a goal twice does no harm.
CANCEL_RESEND_S = 1.0
#: A client that has just found its server waits this long before its first request: the server
#: may not have found the client's reply reader yet, and a reply sent before that is lost.
FIRST_MATCH_GRACE_S = 0.3

#: An executor's ~/status heard this recently tells its state (it is published at least every 0.5 s).
STATUS_RECENT_S = 1.5

_first_ready: weakref.WeakKeyDictionary[Any, float] = weakref.WeakKeyDictionary()


def register(registry: Any) -> None:
    """Give the ROS node types their classes (the executor calls this once)."""
    for cls in (CallActionLeaf, CallServiceLeaf, PublishLeaf, RemoteSubTreeLeaf):
        registry.register_ros_leaf(cls.TYPE_NAME, cls)


# ============================================================================== helpers
class _Timers:
    """Scheduler calls of one piece of work, cancelled together."""

    def __init__(self, scheduler: Any) -> None:
        self._scheduler = scheduler
        self._handles: set[Any] = set()

    def now(self) -> float:
        return self._scheduler.now()

    def after(self, delay: float, fn: Callable[[], None]) -> Any:
        """``fn`` after ``delay`` seconds of the steady clock."""
        return self.at(self._scheduler.now() + delay, fn)

    def at(self, when: float, fn: Callable[[], None], ros: bool = False) -> Any:
        holder: list[Any] = []

        def fire() -> None:
            self._handles.discard(holder[0])
            fn()

        handle = self._scheduler.call_at(when, fire, ros=ros)
        holder.append(handle)
        self._handles.add(handle)
        return handle

    def cancel(self, handle: Any) -> None:
        if handle is not None and handle in self._handles:
            self._handles.discard(handle)
            handle.cancel()

    def cancel_all(self) -> None:
        for handle in list(self._handles):
            handle.cancel()
        self._handles.clear()


def _run(ctx: LeafContext) -> Any:
    """The executor's RunContext. Raises LeafFailure without ROS."""
    run = ctx.run
    if run is None or getattr(run, "node", None) is None:
        raise LeafFailure(f"{ctx.type_name} needs ROS: it runs only in an executor under ROS")
    return run


def _name_field(ctx: LeafContext, field: str, what: str) -> str:
    value = ctx.field(field, "")
    if not isinstance(value, str) or not value.strip():
        raise LeafFailure(f"no {what} is chosen ({field})")
    return value.strip()


def _interface(ctx: LeafContext, kind: str) -> Any:
    try:
        return ros_types.load_interface(ctx.field("Type", ""), kind)
    except ValueError as error:
        raise LeafFailure(f"Type: {error}") from error


def _message(cls: Any, values: Any, what: str) -> Any:
    try:
        return ros_types.build_message(cls, values, what)
    except ValueError as error:
        raise LeafFailure(f"the {what} does not fit the type: {error}") from error


def _write_outputs(ctx: LeafContext, data: dict, prefix: str | None = None) -> None:
    """Write the outputs found in ``data`` (those under ``prefix`` only, when given); problems
    are logged as WARNINGs."""
    for path, entry in ctx.outputs.items():
        if prefix is not None and path != prefix and not path.startswith(prefix + ".") \
                and not path.startswith(prefix + "["):
            continue
        try:
            value = get_path(data, split_path(path))
        except (KeyError, IndexError, TypeError, ValueError):
            ctx.log(LogLevel.WARNING, f"Output {path} -> {entry}: not in the {prefix or 'result'}")
            continue
        try:
            ctx.write_output(path, value)
        except (TypeError, ValueError) as error:
            ctx.log(LogLevel.WARNING, f"Output {path} -> {entry}: {error}")


def _matched(client: Any, ready: bool) -> bool:
    """``ready`` (the client sees its server), and for long enough that the server sees it too."""
    if not ready:
        _first_ready.pop(client, None)
        return False
    first = _first_ready.setdefault(client, time.monotonic())
    return time.monotonic() - first >= FIRST_MATCH_GRACE_S


def _wait_until(timers: _Timers, ready: Callable[[], bool], limit: float, then: Callable[[], None],
                give_up: Callable[[], None]) -> None:
    """Check ``ready()`` now and every 100 ms for ``limit`` seconds; ``then()`` once it holds,
    ``give_up()`` if it never does."""
    deadline = timers.now() + limit

    def check() -> None:
        if ready():
            then()
        elif timers.now() >= deadline:
            give_up()
        else:
            timers.after(SERVER_POLL_S, check)

    check()


class _RosLeaf(AsyncLeaf):
    """What the four ROS leaves share: their declared fields come from ``node_types``."""

    ABSTRACT = True
    TYPE_NAME: ClassVar[str | None] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        declared = spec(cls.__dict__.get("TYPE_NAME") or "")
        if declared is not None:
            cls.fields = declared.defaults()
            cls.title = declared.title


# ============================================================================== action goals
class _Goal:
    """One goal sent by a leaf (Call Action, Remote Sub-Tree). It lives until the goal settles,
    also after its leaf has ended, so that a late reply is cancelled and a late result reaches
    the journal; it tells its leaf only while it is the leaf's current goal."""

    #: Texts of the failures (Remote Sub-Tree words them its own way).
    NOT_AVAILABLE = "server not available"
    REJECTED = "goal rejected"
    LOST = "server lost"

    def __init__(self, leaf: _ActionLeafBase, ctx: LeafContext, client: Any, action_name: str, goal: Any) -> None:
        self.leaf = leaf
        self.ctx = ctx
        self.run = ctx.run
        self.client = client
        self.action_name = action_name
        self.goal = goal
        self.goal_id = uuid.uuid4()
        self.timers = _Timers(ctx.scheduler)
        self.state = "waiting"  # waiting -> sent -> running -> over
        self.handle: Any = None
        self.halted = False      # the leaf was halted: it settles when the goal is over
        self.gave_up = False     # no reply in time: a late acceptance is cancelled
        self.reported = False    # the leaf has been told how this goal went
        self.cancel_sent = False
        self.cancel_answered = False
        self.cancel_until = 0.0
        self.reply_timer: Any = None
        self.feedback_timeout = 0.0
        self.last_feedback = 0.0
        self.latest_feedback: Any = None
        self.feedback_timer: Any = None
        self.last_flush = float("-inf")

    # ----------------------------------------------------------------- telling the leaf
    def _current(self) -> bool:
        return not self.reported and self.leaf._goal is self

    def succeed(self) -> None:
        if self._current() and not self.halted:
            self.reported = True
            self.leaf.succeed()

    def fail(self, reason: str) -> None:
        if self._current() and not self.halted:
            self.reported = True
            self.leaf.fail(reason)

    def settle(self) -> None:
        if self._current() and self.halted:
            self.reported = True
            self.leaf.settled()

    # ----------------------------------------------------------------- starting
    def start(self) -> None:
        _wait_until(self.timers, self.ready_to_send, SERVER_WAIT_S, self.send, self.not_available)

    def ready_to_send(self) -> bool:
        return _matched(self.client, self.client.server_is_ready())

    def not_available(self) -> None:
        self.state = "over"
        self.fail(self.NOT_AVAILABLE)

    def send(self) -> None:
        self.run.record_goal(self.action_name, self.goal_id)
        self.state = "sent"
        future = self.client.send_goal_async(self.goal, feedback_callback=self._on_feedback,
                                             goal_uuid=UUID(uuid=list(self.goal_id.bytes)))
        future.add_done_callback(self._on_goal_reply)
        self.reply_timer = self.timers.after(GOAL_REPLY_S, self._on_no_reply)

    def _on_no_reply(self) -> None:
        self.reply_timer = None
        if self.state != "sent":
            return
        self.gave_up = True
        self.run.goal_cancel_sent(self.goal_id)  # the journal keeps it open, then unsettled
        self.fail("no reply to the goal")
        self.settle()  # halted before a reply that never came: a late acceptance gets its cancel

    def _on_goal_reply(self, future: Any) -> None:
        self.timers.cancel(self.reply_timer)
        self.reply_timer = None
        problem = self.REJECTED
        try:
            handle = future.result()
        except Exception as error:  # noqa: BLE001 - the request failed on its way
            handle, problem = None, f"the goal could not be sent: {error}"
        if handle is None or not handle.accepted:
            self.state = "over"
            self.run.goal_done(self.goal_id)
            self.settle()
            self.fail(problem)
            return
        self.handle = handle
        self.state = "running"
        handle.get_result_async().add_done_callback(self._on_result)
        if self.halted or self.gave_up:
            self.cancel()  # halted before the reply (L-08), or accepted after the leaf gave up (L-04)
            return
        self.accepted()
        self.timers.after(LIVENESS_PERIOD_S, self._check_liveness)
        if self.feedback_timeout > 0:
            self.last_feedback = self.ctx.ros_now()
            self._arm_feedback_timeout()

    def accepted(self) -> None:
        """The goal was accepted (for subclasses)."""

    # ----------------------------------------------------------------- running
    def _check_liveness(self) -> None:
        if self.state != "running" or self.halted:
            return
        problem = self.liveness_problem()
        if problem is not None:
            self.lost(problem)
            return
        self.timers.after(LIVENESS_PERIOD_S, self._check_liveness)

    def liveness_problem(self) -> str | None:
        """Why the goal can no longer end well, or None."""
        return None if self.client.server_is_ready() else self.LOST

    def lost(self, reason: str) -> None:
        """The server is gone: the node shows Cancelling while a cancel is tried, then Failed with
        ``reason`` (a gone server confirms nothing: the cancel times out and the journal keeps
        sending it until the server is back and says the goal is over)."""
        self.ctx.log(LogLevel.WARNING, f"{self.action_name}: {reason}")
        self.give_up(reason)

    def give_up(self, reason: str) -> None:
        """Cancel the goal; the node fails with ``reason`` once that is settled (design 6.5)."""
        if self._current() and not self.halted:
            self.leaf.cancel_and_fail(reason)  # the engine calls on_cancel: see halt()

    def _arm_feedback_timeout(self) -> None:
        when = self.last_feedback + self.feedback_timeout
        self.timers.at(when, self._on_feedback_timeout, ros=True)

    def _on_feedback_timeout(self) -> None:
        if self.state != "running" or self.halted:
            return
        if self.ctx.ros_now() < self.last_feedback + self.feedback_timeout:
            self._arm_feedback_timeout()  # feedback came meanwhile
            return
        self.give_up("no feedback")

    def _on_feedback(self, message: Any) -> None:
        if self.state != "running" or self.halted or self.reported:
            return
        self.last_feedback = self.ctx.ros_now()
        self.latest_feedback = ros_types.to_python(message.feedback)
        if self.feedback_timer is not None:
            return
        wait = self.last_flush + FEEDBACK_PERIOD_S - self.ctx.now()
        if wait <= 0:
            self._flush_feedback()
        else:
            self.feedback_timer = self.timers.after(wait, self._flush_feedback)

    def _flush_feedback(self) -> None:
        """The node's details and outputs get the latest feedback (at most 10 times a second)."""
        self.feedback_timer = None
        if self.state != "running" or self.halted or self.reported:
            return
        self.last_flush = self.ctx.now()
        self.ctx.detail("feedback", self.latest_feedback)
        self.feedback_written(self.latest_feedback)

    def feedback_written(self, feedback: Any) -> None:
        """Also act on a feedback message (for subclasses)."""

    # ----------------------------------------------------------------- cancelling
    def halt(self) -> None:
        """The leaf was halted, or gives up (its Timeout, no feedback, a lost server): cancel,
        settle when the goal is over."""
        self.halted = True
        if self.state == "waiting":
            self.timers.cancel_all()
            self.state = "over"
            self.settle()
        elif self.state == "running":
            self.cancel()
        elif self.state == "over":
            self.settle()
        # "sent": the cancel goes out when the reply comes (L-08)

    def cancel(self) -> None:
        if self.handle is None or self.cancel_sent:
            return
        self.cancel_sent = True
        self.cancel_until = self.timers.now() + self.run.cancel_timeout_s
        self.run.goal_cancel_sent(self.goal_id)
        self._send_cancel()

    def _send_cancel(self) -> None:
        """Send the cancel, and again every :data:`CANCEL_RESEND_S` while it has no reply."""
        if self.state == "over" or self.cancel_answered:
            return
        self.handle.cancel_goal_async().add_done_callback(self._on_cancel_reply)
        if self.timers.now() + CANCEL_RESEND_S < self.cancel_until:
            self.timers.after(CANCEL_RESEND_S, self._send_cancel)

    def _on_cancel_reply(self, future: Any) -> None:
        try:
            response = future.result()
        except Exception:  # noqa: BLE001 - the request was dropped: it is sent again
            return
        if response is None:
            return
        self.cancel_answered = True
        code = response.return_code
        if code == CancelGoal.Response.ERROR_REJECTED:
            self.ctx.log(LogLevel.WARNING, f"{self.action_name}: the server refused to cancel the goal")
        elif code == CancelGoal.Response.ERROR_UNKNOWN_GOAL_ID and self.state != "over":
            # The server does not know the goal (it was restarted, or forgot a finished goal):
            # nothing runs there for it, and no result will come.
            self.ctx.log(LogLevel.INFO, f"{self.action_name}: the server does not know the goal: settled")
            self.run.goal_done(self.goal_id)
            self.state = "over"
            self.timers.cancel_all()
            self.settle()
        # ERROR_GOAL_TERMINATED: the goal is over and its result is on its way (_on_result settles)

    # ----------------------------------------------------------------- the end
    def _on_result(self, future: Any) -> None:
        self.run.goal_done(self.goal_id)
        self.state = "over"
        self.timers.cancel_all()
        try:
            response = future.result()
        except Exception as error:  # noqa: BLE001 - the result request failed
            self.settle()
            self.fail(f"no result: {error}")
            return
        if self.halted:
            self.settle()
        elif not self.reported and self.leaf._goal is self:
            self.finished(response.status, response.result)

    def finished(self, status: int, result: Any) -> None:
        """The result of a goal that was not halted (for subclasses)."""
        if status == GoalStatus.STATUS_SUCCEEDED:
            _write_outputs(self.ctx, {"Result": ros_types.to_python(result)}, "Result")
            self.succeed()
        elif status == GoalStatus.STATUS_ABORTED:
            self.fail("aborted")
        elif status == GoalStatus.STATUS_CANCELED:
            self.fail("cancelled by the server")
        else:
            self.fail(f"the goal ended with status {status}")

    def leaf_ended(self) -> None:
        """The leaf's work is over (``on_end``): its timers stop; the goal's own callbacks stay."""
        self.timers.cancel_all()
        self.feedback_timer = None


class _ActionLeafBase(_RosLeaf):
    """A leaf that runs one goal at a time (:class:`_Goal`)."""

    ABSTRACT = True
    _goal: _Goal | None = None

    def on_cancel(self, ctx: LeafContext) -> None:
        if self._goal is None:
            self.settled()
        else:
            self._goal.halt()

    def on_end(self, ctx: LeafContext) -> None:
        if self._goal is not None:
            self._goal.leaf_ended()
            self._goal = None


# ============================================================================== Call Action
class _ActionGoal(_Goal):
    def feedback_written(self, feedback: Any) -> None:
        _write_outputs(self.ctx, {"Feedback": feedback}, "Feedback")


class CallActionLeaf(_ActionLeafBase):
    """Sends a goal to a ROS action server and waits for the result (see the module docs)."""

    TYPE_NAME = "CallAction"
    ABSTRACT = False

    def prepare(self, ctx: LeafContext) -> None:
        try:
            cls = ros_types.load_interface(ctx.field("Type", ""), "action")
        except ValueError:
            return  # it fails with the reason when it starts
        name = ctx.field("Action", "")
        if isinstance(name, str) and name.strip():
            _run(ctx).action_client(cls, name.strip())

    def on_start(self, ctx: LeafContext) -> None:
        run = _run(ctx)
        cls = _interface(ctx, "action")
        name = _name_field(ctx, "Action", "action")
        goal = _message(cls.Goal, ctx.field("Goal", {}), "goal")
        feedback_timeout = ctx.field("Feedback Timeout", 0.0)
        self._goal = _ActionGoal(self, ctx, run.action_client(cls, name), name, goal)
        if isinstance(feedback_timeout, (int, float)) and not isinstance(feedback_timeout, bool):
            self._goal.feedback_timeout = max(0.0, float(feedback_timeout))
        self._goal.start()


# ============================================================================== Call Service
class _ServiceCall:
    """One request of a Call Service leaf."""

    def __init__(self, leaf: CallServiceLeaf, ctx: LeafContext, client: Any, request: Any) -> None:
        self.leaf = leaf
        self.ctx = ctx
        self.client = client
        self.request = request
        self.timers = _Timers(ctx.scheduler)
        self.future: Any = None
        self.over = False

    def start(self) -> None:
        _wait_until(self.timers, lambda: _matched(self.client, self.client.service_is_ready()), SERVER_WAIT_S,
                    self._send, lambda: self._report(False, "service not available"))

    def _send(self) -> None:
        self.future = self.client.call_async(self.request)
        self.future.add_done_callback(self._on_reply)

    def _report(self, ok: bool, reason: str | None = None) -> None:
        if self.over or self.leaf._call is not self:
            return
        self.over = True
        if ok:
            self.leaf.succeed()
        else:
            self.leaf.fail(reason)

    def _on_reply(self, future: Any) -> None:
        if self.over or self.leaf._call is not self:
            return  # a late reply of a halted call is ignored
        try:
            response = future.result()
        except Exception as error:  # noqa: BLE001 - the call failed on its way
            self._report(False, f"the call failed: {error}")
            return
        fields = response.get_fields_and_field_types()
        success_field = self.ctx.field("Success Field", "success")
        if isinstance(success_field, str) and fields.get(success_field.strip()) == "boolean" \
                and getattr(response, success_field.strip()) is False:
            message = getattr(response, "message", "") if fields.get("message") == "string" else ""
            self._report(False, message or f"the reply's {success_field.strip()} is false")
            return
        _write_outputs(self.ctx, {"Response": ros_types.to_python(response)}, "Response")
        self._report(True)

    def halt(self) -> None:
        """ROS 2 cannot cancel a call: the pending request is dropped and a late reply ignored."""
        self.over = True
        self.timers.cancel_all()
        if self.future is not None and not self.future.done():
            self.client.remove_pending_request(self.future)


class CallServiceLeaf(_RosLeaf):
    """Calls a ROS service and waits for the reply (see the module docs)."""

    TYPE_NAME = "CallService"
    ABSTRACT = False
    _call: _ServiceCall | None = None

    def prepare(self, ctx: LeafContext) -> None:
        try:
            cls = ros_types.load_interface(ctx.field("Type", ""), "srv")
        except ValueError:
            return
        name = ctx.field("Service", "")
        if isinstance(name, str) and name.strip():
            _run(ctx).service_client(cls, name.strip())

    def on_start(self, ctx: LeafContext) -> None:
        run = _run(ctx)
        cls = _interface(ctx, "srv")
        name = _name_field(ctx, "Service", "service")
        request = _message(cls.Request, ctx.field("Request", {}), "request")
        self._call = _ServiceCall(self, ctx, run.service_client(cls, name), request)
        self._call.start()

    def on_cancel(self, ctx: LeafContext) -> None:
        if self._call is not None:
            self._call.halt()
        self.settled()

    def on_end(self, ctx: LeafContext) -> None:
        if self._call is not None:
            self._call.timers.cancel_all()
            self._call = None


# ============================================================================== Publish
class PublishLeaf(_RosLeaf):
    """Publishes one message on a ROS topic (see the module docs)."""

    TYPE_NAME = "Publish"
    ABSTRACT = False
    _timers: _Timers | None = None

    @staticmethod
    def _qos(ctx: LeafContext) -> QoSProfile:
        """Reliable, history 10; with Keep For Late Joiners transient local with a history of 1, so
        a late subscriber gets the last message only, not older ones of earlier runs (the cached
        publisher outlives the run)."""
        if ctx.field("Keep For Late Joiners", False) is True:
            return QoSProfile(history=HistoryPolicy.KEEP_LAST, depth=1, reliability=ReliabilityPolicy.RELIABLE,
                              durability=DurabilityPolicy.TRANSIENT_LOCAL)
        return QoSProfile(history=HistoryPolicy.KEEP_LAST, depth=10, reliability=ReliabilityPolicy.RELIABLE,
                          durability=DurabilityPolicy.VOLATILE)

    def prepare(self, ctx: LeafContext) -> None:
        try:
            cls = ros_types.load_interface(ctx.field("Type", ""), "msg")
        except ValueError:
            return
        topic = ctx.field("Topic", "")
        if isinstance(topic, str) and topic.strip():
            _run(ctx).publisher(cls, topic.strip(), self._qos(ctx))

    def on_start(self, ctx: LeafContext) -> None:
        run = _run(ctx)
        cls = _interface(ctx, "msg")
        topic = _name_field(ctx, "Topic", "topic")
        message = _message(cls, ctx.field("Message", {}), "message")
        publisher = run.publisher(cls, topic, self._qos(ctx))
        wanted = ctx.field("Wait For Subscribers", 0)
        wanted = int(wanted) if isinstance(wanted, (int, float)) and not isinstance(wanted, bool) else 0
        timers = self._timers = _Timers(ctx.scheduler)

        def publish() -> None:
            if self._timers is timers:
                publisher.publish(message)
                self.succeed()

        def too_few() -> None:
            if self._timers is timers:
                self.fail(f"{publisher.get_subscription_count()} of {wanted} subscriber(s) after "
                          f"{SUBSCRIBERS_WAIT_S:g} s")

        if wanted <= 0:
            publish()
        else:
            _wait_until(timers, lambda: publisher.get_subscription_count() >= wanted, SUBSCRIBERS_WAIT_S,
                        publish, too_few)

    def on_cancel(self, ctx: LeafContext) -> None:
        self.on_end(ctx)
        self.settled()

    def on_end(self, ctx: LeafContext) -> None:
        if self._timers is not None:
            self._timers.cancel_all()
            self._timers = None


# ============================================================================== Remote Sub-Tree
class _StatusWatcher:
    """The ``~/status`` of another executor: one volatile subscription per executor for the life of
    the node (an old message kept for late joiners must not count as a sign of life).

    ``last``/``last_time``: the latest message, of any run (the executor's state now).
    ``going[run id]``: when a message last showed that run going (its id, a state other than
    IDLE), as an executor's watchdog counts them (8.2): an executor restarted at once publishes
    messages, but none of them for the run it lost."""

    _watchers: ClassVar[weakref.WeakKeyDictionary[Any, dict[str, _StatusWatcher]]] = weakref.WeakKeyDictionary()
    #: How many runs' signs of life are kept (the latest).
    KEPT_RUNS = 16

    def __init__(self, node: Any, topic: str) -> None:
        self.last: Any = None
        self.last_time: float | None = None
        self.going: dict[str, float] = {}
        self.subscription = node.create_subscription(TreeStatus, topic, self._on_status, WATCHDOG_QOS)

    @classmethod
    def of(cls, node: Any, executor: str) -> _StatusWatcher:
        topic = f"{executor.rstrip('/')}/status"
        watchers = cls._watchers.setdefault(node, {})
        watcher = watchers.get(topic)
        if watcher is None:
            watcher = watchers[topic] = cls(node, topic)
        return watcher

    def _on_status(self, message: Any) -> None:
        now = time.monotonic()
        self.last, self.last_time = message, now
        if message.state != message.STATE_IDLE:
            run_id = bytes(bytearray(message.run_id.uuid)).hex()
            self.going.pop(run_id, None)
            self.going[run_id] = now
            while len(self.going) > self.KEPT_RUNS:
                self.going.pop(next(iter(self.going)))


def _bundle(run: Any) -> dict:
    """The bundle of the run: ``run.bundle``, else the one of the executor node's current run.
    Raises LeafFailure."""
    bundle = getattr(run, "bundle", None)
    if isinstance(bundle, dict) and bundle:  # RunContext.bundle is {} when the executor gave none
        return bundle
    text = getattr(getattr(run.node, "current_run", None), "bundle_json", None)
    try:
        bundle = loads_json(text) if isinstance(text, str) else None
    except ValueError:
        bundle = None
    if not isinstance(bundle, dict):
        raise LeafFailure("the run's bundle is not available to the Remote Sub-Tree")
    return bundle


class _RemoteGoal(_Goal):
    NOT_AVAILABLE = "executor not available"
    REJECTED = "the tree was refused"
    LOST = "executor lost"

    def __init__(self, *args: Any, executor: str, watcher: _StatusWatcher) -> None:
        super().__init__(*args)
        self.executor = executor
        self.watcher = watcher
        self.started_at = time.monotonic()
        self.accepted_at = self.started_at

    def ready_to_send(self) -> bool:
        heard = self.watcher.last_time is not None and self.watcher.last_time >= self.started_at - 1.0
        return _matched(self.client, self.client.server_is_ready()) and heard

    def not_available(self) -> None:
        if self.client.server_is_ready():
            self.send()  # its ~/status was not heard; the goal itself will tell
        else:
            super().not_available()

    def send(self) -> None:
        status, heard = self.watcher.last, self.watcher.last_time
        recent = heard is not None and heard >= time.monotonic() - STATUS_RECENT_S
        if status is not None and recent and status.state != status.STATE_IDLE:
            self.state = "over"
            self.fail("executor busy")
            return
        super().send()

    def accepted(self) -> None:
        self.accepted_at = time.monotonic()
        self.ctx.detail("remote_run", {"executor": self.executor, "run_id": self.goal_id.hex})
        self.timers.at(self._silence_deadline(), self._check_silence)

    def _silence_deadline(self) -> float:
        """3 s after the other executor's ``~/status`` last showed this run going (or after the
        goal was accepted, before it first did)."""
        heard = self.watcher.going.get(self.goal_id.hex)
        return (heard if heard is not None else self.accepted_at) + EXECUTOR_SILENCE_S

    def lost(self, reason: str) -> None:
        """The other executor is gone: fail now (M-07: within 3 s of its last heartbeat). Its own
        watchdog stops the remote tree; the journal keeps sending the cancel until it is back."""
        self.ctx.log(LogLevel.WARNING, f"{self.executor}: {reason}")
        self.cancel()
        self.fail(reason)

    def _check_silence(self) -> None:
        if self.state != "running" or self.halted:
            return
        if time.monotonic() >= self._silence_deadline():
            self.lost(self.LOST)
        else:
            self.timers.at(self._silence_deadline(), self._check_silence)

    def finished(self, status: int, result: Any) -> None:
        try:
            entries = loads_json(result.blackboard_json) if result.blackboard_json else []
        except ValueError:
            entries = []
        values = {}
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                try:
                    values[entry["name"]] = decode_value(entry.get("value"))
                except ValueError:
                    continue
        _write_outputs(self.ctx, values)
        if result.outcome == result.OUTCOME_SUCCEEDED:
            self.succeed()
        elif result.outcome == result.OUTCOME_FAILED:
            self.fail("the remote tree failed")
        elif result.outcome == result.OUTCOME_STOPPED:
            self.fail(f"the remote tree was stopped: {result.message}")
        else:
            self.fail(f"the remote tree ended with an error: {result.message}")


class RemoteSubTreeLeaf(_ActionLeafBase):
    """Runs a tree on another executor (see the module docs)."""

    TYPE_NAME = "RemoteSubTree"
    ABSTRACT = False
    _run_bundle: dict | None = None

    @staticmethod
    def _action(executor: str) -> str:
        return f"{executor.rstrip('/')}/execute"

    def prepare(self, ctx: LeafContext) -> None:
        executor = ctx.field("Executor", "")
        if isinstance(executor, str) and executor.strip():
            run = _run(ctx)
            run.action_client(ExecuteTree, self._action(executor.strip()))
            _StatusWatcher.of(run.node, executor.strip())

    def on_start(self, ctx: LeafContext) -> None:
        run = _run(ctx)
        executor = _name_field(ctx, "Executor", "executor")
        file_name = ctx.field("File", "")
        try:
            key = subtree_key(ctx.tree, file_name if isinstance(file_name, str) else "")
        except PathError as error:
            raise LeafFailure(str(error)) from error
        if self._run_bundle is None:
            self._run_bundle = _bundle(run)  # parsed once per run: leaf objects are made per run
        bundle = self._run_bundle
        if key not in bundle.get("trees", {}):
            raise LeafFailure(f"the remote tree '{file_name}' is missing or not a tree file")
        part = sub_bundle(bundle, key)
        trees = dict(part["trees"])
        root = dict(trees[key])
        root["config"] = {**(root.get("config") if isinstance(root.get("config"), dict) else {}), "repeat": False}
        trees[key] = root
        part["trees"] = trees
        watchdog = ctx.field("Watchdog", 5.0)
        watchdog = float(watchdog) if isinstance(watchdog, (int, float)) and not isinstance(watchdog, bool) else 5.0
        goal = ExecuteTree.Goal(
            bundle_json=dumps_json(part, indent=None),
            blackboard_json=dumps_json(self._remote_blackboard(ctx, bundle, key), indent=None),
            label=key, requested_by=run.executor_name,
            watchdog_topic=run.status_topic if watchdog > 0 else "",
            watchdog_run_id=UUID(uuid=list(run.run_uuid)), watchdog_timeout_s=max(0.0, watchdog))
        client = run.action_client(ExecuteTree, self._action(executor))
        self._goal = _RemoteGoal(self, ctx, client, self._action(executor), goal, executor=executor,
                                 watcher=_StatusWatcher.of(run.node, executor))
        self._goal.start()

    @staticmethod
    def _remote_blackboard(ctx: LeafContext, bundle: dict, key: str) -> list[dict]:
        """The remote tree's blackboard file, its bound entries (remote name -> local name)
        holding this tree's current values. Raises LeafFailure for a missing or stale entry."""
        entries = [dict(entry) for entry in bundle.get("blackboards", {}).get(key, ()) if isinstance(entry, dict)]
        values = ctx.bound_values()
        for remote_name, local_name in ctx.bindings.items():
            value = encode_value(values[remote_name])
            entry = next((item for item in entries if item.get("name") == remote_name), None)
            if entry is None:
                entries.append({"name": remote_name, "type": ctx.blackboard.type_of(local_name), "value": value})
            else:
                entry["value"] = value
        return entries
