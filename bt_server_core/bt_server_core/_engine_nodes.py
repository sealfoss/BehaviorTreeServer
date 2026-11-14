"""The nodes of a run (internal; used by :class:`bt_server_core.engine.TreeRunner`).

What each node does when ticked is design 5.2 and 5.3; the lifecycle of started leaves is design
5.6. Statuses live in the runner's ``statuses`` array; a node changes its own entry through
``run.set_status``, which records the change for the report of the tick.

Two status rules (design 5.2): a composite that starts anew first sets every node below it to
Ready, except a leaf that is still Cancelling (:meth:`Node.reset_subtree`); halting a node sets it
and every node below it to Ready, also the ones that had finished, while active started leaves
pass through Cancelling (:meth:`Node.halt`).
"""

from __future__ import annotations

import copy
import threading
import traceback
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any

from ._engine_types import RunToken, Status, bound, describe_error, result_status
from ._engine_workers import leaf_pool
from .compat import LeafNodeWidget, NodeStatus
from .leaves import Leaf, LeafContext, LeafFailure, set_path, split_path
from .treelog import LogLevel
from .values import encode_value

if TYPE_CHECKING:  # pragma: no cover
    from .engine import TreeRunner

RUNNING, SUCCEEDED, FAILED, READY, CANCELLING = (
    Status.RUNNING, Status.SUCCEEDED, Status.FAILED, Status.READY, Status.CANCELLING)

#: Node types whose ``bindings`` map entries to entries (remote entry -> local entry), not field
#: paths to entries (contract 2.1).
ENTRY_BINDING_TYPES = frozenset({"RemoteSubTree"})

#: Lifecycle states of a started leaf (design 5.6). UNSETTLED: its cancel timed out and its work
#: has not settled yet; DONE: it finished in this start.
IDLE, WAITING, ACTIVE, CANCELLING_STATE, UNSETTLED, DONE = "idle", "waiting", "active", "cancelling", "unsettled", "done"


class Node:
    """A node of a run: its place, its label for the log, and the status rules."""

    def __init__(self, run: TreeRunner, index: int, path: str, data: dict, tree_key: str, label: str) -> None:
        self.run = run
        self.index = index
        self.path = path
        self.data = data
        self.tree_key = tree_key
        self.node_id = str(data.get("id", ""))
        self.type_name = str(data.get("type", ""))
        title = data.get("title")
        self.title = title if isinstance(title, str) and title else self.type_name
        self.label = label
        self.children: list[Node] = []
        self.ancestors: tuple[Node, ...] = ()

    @property
    def status(self) -> Status:
        return Status(self.run.statuses[self.index])

    def set_status(self, status: Status) -> None:
        self.run.set_status(self.index, status)

    def tick(self) -> Status:
        raise NotImplementedError

    def needs_halt(self) -> bool:
        """True while halting this node would stop something (it runs, or waits to start)."""
        return self.status == RUNNING

    def halt(self) -> None:
        """Halt this node and everything below it: all go to Ready (active leaves to Cancelling)."""
        for child in self.children:
            child.halt()
        if self.status == RUNNING:
            self.run.note_halted(self)
        self.set_status(READY)
        self.on_reset()

    def reset_subtree(self) -> None:
        """An ancestor starts anew: this node and its descendants go to Ready (except leaves
        still Cancelling)."""
        self.set_status(READY)
        self.on_reset()
        for child in self.children:
            child.reset_subtree()

    def reset_below(self) -> None:
        for child in self.children:
            child.reset_subtree()

    def on_reset(self) -> None:
        """Forget the progress of the node's current run."""

    def finish(self, before: Status, status: Status, reason: str | None = None) -> Status:
        if reason:
            self.run.detail(self.index, "error", reason)
        self.set_status(status)
        self.run.note(self, before, status, reason)
        return status

    def leaves(self) -> list[LeafNode]:
        result = []
        stack: list[Node] = [self]
        while stack:
            node = stack.pop()
            if isinstance(node, LeafNode):
                result.append(node)
            stack.extend(reversed(node.children))
        return result


