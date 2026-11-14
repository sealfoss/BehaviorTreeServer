"""The engine (design 5): :class:`TreeRunner` runs one bundle (design 4.3) on one thread.

* **Ticks** come from events and from the periodic tick (5.4). Requests are merged, event ticks
  are capped at ``max_tick_rate_hz``, and the periodic tick keeps its rhythm from the start of
  the run. A change made inside a tick (a Set node, leaf code on the engine thread) requests no
  tick; a blackboard change from outside a tick, a leaf result, a confirmed cancel, a deadline,
  a fed entry going stale, resume and reset do.
* **Started leaves** follow the lifecycle of 5.6: a tick only notes which leaves want to start
  and which must be cancelled; after it, the cancels are sent and then the waiting leaves that
  may start are started. A start waits for a Cancelling leaf when the nearest composite that
  contains both is a Sequence or a Selector (never across branches of a Parallel), unless the
  node or the tree switches *Wait for cancelled actions* off; local leaves never wait. A cancel
  that is not confirmed within the cancel timeout is logged at ERROR, counted, kept as unsettled,
  and stops the tree (or not, ``on_unconfirmed_cancel: "continue"``).
* **Loops, Pause, Stop, Reset** as in 5.5 and the command table of 4.4; commands issued by node
  code end the tick at once. :class:`CommandRefused` is raised for what the table refuses.
* **Errors** (5.7): an exception in leaf code fails that node; a failing tick ends the run with
  the outcome *error* and the Root shows Failed with the error.
* **Reporting** goes to a :class:`Sink`, on the engine thread: statuses at most once per tick
  (and changes outside ticks when they happen), details, blackboard changes, log records.

Statuses live in :attr:`TreeRunner.statuses`, one byte per node, indexed like
:attr:`TreeRunner.node_paths` (contract 2.2): depth-first pre-order from the Root, children in
connection order, a Sub-Tree node's own index before its sub-tree's Root.

Two clocks (5.4): node timeouts set by users follow ``scheduler.ros_now``; the tick interval and
the cancel timeouts follow ``scheduler.now``.

Construction raises :class:`BundleError` for a bundle that cannot be read. Operations applied
during a run (:meth:`TreeRunner.apply`) carry Python values (decoded from their file or wire
encoding by the caller), as :meth:`Blackboard.apply_op` takes them.
"""

from __future__ import annotations

import array
import copy
import threading
import traceback
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

from ._engine_nodes import (
    CompositeNode,
    DecoratorNode,
    FailingNode,
    LeafNode,
    Node,
    ParallelNode,
)
from ._engine_types import BundleError, CommandRefused, ExecutionCancelled, Outcome, RunToken, State, Status, describe_error
from .blackboard import Blackboard, BlackboardSnapshot, OpError
from .leaves import set_path, split_path
from .registry import Registry
from .scheduler import Scheduler
from .treelog import LogLevel
from .values import decode_value, loads_json

__all__ = [
    "DEFAULT_CONFIG",
    "BundleError",
    "CommandRefused",
    "ExecutionCancelled",
    "OpError",
    "Outcome",
    "RunResult",
    "Sink",
    "State",
    "Status",
    "TreeRunner",
]

#: The tree options the engine uses (design 12) and their defaults.
DEFAULT_CONFIG: dict[str, Any] = {
    "tick_interval_ms": 100,
    "repeat": False,
    "restore_blackboard": False,
    "default_memory": True,
    "log_level": 4,
    "wait_for_cancels": True,
    "cancel_timeout_ms": 5000,
    "on_unconfirmed_cancel": "stop",
}

#: Built-in leaves whose fields name entries: they follow renames during a run, as in the widget.
_ENTRY_FIELDS = {"Evaluation": ("ValueName", "CompareTo"), "Set": ("ValueName", "CompareTo"), "Print Message": ("Source",)}


@dataclass
class RunResult:
    """How a run ended (the ``ExecuteTree`` result)."""

    outcome: Outcome
    message: str
    loops_completed: int
    unconfirmed_cancels: int
    blackboard: list[dict] = field(default_factory=list)  # final entries, file form plus "id"
    stopped_by_command: bool = False  # Stop button / goal cancel, versus a Stop node or a watchdog


class Sink(Protocol):
    """Where the engine reports (design 5.8); called on the engine thread."""

    def state(self, state: State, loop: int) -> None: ...

    def statuses(self, changed: Iterable[int]) -> None: ...

    def detail(self, index: int, kind: str, value: object) -> None: ...

    def blackboard(self, changes: dict) -> None: ...

    def log(self, level: int, text: str, index: int = -1) -> None: ...