class CompositeNode(Node):
    """Root, Sequence or Selector, with or without Memory (design 5.2)."""

    def __init__(self, *args: Any, selector: bool, memory: bool) -> None:
        super().__init__(*args)
        self.selector = selector
        self.memory = memory
        self.current = 0

    def on_reset(self) -> None:
        self.current = 0

    def tick(self) -> Status:
        before = self.status
        if before != RUNNING:
            self.run.clear_details(self.index)
            self.reset_below()
            self.current = 0
        stop_on = SUCCEEDED if self.selector else FAILED
        start = self.current if self.memory else 0
        for position in range(start, len(self.children)):
            result = self.children[position].tick()
            if self.run.aborting:
                # The tick ends here (a command from node code); with Memory the next tick resumes
                # at this child, or after it when it already passed.
                passed = result not in (RUNNING, stop_on)
                self.current = position + 1 if passed else position
                return self.finish(before, RUNNING)
            if result == RUNNING or result == stop_on:
                self.current = position
                for later in self.children[position + 1:]:
                    if later.needs_halt():
                        later.halt()
                return self.finish(before, result)
        self.current = len(self.children)
        return self.finish(before, FAILED if self.selector else SUCCEEDED)


class ParallelNode(Node):
    """Parallel with the policy All or Any (design 5.3, py_trees's rule)."""

    def __init__(self, *args: Any, policy: str) -> None:
        super().__init__(*args)
        self.policy = policy
        self.results: list[Status | None] = []

    def on_reset(self) -> None:
        self.results = [None] * len(self.children)

    def tick(self) -> Status:
        before = self.status
        if before != RUNNING:
            self.run.clear_details(self.index)
            self.reset_below()
            self.results = [None] * len(self.children)
        if not self.children:
            return self.finish(before, SUCCEEDED)
        for position, child in enumerate(self.children):
            if self.results[position] is not None:
                continue  # a finished child keeps its result
            result = child.tick()
            if result != RUNNING:
                self.results[position] = result
            if self.run.aborting:
                return self.finish(before, RUNNING)
        if FAILED in self.results:
            outcome = FAILED
        elif self.policy == "any" and SUCCEEDED in self.results:
            outcome = SUCCEEDED
        elif self.policy == "all" and all(result == SUCCEEDED for result in self.results):
            outcome = SUCCEEDED
        else:
            outcome = RUNNING
        if outcome != RUNNING:
            for position, child in enumerate(self.children):
                if self.results[position] is None and child.needs_halt():
                    child.halt()
        return self.finish(before, outcome)


class DecoratorNode(Node):
    """Negation (inverts its child) and Sub-Tree (runs a sub-tree's Root, design 6.7). A node
    with ``problem`` fails with it when executed."""

    def __init__(self, *args: Any, invert: bool, problem: str | None = None) -> None:
        super().__init__(*args)
        self.invert = invert
        self.problem = problem

    def tick(self) -> Status:
        before = self.status
        if before != RUNNING:
            self.run.clear_details(self.index)
        if self.problem is not None or not self.children:
            return self.finish(before, FAILED, self.problem or "a Negation needs a child: connect the node whose "
                                                              "result it inverts")
        if before != RUNNING:
            self.reset_below()
        result = self.children[0].tick()
        if self.run.aborting:
            return self.finish(before, RUNNING)
        if self.invert and result != RUNNING:
            result = FAILED if result == SUCCEEDED else SUCCEEDED
        return self.finish(before, result)


class FailingNode(Node):
    """A leaf that cannot run (an unknown type, a ROS type without ROS): fails with ``reason``."""

    def __init__(self, *args: Any, reason: str) -> None:
        super().__init__(*args)
        self.reason = reason

    def tick(self) -> Status:
        before = self.status
        if before != RUNNING:
            self.run.clear_details(self.index)
        return self.finish(before, FAILED, self.reason)


class LeafNode(Node):
    """A leaf of a run: drives its :class:`~bt_server_core.leaves.Leaf` object through the
    lifecycle of its kind, and is the host of its :class:`LeafContext`.

    Kinds: ``instant`` (InstantLeaf), ``inline`` (a widget leaf with ``RUN_IN_THREAD = False``),
    ``async`` (AsyncLeaf), ``thread`` (ThreadLeaf), ``widget_thread`` (a widget leaf on a worker
    thread, abandoned at once when halted).
    """

    def __init__(self, *args: Any, leaf_class: type[Leaf], fields: dict, selections: dict, bindings: dict,
                 outputs: dict) -> None:
        super().__init__(*args)
        self.lock = threading.Lock()  # fields and values are changed from worker threads too
        self.leaf_class = leaf_class
        self.fields = fields
        self.values = copy.deepcopy(fields)
        self.selections = selections
        self.bindings = bindings
        self.outputs = outputs
        widget = issubclass(leaf_class, LeafNodeWidget)
        base_kind = leaf_class.leaf_kind()
        if widget:
            self.kind = "widget_thread" if base_kind == "thread" else "inline"
        else:
            self.kind = base_kind
        self.state = IDLE
        self.pending: Status | None = None
        self.reason: str | None = None
        self.token: RunToken | None = None
        self.future: Future | None = None
        self.outstanding: Future | None = None  # an abandoned call that must return first
        self.watched: Future | None = None  # a call of an earlier run this node waits for
        self.deadline: Any = None
        self.cancel_timer: Any = None
        self.cancel_generation = 0
        self.fail_after_cancel: str | None = None  # a deadline, or cancel_and_fail(reason)
        self.was_running = False
        self.started = False  # an AsyncLeaf's on_start ran: on_end is due
        self.rerun = False
        self.leaf = leaf_class()
        self.ctx = LeafContext(self)
        self.leaf._ctx = self.ctx

    # ----------------------------------------------------------------- host of the LeafContext
    @property
    def run_context(self) -> Any:
        return self.run.run_context

    @property
    def scheduler(self) -> Any:
        return self.run.scheduler

    @property
    def blackboard(self) -> Any:
        return self.run.blackboard

    def field_values(self) -> dict:
        with self.lock:
            return self.values

    def selection_index(self, name: str) -> int:
        index = self.selections.get(name, 0)
        return index if isinstance(index, int) and not isinstance(index, bool) else 0

    def set_selection(self, name: str, index: int) -> None:
        """Select an option of a choice field, at once (any thread)."""
        with self.lock:
            self.selections[name] = index

    def set_field(self, name: str, value: Any) -> None:
        """Change a field at once (any thread), as the widget's ``SetField`` does; the ``field``
        detail is reported on the engine thread."""
        with self.lock:
            self.fields[name] = value
            self.values = {**self.values, name: value}
        try:
            encoded = encode_value(value)
        except TypeError:
            encoded = None
        detail = {"tree": self.tree_key, "id": self.node_id, "field": name, "value": encoded}
        self.post(lambda: self.run.detail(self.index, "field", detail))

    def set_title(self, title: str) -> None:
        """Change the title at once (any thread); the ``title`` detail is reported on the engine thread."""
        with self.lock:
            self.title = title
        detail = {"tree": self.tree_key, "id": self.node_id, "title": title}
        self.post(lambda: self.run.detail(self.index, "title", detail))

    def post(self, fn: Any) -> None:
        self.run.post(fn)

    def log(self, level: int, text: str) -> None:
        self.run.log(level, text, self.index)

    def detail(self, kind: str, value: Any) -> None:
        self.run.detail(self.index, kind, value)

    def command(self, name: str, token: RunToken | None) -> None:
        self.run.node_command(name, token)

    def request_tick(self) -> None:
        self.run.request_tick()

    def leaf_result(self, status: Status, reason: str | None) -> None:
        self.post(lambda: self.on_result(status, reason))

    def leaf_settled(self) -> None:
        self.post(self.on_settled)

    # ----------------------------------------------------------------- options
    def waits_for_cancels(self) -> bool:
        if self.kind in ("instant", "inline") or not self.leaf_class.waits_for_cancels():
            return False
        option = self.data.get("wait_for_cancels")
        if isinstance(option, bool):
            return option
        return bool(self.run.config["wait_for_cancels"])

    def cancel_timeout(self) -> float:
        option = self.data.get("cancel_timeout_ms")
        if isinstance(option, (int, float)) and not isinstance(option, bool) and option >= 0:
            return option / 1000.0
        return self.run.config["cancel_timeout_ms"] / 1000.0

    # ----------------------------------------------------------------- ticks
    def needs_halt(self) -> bool:
        if self.kind in ("instant", "inline"):
            return self.status == RUNNING
        return self.state in (WAITING, ACTIVE) or (self.state == UNSETTLED and self.status == CANCELLING)

    def tick(self) -> Status:
        if self.kind in ("instant", "inline"):
            return self._tick_instant()
        return self._tick_started()

    def binds_fields(self) -> bool:
        """True when the node's bindings are field paths; a Remote Sub-Tree's map remote entries
        to local entries (contract 2.1) and are given to the leaf as they are."""
        return self.type_name not in ENTRY_BINDING_TYPES

    def bound_values(self) -> dict[str, Any]:
        """``{binding key: value of its entry}`` (copies). Raises LeafFailure for a missing entry
        or a sourced entry without recent data, as when the leaf starts."""
        board = self.run.blackboard
        values = {}
        with board.lock:
            for key, entry in self.bindings.items():
                if not board.has(entry):
                    raise LeafFailure(f"the binding of {key} names the missing entry {entry!r}")
                if board.freshness(entry) in ("nodata", "stale"):
                    raise LeafFailure(f"the entry {entry!r} bound to {key} has no recent data")
                values[key] = board.get(entry)
        return values

    def _resolve(self) -> str | None:
        """Take the values of the bound fields from the blackboard; the failure reason, or None."""
        values = copy.deepcopy(self.fields)
        if not self.binds_fields():
            try:
                self.bound_values()  # checked when the leaf starts; the leaf reads them itself
            except LeafFailure as failure:
                return str(failure)
            with self.lock:
                self.values = values
            return None
        board = self.run.blackboard
        for path, entry in self.bindings.items():
            try:
                parts = split_path(path)
            except ValueError as error:
                return str(error)
            with board.lock:
                if not board.has(entry):
                    return f"the binding of {path} names the missing entry {entry!r}"
                if board.freshness(entry) in ("nodata", "stale"):
                    return f"the entry {entry!r} bound to {path} has no recent data"
                value = board.get(entry)
            try:
                values = set_path(values, parts, value)
            except (KeyError, IndexError, TypeError) as error:
                return f"the binding of {path} cannot be applied: {error}"
        with self.lock:
            self.values = values
        return None

    def _tick_instant(self) -> Status:
        before = self.status
        widget = self.kind == "inline"
        if before != RUNNING:
            self.run.clear_details(self.index)
            reason = self._resolve()
            if reason is not None:
                return self._finish_instant(before, FAILED, reason)
            try:
                if widget:
                    self.leaf.OnStart(self.leaf.widget_tree())
                else:
                    self.leaf.on_start(self.ctx)
            except LeafFailure as failure:
                return self._finish_instant(before, FAILED, str(failure))
            except Exception as error:  # noqa: BLE001 - leaf code
                return self._finish_instant(before, FAILED, self._report(error, "OnStart" if widget else "on_start"))
            if self.run.aborting:
                return self.finish(before, RUNNING)
        elif widget and not self.run.periodic:
            return RUNNING  # widget code is called again once per tick interval
        try:
            result = self.leaf.call_on_run() if widget else self.leaf.tick(self.ctx)
        except LeafFailure as failure:
            return self._finish_instant(before, FAILED, str(failure))
        except Exception as error:  # noqa: BLE001 - leaf code
            return self._finish_instant(before, FAILED, self._report(error, "OnRun" if widget else "tick"))
        status, message = result_status(result)
        if message is not None:
            return self._finish_instant(before, FAILED, self._report_text(f"{'OnRun' if widget else 'tick'} {message}"))
        if status == RUNNING:
            return self.finish(before, RUNNING)
        return self._finish_instant(before, status)

    def _finish_instant(self, before: Status, status: Status, reason: str | None = None) -> Status:
        result = self.finish(before, status, reason)
        if self.kind == "inline":
            self._terminate(NodeStatus.SUCCEEDED if status == SUCCEEDED else NodeStatus.FAILED)
        return result

    def _tick_started(self) -> Status:
        before = self.status
        if self.pending is not None and self.state not in (WAITING, CANCELLING_STATE):
            return self._consume()
        if self.state == ACTIVE:
            if self.rerun and self.run.periodic:
                self.rerun = False
                self._submit()
            return RUNNING
        if self.state in (WAITING, CANCELLING_STATE):
            return RUNNING
        if self.state == UNSETTLED:
            self.set_status(CANCELLING)  # its own earlier work is outstanding: it waits for it
            return RUNNING
        self.run.clear_details(self.index)
        self.state = WAITING
        self.pending = None
        self.reason = None
        self.fail_after_cancel = None
        self.rerun = False
        self.was_running = True
        self.run.waiting[self] = None
        return self.finish(before, RUNNING)

    def _consume(self) -> Status:
        status = self.pending
        assert status is not None
        self.pending = None
        if self.state != UNSETTLED:
            self.state = DONE
        reason, self.reason = self.reason, None
        before = RUNNING if self.was_running else self.status
        self.was_running = False
        self.set_status(status)
        self.run.note(self, before, status, reason)
        return status

    # ----------------------------------------------------------------- starting (after the tick)
    def blocked_by_own_call(self) -> bool:
        """True while a call of this node (or of its node in an earlier run) has not returned.
        A call left from an earlier run requests a tick of this run when it returns."""
        if self.outstanding is not None and not self.outstanding.done():
            return True
        earlier = self.run.registry.outstanding_calls.get(self.path)
        if earlier is None or earlier is self.future or earlier.done():
            return False
        if self.watched is not earlier:
            self.watched = earlier
            tick = self.run.entry_point(self.run.request_tick)
            earlier.add_done_callback(lambda _call: self.run.scheduler.call_soon(tick))
        return True

    def start(self) -> None:
        """The leaf may start: take the bound values and start its work."""
        self.run.waiting.pop(self, None)
        if "waiting" in self.run.details_of(self.index):
            self.run.detail(self.index, "waiting", None)
        self.state = ACTIVE
        reason = self._resolve()
        if reason is not None:
            self._fail_now(reason)
            return
        try:
            timeout = float(self.leaf.timeout(self.ctx))
        except Exception:  # noqa: BLE001 - leaf code
            timeout = 0.0
        if timeout > 0:
            when = self.run.scheduler.ros_now() + timeout
            self.deadline = self.run.scheduler.call_at(when, self.run.entry_point(self._on_deadline), ros=True)
        if self.kind == "async":
            self.started = True
            try:
                self.leaf.on_start(self.ctx)
            except LeafFailure as failure:
                self.on_result(FAILED, str(failure))
            except Exception as error:  # noqa: BLE001 - leaf code
                self.on_result(FAILED, self._report(error, "on_start"))
            return
        self.token = RunToken(self.run.generation)
        if self.kind == "widget_thread":
            try:
                self.leaf.OnStart(self.leaf.widget_tree())
            except LeafFailure as failure:
                self.on_result(FAILED, str(failure))
                return
            except Exception as error:  # noqa: BLE001 - leaf code
                self.on_result(FAILED, self._report(error, "OnStart"))
                return
        self._submit()

    def _fail_now(self, reason: str) -> None:
        self.pending = FAILED
        self.reason = reason
        self.run.detail(self.index, "error", reason)
        self.run.request_tick()

    def _submit(self) -> None:
        token = self.token
        leaf = self.leaf
        widget = self.kind == "widget_thread"
        ctx = self.ctx

        def call() -> Any:
            with bound(token):
                return leaf.call_on_run() if widget else leaf.run(ctx)

        try:
            future = leaf_pool().submit(call, f"bt_server:{self.title}")
        except Exception as error:  # noqa: BLE001 - e.g. "can't start new thread"
            self.on_result(FAILED, self._report(error, "starting the worker thread"))
            return
        self.future = future
        self.run.registry.outstanding_calls[self.path] = future
        done = self.run.entry_point(lambda: self._on_call_done(token, future))
        future.add_done_callback(lambda _future: self.run.scheduler.call_soon(done))

    def _on_call_done(self, token: RunToken | None, future: Future) -> None:
        calls = self.run.registry.outstanding_calls
        if calls.get(self.path) is future:
            del calls[self.path]
        if self.outstanding is future:
            self.outstanding = None
            self.run.request_tick()  # the node may start again
        if future is not self.future:
            return  # an abandoned call: its result is ignored
        self.future = None
        where = "OnRun" if self.kind == "widget_thread" else "run"
        error = future.exception()
        if isinstance(error, LeafFailure):
            status, reason = FAILED, str(error)
        elif error is not None:
            status, reason = FAILED, (self._report(error, where) if self.state == ACTIVE else describe_error(error))
        else:
            status, message = result_status(future.result())
            reason = self._report_text(f"{where} {message}") if message is not None and self.state == ACTIVE else message
        if self.state == ACTIVE:
            self.on_result(status, reason)
        elif self.state in (CANCELLING_STATE, UNSETTLED):
            self.on_settled()

    # ----------------------------------------------------------------- results and cancels
    def on_result(self, status: Status, reason: str | None) -> None:
        """The work of an active leaf ended (engine thread)."""
        if self.state == ACTIVE and self.pending is None:
            if status == RUNNING:
                self.rerun = True
                return
            self._cancel_deadline()
            self.pending = status
            self.reason = reason if status == FAILED else None
            if status == FAILED and reason:
                self.run.detail(self.index, "error", reason)
            self._end(status)
            self.run.request_tick()
        elif self.state in (CANCELLING_STATE, UNSETTLED):
            self.on_settled()  # the result of a cancelled leaf is ignored

    def _on_deadline(self) -> None:
        self.deadline = None
        self.cancel_and_fail("timed out")

    def leaf_cancel_and_fail(self, reason: str) -> None:
        self.post(lambda: self.cancel_and_fail(reason))

    def cancel_and_fail(self, reason: str) -> None:
        """An active leaf gives up (a deadline, or the leaf itself): it shows Cancelling, its cancel
        is sent, and once that is confirmed (or times out) it ends Failed with ``reason``."""
        if self.state != ACTIVE or self.pending is not None:
            return
        self._cancel_deadline()
        self.run.detail(self.index, "error", reason)
        self.run.log(LogLevel.INFO, f"Node {self.label} {reason}: cancelling", self.index)
        self.fail_after_cancel = reason
        self.state = CANCELLING_STATE
        self.run.cancelling_leaves[self] = None
        self.set_status(CANCELLING)
        self.send_cancel()

    def send_cancel(self) -> None:
        """Send the cancel of a Cancelling leaf and start its cancel timeout."""
        self.cancel_generation += 1
        generation = self.cancel_generation
        when = self.run.scheduler.now() + self.cancel_timeout()
        self.cancel_timer = self.run.scheduler.call_at(
            when, self.run.entry_point(lambda: self._on_cancel_timeout(generation)))
        if self.kind == "async":
            try:
                self.leaf.on_cancel(self.ctx)
            except Exception as error:  # noqa: BLE001 - leaf code
                self._report(error, "on_cancel")
                self.on_settled()
        elif self.token is not None:
            self.token.cancel()
            if self.future is None:
                self.on_settled()
        else:
            self.on_settled()

    def on_settled(self) -> None:
        """The cancel is confirmed, or the work ended while cancelling (engine thread)."""
        if self.cancel_timer is not None:
            self.cancel_timer.cancel()
            self.cancel_timer = None
        if self.state == UNSETTLED:
            self.state = IDLE
            self.run.unsettled -= 1
            self.run.log(LogLevel.INFO, f"Node {self.label} cancel confirmed late", self.index)
            self._end(READY)
            if self.pending is None and self.status == CANCELLING:
                self.set_status(READY)
            if self.pending is not None:
                self.state = DONE
            self.run.request_tick()
            self.run.progress()
            return
        if self.state != CANCELLING_STATE:
            return
        self.run.cancelling_leaves.pop(self, None)
        self.run.log(LogLevel.INFO, f"Node {self.label} cancel confirmed", self.index)
        self._end(READY)
        if self.fail_after_cancel is not None:
            self.state = DONE
            self.pending = FAILED
            self.reason = self.fail_after_cancel
            self.set_status(FAILED)
        else:
            self.state = IDLE
            self.set_status(READY)
        self.fail_after_cancel = None
        self.run.request_tick()
        self.run.progress()

    def _on_cancel_timeout(self, generation: int) -> None:
        if generation != self.cancel_generation or self.state != CANCELLING_STATE:
            return
        self.cancel_timer = None
        self.run.cancelling_leaves.pop(self, None)
        self.state = UNSETTLED
        self.run.unsettled += 1
        self.run.unconfirmed_cancels += 1
        self.run.log(LogLevel.ERROR, f"Node {self.label}: the cancel was not confirmed within "
                                     f"{self.cancel_timeout():g} s", self.index)
        if self.fail_after_cancel is not None:
            self.pending = FAILED
            self.reason = self.fail_after_cancel
            self.set_status(FAILED)
        else:
            self.set_status(READY)
        self.fail_after_cancel = None
        self.run.unconfirmed_cancel()

    def _end(self, status: Status) -> None:
        """The work of one start is over: on_end (AsyncLeaf) or OnTerminate (widget leaf)."""
        if self.kind == "async" and self.started:
            self.started = False
            try:
                self.leaf.on_end(self.ctx)
            except Exception as error:  # noqa: BLE001 - leaf code
                self._report(error, "on_end")
        elif self.kind == "widget_thread" and status in (SUCCEEDED, FAILED):
            self._terminate(NodeStatus.SUCCEEDED if status == SUCCEEDED else NodeStatus.FAILED)

    def _terminate(self, status: NodeStatus) -> None:
        try:
            self.leaf.OnTerminate(self.leaf.widget_tree(), status)
        except Exception as error:  # noqa: BLE001 - leaf code
            self._report(error, "OnTerminate")

    def _cancel_deadline(self) -> None:
        if self.deadline is not None:
            self.deadline.cancel()
            self.deadline = None

    # ----------------------------------------------------------------- halting
    def halt(self) -> None:
        before = self.status
        if self.kind in ("instant", "inline"):
            if before == RUNNING:
                try:
                    if self.kind == "inline":
                        self.leaf.OnTerminate(self.leaf.widget_tree(), NodeStatus.READY)
                    else:
                        self.leaf.on_halt(self.ctx)
                except Exception as error:  # noqa: BLE001 - leaf code
                    self._report(error, "OnTerminate" if self.kind == "inline" else "on_halt")
                self.run.note_halted(self)
            self.set_status(READY)
            return
        state = self.state
        if state == WAITING:
            self.run.waiting.pop(self, None)
            if "waiting" in self.run.details_of(self.index):
                self.run.detail(self.index, "waiting", None)
            self.state = IDLE
            self.was_running = False
            self.run.note_halted(self)
            self.set_status(READY)
        elif state == ACTIVE:
            self._cancel_deadline()
            self.was_running = False
            self.run.note_halted(self)
            if self.pending is not None:  # finished already: nothing to cancel
                self.pending = None
                self.state = IDLE
                self.set_status(READY)
            elif self.kind == "widget_thread":  # abandoned at once, as in the widget
                if self.token is not None:
                    self.token.cancel()
                self.outstanding, self.future = self.future, None
                self.state = IDLE
                self.set_status(READY)
                self._terminate(NodeStatus.READY)
            else:
                self.state = CANCELLING_STATE
                self.run.cancelling_leaves[self] = None
                self.set_status(CANCELLING)
                if self.run.ticking:
                    self.run.to_cancel.append(self)  # sent after the tick
                else:
                    self.send_cancel()
        elif state == CANCELLING_STATE:
            self.fail_after_cancel = None  # halted: the failure it gave up with no longer matters
        elif state == UNSETTLED:
            self.pending = None
            if self.status != READY:
                self.set_status(READY)
        else:
            self.pending = None
            self.state = IDLE
            self.set_status(READY)

    def reset_subtree(self) -> None:
        if self.kind in ("instant", "inline"):
            self.set_status(READY)
            return
        if self.state == CANCELLING_STATE:
            self.fail_after_cancel = None
            return
        if self.state in (WAITING, ACTIVE):
            self.halt()
            return
        self.pending = None
        self.reason = None
        if self.state == DONE:
            self.state = IDLE
        self.set_status(READY)

    # ----------------------------------------------------------------- errors
    def _report(self, error: BaseException, where: str) -> str:
        """Log an exception of leaf code at ERROR (with its traceback); returns the reason."""
        reason = f"{where} raised {describe_error(error)}"
        try:
            details = "".join(traceback.format_exception(type(error), error, error.__traceback__)).rstrip()
        except Exception:  # noqa: BLE001
            details = ""
        self.run.log(LogLevel.ERROR, f"{self.label}: {reason}" + (f"\n{details}" if details else ""), self.index)
        return reason

    def _report_text(self, reason: str) -> str:
        self.run.log(LogLevel.ERROR, f"{self.label}: {reason}", self.index)
        return reason