def _check_config(config: Any) -> dict[str, Any]:
    """The tree options with defaults for what is missing or malformed."""
    result = dict(DEFAULT_CONFIG)
    if not isinstance(config, dict):
        return result
    for key, value in config.items():
        result[key] = value
    if not (isinstance(result["tick_interval_ms"], (int, float)) and not isinstance(result["tick_interval_ms"], bool)
            and result["tick_interval_ms"] > 0):
        result["tick_interval_ms"] = DEFAULT_CONFIG["tick_interval_ms"]
    for key in ("repeat", "restore_blackboard", "default_memory", "wait_for_cancels"):
        if not isinstance(result[key], bool):
            result[key] = DEFAULT_CONFIG[key]
    level = result["log_level"]
    if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= 4:
        result["log_level"] = DEFAULT_CONFIG["log_level"]
    timeout = result["cancel_timeout_ms"]
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout < 0:
        result["cancel_timeout_ms"] = DEFAULT_CONFIG["cancel_timeout_ms"]
    if result["on_unconfirmed_cancel"] not in ("stop", "continue"):
        result["on_unconfirmed_cancel"] = DEFAULT_CONFIG["on_unconfirmed_cancel"]
    return result


def _decoded(value: Any) -> Any:
    try:
        return decode_value(value)
    except ValueError:
        return value


class _Tree:
    """One tree document of the bundle: its nodes by id and the children of each parent."""

    def __init__(self, key: str, data: Any) -> None:
        self.key = key
        if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
            raise BundleError(f"the tree {key!r} of the bundle has no list of nodes")
        self.config = data.get("config") if isinstance(data.get("config"), dict) else {}
        self.nodes: dict[str, dict] = {}
        self.root: dict | None = None
        for item in data["nodes"]:
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"] not in self.nodes:
                self.nodes[item["id"]] = item
                if item.get("type") == "Root" and self.root is None:
                    self.root = item
        self.children: dict[str, list[str]] = {}
        connections = data.get("connections")
        for item in connections if isinstance(connections, list) else []:
            if not isinstance(item, dict):
                continue
            parent, child = item.get("parent"), item.get("child")
            if parent in self.nodes and child in self.nodes and child != parent:
                siblings = self.children.setdefault(parent, [])
                if child not in siblings:
                    siblings.append(child)


class TreeRunner:
    """Runs one bundle (see the module docs and contract section 3).

    ``bundle``: the bundle object (or its JSON text). ``blackboard``: the run's blackboard, made
    from the goal; the Object entries of the registry's setup hooks are added to it.
    ``on_finished(result)`` is called once, on the engine thread, when the run ends.
    ``run_context`` is handed to leaves as ``ctx.run``.
    """

    def __init__(
        self,
        bundle: dict | str,
        blackboard: Blackboard,
        registry: Registry,
        scheduler: Scheduler,
        sink: Sink,
        on_finished: Callable[[RunResult], None],
        *,
        run_context: object | None = None,
        max_tick_rate_hz: float = 200.0,
    ) -> None:
        self.blackboard = blackboard
        self.registry = registry
        self.scheduler = scheduler
        self.run_context = run_context
        self._sink = sink
        self._on_finished = on_finished
        self._min_event_gap = 1.0 / max_tick_rate_hz if max_tick_rate_hz > 0 else 0.0
        self._engine_thread = threading.get_ident()
        trees, root_key = self._read_bundle(bundle)
        self._trees = trees
        self.config = _check_config(trees[root_key].config)
        self.node_paths: list[str] = []
        self._nodes: list[Node] = []
        self._root = self._build(trees[root_key], (), (), (root_key,), ())
        self.statuses = array.array("B", bytes(len(self._nodes)))
        # run state
        self.state = State.IDLE
        self.loop_count = 0
        self.unsettled = 0
        self.unconfirmed_cancels = 0
        self.generation = 0
        self.waiting: dict[LeafNode, None] = {}
        self.cancelling_leaves: dict[LeafNode, None] = {}
        self.to_cancel: list[LeafNode] = []
        self.ticking = False
        self.aborting = False
        self.periodic = False
        self._deferred: list[str] = []
        self._details: dict[int, dict[str, object]] = {}
        self._changed: set[int] = set()
        self._depth = 0
        self._started = False
        self._finished = False
        self._loop_active = False
        self._root_result: Status | None = None
        self._new_loop: str | None = None  # "periodic": at the next periodic tick; "next": at the next tick
        self._restart_pending = False  # a Reset waits for its cancels
        self._pause_after_restart = False
        self._stop_outcome = Outcome.STOPPED
        self._stop_message = "Stopped"
        self._stopped_by_command = False
        self._error: str | None = None
        self._halting = 0
        self._snapshot: BlackboardSnapshot | None = None
        self._tick_requested = False
        self._last_event_tick = float("-inf")
        self._periodic_handle: Any = None
        self._periodic_base = 0.0
        self._periodic_count = 0
        self._stale_handle: Any = None
        self._stale_at: float | None = None
        blackboard.set_clock(scheduler.now)
        for name, value in registry.objects.items():
            blackboard.put_object(name, value)
        blackboard.add_listener(self._on_blackboard_change)

    # ----------------------------------------------------------------- the bundle
    @staticmethod
    def _read_bundle(bundle: Any) -> tuple[dict[str, Any], str]:
        if isinstance(bundle, (str, bytes, bytearray)):
            try:
                bundle = loads_json(bundle)
            except ValueError as error:
                raise BundleError(f"the bundle is not JSON: {error}") from error
        if not isinstance(bundle, dict):
            raise BundleError("the bundle is not a JSON object")
        if bundle.get("format", "behavior_tree_bundle") != "behavior_tree_bundle":
            raise BundleError(f"not a bundle: format {bundle.get('format')!r}")
        trees = bundle.get("trees")
        root_key = bundle.get("root")
        if not isinstance(trees, dict) or not isinstance(root_key, str) or root_key not in trees:
            raise BundleError("the bundle names no root tree among its trees")
        root = _Tree(root_key, trees[root_key])
        if root.root is None:
            raise BundleError(f"the tree {root_key!r} has no Root node")
        result: dict[str, Any] = dict(trees)
        result[root_key] = root
        return result, root_key

    def _tree(self, key: str) -> _Tree | None:
        tree = self._trees.get(key)
        if isinstance(tree, _Tree):
            return tree
        try:
            parsed = _Tree(key, tree)
        except BundleError:
            return None
        self._trees[key] = parsed
        return parsed

    def _build(self, tree: _Tree, scope: tuple[str, ...], titles: tuple[str, ...], chain: tuple[str, ...],
               ancestors: tuple[Node, ...], data: dict | None = None, visiting: frozenset[str] = frozenset()) -> Node:
        data = data if data is not None else tree.root
        assert data is not None
        node_id = data["id"]
        type_name = data.get("type") if isinstance(data.get("type"), str) else ""
        title = data.get("title") if isinstance(data.get("title"), str) and data.get("title") else type_name
        index = len(self._nodes)
        path = "/".join((*scope, node_id))
        label = "".join(f"'{outer}' > " for outer in titles) + f"'{title}' ({type_name})"
        args = (self, index, path, data, tree.key, label)
        child_ids = tree.children.get(node_id, [])
        node: Node
        if type_name in ("Root", "Sequence", "Selector"):
            composite = data.get("composite") if type_name == "Root" else type_name
            memory = data.get("memory")
            node = CompositeNode(*args, selector=composite == "Selector",
                                 memory=memory if isinstance(memory, bool) else bool(self.config["default_memory"]))
        elif type_name == "Parallel":
            policy = data.get("policy")
            node = ParallelNode(*args, policy=policy if policy in ("all", "any") else "all")
        elif type_name == "Negation":
            node = DecoratorNode(*args, invert=True)
            child_ids = child_ids[:1]
        elif type_name == "Sub-Tree":
            sub_tree, problem = self._sub_tree(data, chain)
            node = DecoratorNode(*args, invert=False, problem=problem)
            child_ids = []
        else:
            node = self._leaf_node(args, data, type_name)
            child_ids = []
        self._nodes.append(node)
        self.node_paths.append(path)
        node.ancestors = ancestors
        inner = (*ancestors, node)
        for child_id in child_ids:
            if child_id in visiting or child_id == node_id:
                continue  # a cycle in a malformed file
            node.children.append(self._build(tree, scope, titles, chain, inner, tree.nodes[child_id],
                                             visiting | {node_id}))
        if type_name == "Sub-Tree" and node.problem is None:  # type: ignore[attr-defined]
            assert sub_tree is not None
            node.children.append(self._build(sub_tree, (*scope, node_id), (*titles, title),
                                             (*chain, sub_tree.key), inner))
        return node

    def _sub_tree(self, data: dict, chain: tuple[str, ...]) -> tuple[_Tree | None, str | None]:
        fields = data.get("fields") if isinstance(data.get("fields"), dict) else {}
        file_name = fields.get("File") if isinstance(fields.get("File"), str) else ""
        key = data.get("tree")
        if not file_name.strip() and not isinstance(key, str):
            return None, "the sub-tree cannot be run: no sub-tree file is chosen (File)"
        if isinstance(key, str) and key in chain:
            return None, (f"the sub-tree cannot be run: '{file_name or key}' contains this Sub-Tree node itself "
                          "(directly or through other sub-trees), which would never end")
        tree = self._tree(key) if isinstance(key, str) else None
        if tree is None or tree.root is None:
            return None, f"the sub-tree cannot be run: '{file_name or key}' is missing or not a tree file"
        return tree, None

    def _leaf_node(self, args: tuple, data: dict, type_name: str) -> Node:
        cls = self.registry.leaf_class(type_name)
        if cls is None:
            return FailingNode(*args, reason=self.registry.missing_reason(type_name))
        saved = data.get("fields") if isinstance(data.get("fields"), dict) else {}
        fields = cls.field_defaults()
        fields.update({key: _decoded(value) for key, value in saved.items()})
        selections = data.get("field_selections") if isinstance(data.get("field_selections"), dict) else {}
        bindings = {k: v for k, v in (data.get("bindings") or {}).items() if isinstance(k, str) and isinstance(v, str)} \
            if isinstance(data.get("bindings"), dict) else {}
        outputs = {k: v for k, v in (data.get("outputs") or {}).items() if isinstance(k, str) and isinstance(v, str)} \
            if isinstance(data.get("outputs"), dict) else {}
        try:
            return LeafNode(*args, leaf_class=cls, fields=fields, selections=dict(selections), bindings=bindings,
                            outputs=outputs)
        except Exception as error:  # noqa: BLE001 - a leaf class whose constructor fails
            return FailingNode(*args, reason=f"{type_name} could not be created: {describe_error(error)}")

    # ----------------------------------------------------------------- reading the run
    @property
    def cancelling(self) -> int:
        """Leaves waiting for a cancel to be confirmed."""
        return len(self.cancelling_leaves)

    def details(self) -> dict[int, dict[str, object]]:
        """``{index: {kind: value}}`` of the details set now (contract 2.2)."""
        return {index: dict(kinds) for index, kinds in self._details.items() if kinds}

    def details_of(self, index: int) -> dict[str, object]:
        return self._details.get(index, {})

    def node(self, index: int) -> Node:
        """The node of ``index`` (for tests and tools)."""
        return self._nodes[index]

    def index_of(self, path: str) -> int:
        """The index of the node with instance path ``path``. Raises ValueError."""
        return self.node_paths.index(path)

    # ----------------------------------------------------------------- entry points and reporting
    @contextmanager
    def _entry(self) -> Iterator[None]:
        """Everything that enters the engine runs inside: at the outermost exit the changes are
        reported."""
        self._depth += 1
        try:
            yield
        finally:
            self._depth -= 1
            if self._depth == 0:
                self._flush()

    def entry_point(self, fn: Callable[[], None]) -> Callable[[], None]:
        """``fn`` wrapped as an entry point (for scheduler callbacks)."""
        def call() -> None:
            with self._entry():
                fn()
        return call

    def post(self, fn: Callable[[], None]) -> None:
        """Run ``fn`` on the engine thread: now if this is it, else through ``call_soon``."""
        if threading.get_ident() == self._engine_thread:
            with self._entry():
                fn()
        else:
            self.scheduler.call_soon(self.entry_point(fn))

    def _flush(self) -> None:
        self._follow_renames()
        if self._changed:
            changed, self._changed = sorted(self._changed), set()
            self._sink.statuses(changed)
        changes = self.blackboard.drain_changes()
        if changes is not None:
            self._sink.blackboard(changes)
        self._arm_stale_timer()

    def set_status(self, index: int, status: Status) -> None:
        if self.statuses[index] != status:
            self.statuses[index] = status
            self._changed.add(index)

    def detail(self, index: int, kind: str, value: object) -> None:
        kinds = self._details.setdefault(index, {})
        if value is None:
            if kind not in kinds:
                return
            del kinds[kind]
        else:
            kinds[kind] = value
        self._sink.detail(index, kind, value)

    def clear_details(self, index: int) -> None:
        for kind in list(self._details.get(index, ())):
            self.detail(index, kind, None)

    def log(self, level: int, text: str, index: int = -1) -> None:
        level = int(level)
        if level <= 0 or level > self.config["log_level"]:
            return
        self._sink.log(level, text, index)

    def note(self, node: Node, before: Status, after: Status, reason: str | None = None) -> None:
        """Log the execution of ``node`` in a tick, as the widget does (INFO)."""
        if self.config["log_level"] < LogLevel.INFO:
            return
        if after == Status.RUNNING:
            if before == Status.RUNNING:
                return
            what = "started: Running"
        elif after in (Status.SUCCEEDED, Status.FAILED):
            what = f"{'finished' if before == Status.RUNNING else 'executed'}: {after.text}"
            if after == Status.FAILED and reason:
                what += f" ({reason})"
        else:
            return
        self.log(LogLevel.INFO, f"Node {node.label} {what}", node.index)

    def note_halted(self, node: Node) -> None:
        self.log(LogLevel.INFO, f"Node {node.label} interrupted", node.index)

    def _set_state(self, state: State) -> None:
        if state != self.state:
            self.state = state
            self._sink.state(state, self.loop_count)

    # ----------------------------------------------------------------- ticks
    def request_tick(self) -> None:
        """Ask for a tick: merged with other requests, capped at ``max_tick_rate_hz``."""
        if self._tick_requested or self._finished:
            return
        self._tick_requested = True
        when = self._last_event_tick + self._min_event_gap
        if when <= self.scheduler.now():
            self.scheduler.call_soon(self.entry_point(self._on_event_tick))
        else:
            self.scheduler.call_at(when, self.entry_point(self._on_event_tick))

    def _on_event_tick(self) -> None:
        if not self._tick_requested:
            return  # a periodic tick served the request
        self._last_event_tick = self.scheduler.now()
        self._tick(periodic=False)

    def _schedule_periodic(self) -> None:
        """Arm the next periodic tick on the rhythm from the start of the run; periods already
        past (a slow tick, a loaded host) are skipped, not caught up in a burst."""
        interval = self.config["tick_interval_ms"] / 1000.0
        now = self.scheduler.now()
        self._periodic_count += 1
        while self._periodic_base + self._periodic_count * interval <= now:
            self._periodic_count += 1
        when = self._periodic_base + self._periodic_count * interval
        self._periodic_handle = self.scheduler.call_at(when, self.entry_point(self._on_periodic))

    def _on_periodic(self) -> None:
        self._periodic_handle = None
        if self._finished:
            return
        try:
            self._periodic_tick()
        finally:
            if not self._finished and self._periodic_handle is None:
                self._schedule_periodic()

    def _periodic_tick(self) -> None:
        if self.state == State.RESTARTING and self._new_loop is not None and not self.cancelling_leaves:
            self._begin_loop()
            if self._pause_after_restart:
                self._pause_after_restart = False
                self._set_state(State.PAUSED)
                self.log(LogLevel.INFO, "Execution paused")
                return
            self._set_state(State.RUNNING)
        self._tick(periodic=True)

    def _tick(self, periodic: bool) -> None:
        self._tick_requested = False
        if self.state != State.RUNNING or self._finished:
            return
        if self._new_loop == "next" or (self._new_loop == "periodic" and periodic):
            if self._new_loop == "periodic" and self.config["repeat"]:
                self.log(LogLevel.INFO, "Loop Execution: the tree starts again from the Root")
            self._begin_loop()
        if not self._loop_active or self._root_result is not None:
            return
        self._follow_renames()
        self.ticking = True
        self.periodic = periodic
        self.aborting = False
        self._deferred = []
        try:
            result = self._root.tick()
        except Exception as error:  # noqa: BLE001 - the tick itself failed (design 5.7)
            self.ticking = False
            self.aborting = False
            self._deferred = []
            self._commit(start=False)  # the cancels of the leaves halted before the failure
            self._tick_failed(error)
            return
        finally:
            self.ticking = False
        deferred, self._deferred = self._deferred, []
        self.aborting = False
        halting = "stop" in deferred or "reset" in deferred
        self._commit(start=not halting)
        if result != Status.RUNNING and not halting:
            self._root_result = result
        for name in deferred:
            self._node_command_now(name)
        self.progress()

    def _commit(self, start: bool = True) -> None:
        """After a tick: send the cancels of the leaves halted in it, then start the waiting
        leaves that may start (design 5.6)."""
        to_cancel, self.to_cancel = self.to_cancel, []
        for leaf in to_cancel:
            if leaf.state == "cancelling" and leaf.cancel_timer is None:
                leaf.send_cancel()
        if not start:
            return
        for leaf in sorted(self.waiting, key=lambda item: item.index):
            if leaf.state != "waiting":
                continue
            if leaf.blocked_by_own_call():
                self.detail(leaf.index, "waiting", "waiting for the previous call to return")
                continue
            blocker = self._blocker(leaf)
            if blocker is not None:
                self.detail(leaf.index, "waiting", f"waiting for {blocker.title!r} to cancel")
                continue
            leaf.start()

    def _blocker(self, leaf: LeafNode) -> LeafNode | None:
        """A Cancelling leaf whose work ``leaf`` follows, or None (design 5.6)."""
        if not leaf.waits_for_cancels():
            return None
        mine = (*leaf.ancestors, leaf)
        for other in self.cancelling_leaves:
            if other is leaf:
                continue
            theirs = (*other.ancestors, other)
            common = None
            for a, b in zip(mine, theirs, strict=False):
                if a is not b:
                    break
                common = a
            if not isinstance(common, ParallelNode):
                return other
        return None

    def _tick_failed(self, error: BaseException) -> None:
        text = describe_error(error)
        details = "".join(traceback.format_exception(type(error), error, error.__traceback__)).rstrip()
        self.log(LogLevel.ERROR, f"The tick failed: {text}\n{details}")
        self._error = f"Tick failed: {text}"
        self._stop(by_command=False, outcome=Outcome.ERROR, message=f"The tick failed: {text}")

    # ----------------------------------------------------------------- loops
    def _begin_loop(self) -> None:
        self._new_loop = None
        self._root_result = None
        self._loop_active = True
        for node in self._nodes:
            if isinstance(node, LeafNode) and node.state not in ("cancelling", "unsettled"):
                node.state = "idle"
                node.pending = None
            node.on_reset()
            if self.statuses[node.index] != Status.READY:
                self.set_status(node.index, Status.READY)
        self.blackboard.start_loop()

    def _close_loop(self) -> None:
        """The loop in progress ends: entries with Resets get their loop-start values back."""
        if self._loop_active:
            self._loop_active = False
            self.blackboard.end_loop()

    def progress(self) -> None:
        """Move the run on once the cancels it waits for are done. Not while the tree is being
        halted: a leaf that settles at once must not end a Stop or Reset before every other leaf
        was halted. A loop ends only while RUNNING: during a pause only the tree's progress waits."""
        if self._finished or self.cancelling_leaves or self._halting:
            return
        if self.state == State.STOPPING:
            self._finish_stop()
        elif self._restart_pending:
            self._finish_restart()
        elif self.state == State.RUNNING and self._loop_active and self._root_result is not None:
            self._end_loop()
        elif (self.state == State.RUNNING and not self._loop_active and self._new_loop == "periodic"
              and not self.config["repeat"] and self._root_result is not None):
            result = self._root_result  # Loop Execution was switched off between two loops
            self.log(LogLevel.INFO, f"Execution finished: {result.text}")
            self._finish(Outcome.SUCCEEDED if result == Status.SUCCEEDED else Outcome.FAILED, result.text)

    def _halt_all(self) -> None:
        """Halt the whole tree (outside a tick), then move the run on once."""
        self._halting += 1
        try:
            self._root.halt()
        finally:
            self._halting -= 1
        self.progress()

    def _end_loop(self) -> None:
        result = self._root_result
        assert result is not None
        self._close_loop()
        self.loop_count += 1
        self._sink.state(self.state, self.loop_count)
        if self.config["repeat"]:
            self._new_loop = "periodic"
            return
        outcome = Outcome.SUCCEEDED if result == Status.SUCCEEDED else Outcome.FAILED
        self.log(LogLevel.INFO, f"Execution finished: {result.text}")
        self._finish(outcome, result.text)

    def _finish_restart(self) -> None:
        self._restart_pending = False
        self._close_loop()
        self._restore()
        self._all_ready()
        self._new_loop = "periodic" if self.state == State.RESTARTING else "next"

    def _finish_stop(self) -> None:
        self._close_loop()
        self._restore()
        self._all_ready()
        if self._error is not None:
            self.set_status(self._root.index, Status.FAILED)
            self.detail(self._root.index, "error", self._error)
        self.log(LogLevel.INFO, "Execution stopped" if self._stop_outcome == Outcome.STOPPED else "Execution ended")
        self._finish(self._stop_outcome, self._stop_message)

    def _restore(self) -> None:
        if self._snapshot is not None and self.config["restore_blackboard"]:
            self.blackboard.restore(self._snapshot)

    def _all_ready(self) -> None:
        for node in self._nodes:
            node.on_reset()
            if isinstance(node, LeafNode) and node.state == "done":
                node.state = "idle"
                node.pending = None
            if self.statuses[node.index] != Status.READY:
                self.set_status(node.index, Status.READY)

    def _finish(self, outcome: Outcome, message: str) -> None:
        self._finished = True
        self._new_loop = None
        for handle in (self._periodic_handle, self._stale_handle):
            if handle is not None:
                handle.cancel()
        self._periodic_handle = self._stale_handle = None
        self.blackboard.remove_listener(self._on_blackboard_change)
        self._set_state(State.IDLE)
        result = RunResult(outcome, message, self.loop_count, self.unconfirmed_cancels, self.blackboard.to_list(),
                           self._stopped_by_command)
        self._flush()
        self._on_finished(result)

    # ----------------------------------------------------------------- commands
    def start(self) -> None:
        """Execute: the first tick happens now. Raises :class:`CommandRefused` when the runner
        was started already."""
        if self._started:
            raise CommandRefused("the run was started already")
        self._started = True
        with self._entry():
            self.log(LogLevel.INFO, "Execution started")
            self._snapshot = self.blackboard.snapshot()  # the option may be switched on later
            self._set_state(State.RUNNING)
            self._periodic_base = self.scheduler.now()
            self._periodic_count = 0
            self._schedule_periodic()
            self._begin_loop()
            self._tick(periodic=True)

    def pause(self) -> None:
        """Pause (design 4.4). Raises :class:`CommandRefused` while idle or stopping."""
        with self._entry():
            if self.state in (State.IDLE, State.STOPPING):
                raise CommandRefused(f"Pause is refused while {self.state.name}")
            if self.ticking:
                self._defer("pause")
            elif self.state == State.RUNNING:
                self._set_state(State.PAUSED)
                self.log(LogLevel.INFO, "Execution paused")
            elif self.state == State.RESTARTING:
                self._pause_after_restart = True

    def resume(self) -> None:
        """Resume (design 4.4). Raises :class:`CommandRefused` while idle or stopping."""
        with self._entry():
            if self.state in (State.IDLE, State.STOPPING):
                raise CommandRefused(f"Resume is refused while {self.state.name}")
            if self.state == State.RESTARTING:
                self._pause_after_restart = False
            if self.state != State.PAUSED:
                return
            self.log(LogLevel.INFO, "Execution resumed")
            if self._restart_pending:
                self._set_state(State.RESTARTING)  # the Reset made while paused still waits for cancels
                return
            self._set_state(State.RUNNING)
            self.progress()  # a loop whose last cancel was confirmed during the pause ends now
            self.request_tick()

    def stop(self) -> None:
        """Stop (the Stop button or a cancelled goal): no effect while idle or stopping."""
        with self._entry():
            self._stop(by_command=True)

    def reset(self) -> None:
        """Reset (design 4.4): while idle every node goes to Ready; while running the run passes
        through RESTARTING; while paused the leaves are halted and the resume starts a new loop."""
        with self._entry():
            if self.ticking:
                self._defer("reset")
                return
            if self.state == State.IDLE:
                self._all_ready()
                return
            if self.state in (State.STOPPING, State.RESTARTING) or self._restart_pending:
                return
            self.log(LogLevel.INFO, "Tree reset: execution starts again from the Root")
            self.generation += 1
            if self.state == State.RUNNING:
                self._set_state(State.RESTARTING)
            self._restart_pending = True
            self._new_loop = None
            self._halt_all()

    def _stop(self, by_command: bool, outcome: Outcome = Outcome.STOPPED, message: str = "Stopped") -> None:
        if self.ticking:
            self._defer("stop")
            return
        if self.state in (State.IDLE, State.STOPPING) or self._finished:
            return
        self._stopped_by_command = by_command
        self._stop_outcome = outcome
        self._stop_message = message
        self.generation += 1
        self._restart_pending = False
        self._new_loop = None
        self._set_state(State.STOPPING)
        self._halt_all()

    def _defer(self, name: str) -> None:
        """A command issued during the tick: the tick ends at once and it is carried out after."""
        self._deferred.append(name)
        self.aborting = True

    def node_command(self, name: str, token: RunToken | None) -> None:
        """A command issued by node code (any thread). From a worker thread it is dropped if the
        run was stopped or reset since the call started."""
        if threading.get_ident() == self._engine_thread:
            with self._entry():
                if self.ticking:
                    if name != "resume":
                        self._defer(name)
                    return
                self._node_command_now(name)
            return
        generation = token.generation if token is not None else None

        def carry_out() -> None:
            if generation is None or generation == self.generation:
                self._node_command_now(name)

        self.scheduler.call_soon(self.entry_point(carry_out))

    def _node_command_now(self, name: str) -> None:
        try:
            if name == "stop":
                self._stop(by_command=False)
            elif name == "pause":
                self.pause()
            elif name == "resume":
                self.resume()
            elif name == "reset":
                self.reset()
        except CommandRefused:
            pass

    def unconfirmed_cancel(self) -> None:
        """A cancel timed out: stop the tree, or go on (``on_unconfirmed_cancel``)."""
        if self.config["on_unconfirmed_cancel"] == "stop" and self.state not in (State.IDLE, State.STOPPING):
            self._stop(by_command=False, message="Stopped: a cancel was not confirmed")
        else:
            self.request_tick()
            self.progress()

    # ----------------------------------------------------------------- changes during a run
    def apply(self, ops: list[dict]) -> None:
        """Apply operations during a run (contract 3): ``bb.*`` (as :meth:`Blackboard.apply_op`),
        ``node.set_field`` (``tree``, ``id``, ``field``, ``value``, optional ``path``),
        ``node.set_selection`` (``tree``, ``id``, ``field``, ``index``) and
        ``doc.set_config`` (``config``: the root tree's options). Values are Python values.
        Raises :class:`OpError` at the first refused operation (earlier ones stay applied)."""
        with self._entry():
            for op in ops:
                if not isinstance(op, dict):
                    raise OpError("an operation is an object with 'op'")
                kind = op.get("op")
                if isinstance(kind, str) and kind.startswith("bb."):
                    self.blackboard.apply_op(op)
                elif kind == "node.set_field":
                    self._apply_set_field(op)
                elif kind == "node.set_selection":
                    self._apply_set_selection(op)
                elif kind == "doc.set_config":
                    self._apply_config(op.get("config"))
                else:
                    raise OpError(f"the operation {kind!r} cannot be applied during a run")

    def _targets(self, op: dict) -> list[LeafNode]:
        """Every leaf of the run made from the node ``op`` names (``tree``, ``id``)."""
        tree, node_id = op.get("tree"), op.get("id")
        targets = [node for node in self._nodes if isinstance(node, LeafNode) and node.node_id == node_id
                   and (tree is None or node.tree_key == tree)]
        if not targets:
            raise OpError(f"no running leaf has the id {node_id!r} in the tree {tree!r}")
        return targets

    def _apply_set_selection(self, op: dict) -> None:
        """Choose another option of a choice field; a leaf that starts afterwards uses it."""
        name, index = op.get("field"), op.get("index")
        targets = self._targets(op)
        for node in targets:
            options = node.fields.get(name) if isinstance(name, str) else None
            if not isinstance(options, list):
                raise OpError(f"{name!r} is not a choice field of {node.title!r}")
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(options):
                raise OpError(f"the index {index!r} is not one of the {len(options)} options of {name!r}")
        for node in targets:
            node.set_selection(name, index)

    def _apply_set_field(self, op: dict) -> None:
        tree, node_id, name = op.get("tree"), op.get("id"), op.get("field")
        if not isinstance(name, str):
            raise OpError("node.set_field needs a field name")
        targets = [node for node in self._nodes if isinstance(node, LeafNode) and node.node_id == node_id
                   and (tree is None or node.tree_key == tree)]
        if not targets:
            raise OpError(f"no running leaf has the id {node_id!r} in the tree {tree!r}")
        value = op.get("value")
        path = op.get("path")
        for node in targets:
            new = value
            if isinstance(path, str) and path:
                try:
                    new = set_path(node.fields.get(name), split_path(path), value)
                except (KeyError, IndexError, TypeError, ValueError) as error:
                    raise OpError(f"{name}.{path}: {error}") from error
            with node.lock:
                node.fields[name] = copy.deepcopy(new)
                if not node.binds_fields() or not any(key.split(".")[0].split("[")[0] == name
                                                      for key in node.bindings):
                    node.values = {**node.values, name: copy.deepcopy(new)}

    def _apply_config(self, config: Any) -> None:
        if not isinstance(config, dict):
            raise OpError("doc.set_config needs a config object")
        old_interval = self.config["tick_interval_ms"]
        self.config = _check_config({**self.config, **config})
        if self.config["tick_interval_ms"] != old_interval and self._periodic_handle is not None:
            self._periodic_handle.cancel()
            self._periodic_base = self.scheduler.now()
            self._periodic_count = 0
            self._schedule_periodic()
        self.progress()

    # ----------------------------------------------------------------- the blackboard
    def _on_blackboard_change(self) -> None:
        if threading.get_ident() == self._engine_thread:
            if not self.ticking:
                self.request_tick()
        else:
            self.scheduler.call_soon(self.entry_point(self.request_tick))

    def _follow_renames(self) -> None:
        """Nodes that name an entry follow its renames, as in the widget."""
        for old, new in self.blackboard.drain_renames():
            for node in self._nodes:
                if not isinstance(node, LeafNode):
                    continue
                for name in _ENTRY_FIELDS.get(node.type_name, ()):
                    if node.fields.get(name) == old:
                        node.fields[name] = new
                        node.values[name] = new
                for mapping in (node.bindings, node.outputs):
                    for key, entry in mapping.items():
                        if entry == old:
                            mapping[key] = new

    def _arm_stale_timer(self) -> None:
        if self._finished:
            return
        when = self.blackboard.next_stale_time()
        if when == self._stale_at:
            return
        if self._stale_handle is not None:
            self._stale_handle.cancel()
            self._stale_handle = None
        self._stale_at = when
        if when is not None:
            def stale() -> None:
                self._stale_handle = None
                self._stale_at = None
                self.request_tick()
            self._stale_handle = self.scheduler.call_at(when, self.entry_point(stale))
