"""The executor node (design 8): runs one behaviour tree at a time with the engine of
``bt_server_core`` and offers it as the tree API of design 4.1.

One rclpy node, ``bt_executor`` (its namespace from ``--ros-args -r __ns:=/cell1``), spun by one
``EventsExecutor`` (``SingleThreadedExecutor`` if that cannot be made) on the main thread, which
is also the engine thread: the engine, every ROS callback and every service of the tree API run
there, so nothing needs a lock (8.2).

The tree API (4.1), under the node's name (``~``):

* ``~/execute`` (``ExecuteTree``): the goal callback rejects a goal while a run is active or when
  its bundle or blackboard cannot be read, and marks the executor busy at once when it accepts
  (two goals arriving together cannot both be accepted). Cancelling the goal is Stop. Feedback
  is sent when the state or the loop count changes. Final goal state (4.2): succeeded when the
  Root succeeded; canceled when the run stopped after a cancel request; aborted otherwise.
* ``~/run`` (``RunInfo``, kept for late joiners): published empty at start-up, then at the start
  of each run.
* ``~/status`` (``TreeStatus``, kept for late joiners, deadline 1 s): on change at most
  ``status_rate_hz`` times a second and at least every ``heartbeat_period_s``; carries the
  outcome of the last run, the leaves cancelling and the unsettled goals.
* ``~/blackboard``, ``~/details`` (numbered, at most ``blackboard_rate_hz`` batches a second;
  the blackboard in full at the start of a run), ``~/log`` (numbered records in batches, kept
  for late joiners).
* Services ``~/get_snapshot``, ``~/get_log``, ``~/apply``, ``~/describe``, ``~/validate``,
  ``~/pause`` (``std_srvs/SetBool``), ``~/reset`` (``std_srvs/Trigger``).

The run id is the goal id (16 bytes; 32 hex digits in texts and file names). Each run writes
its tree log to ``log_dir`` (8.5); ERROR and WARNING records also go to ``/rosout``. Goals sent
by leaves are kept in the goal journal (8.4, :mod:`.journal`). A goal may name a watchdog
(4.4): the run is stopped when the named ``TreeStatus`` topic has not shown the named run as
going for the given time. SIGINT and SIGTERM stop a running tree as Stop does, wait for its
cancels up to the cancel timeout, and end the process (8.2).

Optional parts written elsewhere in this package are imported when present, so the node works
without them: ``ros_leaves.register(registry)`` (the ROS leaf classes), ``sources.SourceManager``
(entries fed from topics), ``ros_types.describe`` (``~/describe`` kinds other than
``node_types``) and ``ros_validate.validate_ros`` (the ROS checks of 8.3).
"""

from __future__ import annotations

import array
import importlib
import importlib.util
import json
import os
import signal
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import rclpy
from builtin_interfaces.msg import Time as TimeMsg
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.exceptions import ParameterUninitializedException
from rclpy.executors import Executor, SingleThreadedExecutor
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.signals import SignalHandlerOptions
from rclpy.task import Future
from std_srvs.srv import SetBool, Trigger
from unique_identifier_msgs.msg import UUID

from bt_server_core.blackboard import Blackboard, OpError
from bt_server_core.bundle import validate_bundle
from bt_server_core.engine import BundleError, CommandRefused, Outcome, RunResult, State, TreeRunner
from bt_server_core.registry import Registry, RegistryError
from bt_server_core.treelog import LogLevel, TreeLogWriter, delete_old_logs, read_records
from bt_server_core.values import decode_value, dumps_json, encode_value, loads_json
from bt_server_interfaces.action import ExecuteTree
from bt_server_interfaces.msg import BlackboardUpdate, LogBatch, LogRecord, NodeDetails, RunInfo, TreeStatus
from bt_server_interfaces.srv import Apply, Describe, GetLog, GetSnapshot, Validate

from .journal import GoalJournal
from .qos import BLACKBOARD_QOS, DETAILS_QOS, LOG_QOS, RUN_QOS, STATUS_QOS, WATCHDOG_QOS
from .rclpy_scheduler import RclpyScheduler
from .run_context import ClientCache, RunContext

__all__ = ["ExecutorNode", "check_bundle", "decode_ops", "main"]

NODE_NAME = "bt_executor"
#: Log records are published at most this often, or when this many are waiting.
_LOG_BATCH_S = 0.1
_LOG_BATCH_MAX = 100
#: ``~/get_log`` returns at most this many records per call.
_GET_LOG_MAX = 1000
#: Runs whose log files ``~/get_log`` still finds by id.
_RUNS_KEPT = 50
#: How often unused cached clients are looked for.
_SWEEP_PERIOD_S = 60.0


# ---------------------------------------------------------------------------------------------
# Small helpers


def _optional(module: str) -> Any:
    """``bt_server_executor.<module>`` if that file exists, else None. Errors inside an existing
    module are raised: a broken module must not pass for a missing one."""
    name = f"{__package__}.{module}"
    if importlib.util.find_spec(name) is None:
        return None
    return importlib.import_module(name)


def _uuid_msg(data: bytes) -> UUID:
    return UUID(uuid=list(data))


def _time_msg(stamp: float) -> TimeMsg:
    seconds = int(stamp)
    return TimeMsg(sec=seconds, nanosec=int((stamp - seconds) * 1e9))


def _json(data: Any) -> str:
    """JSON text of report data (details, findings): NaN allowed; anything else not JSON as its repr."""
    return json.dumps(data, ensure_ascii=False, allow_nan=True, default=repr)


def check_bundle(bundle: Any) -> str | None:
    """Why ``bundle`` cannot be read (the reasons of :class:`BundleError`), or None. Validation
    needs this much before it can look at the trees."""
    if not isinstance(bundle, dict):
        return "the bundle is not a JSON object"
    if bundle.get("format", "behavior_tree_bundle") != "behavior_tree_bundle":
        return f"not a bundle: format {bundle.get('format')!r}"
    trees, root = bundle.get("trees"), bundle.get("root")
    if not isinstance(trees, dict) or not isinstance(root, str) or root not in trees:
        return "the bundle names no root tree among its trees"
    for key, tree in trees.items():
        if not isinstance(tree, dict) or not isinstance(tree.get("nodes"), list):
            return f"the tree {key!r} of the bundle has no list of nodes"
        if not isinstance(tree.get("connections", []), list):
            return f"the tree {key!r} of the bundle has no list of connections"
    if not any(isinstance(item, dict) and item.get("type") == "Root" for item in trees[root]["nodes"]):
        return f"the tree {root!r} has no Root node"
    return None


def decode_ops(ops: Any) -> list[dict]:
    """``~/apply`` operations with their values decoded from the file encoding (contract 6).
    Raises ValueError."""
    if not isinstance(ops, list):
        raise ValueError("ops_json is not a list of operations")
    result = []
    for op in ops:
        if not isinstance(op, dict):
            raise ValueError("an operation is an object with 'op'")
        op = dict(op)
        kind = op.get("op")
        if kind == "bb.add" and isinstance(op.get("entry"), dict) and "value" in op["entry"]:
            op["entry"] = {**op["entry"], "value": decode_value(op["entry"]["value"])}
        elif kind in ("bb.set_value", "node.set_field") and "value" in op:
            op["value"] = decode_value(op["value"])
        result.append(op)
    return result


def _parse_blackboard(text: str) -> list:
    """The goal's blackboard entries. Raises ValueError."""
    if not text.strip():
        return []
    data = loads_json(text)
    if isinstance(data, dict) and isinstance(data.get("blackboard"), list):
        data = data["blackboard"]
    if not isinstance(data, list):
        raise ValueError("the blackboard is not a list of entries")
    return data


# ---------------------------------------------------------------------------------------------
# One run


@dataclass
class _Run:
    """What the executor keeps of one run."""

    runner: TreeRunner
    blackboard: Blackboard
    context: RunContext
    label: str
    requested_by: str
    bundle_json: str
    watchdog: tuple[str, bytes, float] | None
    uuid: bytes = bytes(16)
    goal_handle: Any = None
    writer: TreeLogWriter | None = None
    finished: Future | None = None
    result: RunResult | None = None
    watchdog_task: _Watchdog | None = None
    watchdog_fired: str | None = None
    sources: Any = None
    active: bool = True

    @property
    def hex(self) -> str:
        return self.uuid.hex()


class _Watchdog:
    """Stops a run when ``topic`` has not shown ``run_uuid`` going (a state other than IDLE) for
    ``timeout`` seconds (4.4, 8.2). Counting starts when the run starts."""

    def __init__(self, node: Node, scheduler: RclpyScheduler, topic: str, run_uuid: bytes, timeout: float,
                 on_silent: Callable[[], None]) -> None:
        self._node = node
        self._scheduler = scheduler
        self._run_uuid = run_uuid
        self._timeout = timeout
        self._on_silent = on_silent
        self._last = scheduler.now()
        self._subscription = node.create_subscription(TreeStatus, topic, self._on_status, WATCHDOG_QOS)
        self._timer: Any = scheduler.call_later(timeout, self._check)

    def _on_status(self, msg: TreeStatus) -> None:
        if bytes(bytearray(msg.run_id.uuid)) == self._run_uuid and msg.state != TreeStatus.STATE_IDLE:
            self._last = self._scheduler.now()

    def _check(self) -> None:
        self._timer = None
        due = self._last + self._timeout
        if self._scheduler.now() >= due:
            self._on_silent()
        else:
            self._timer = self._scheduler.call_at(due, self._check)

    def close(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._subscription is not None:
            self._node.destroy_subscription(self._subscription)
            self._subscription = None


class _Sink:
    """The engine's :class:`~bt_server_core.engine.Sink` for one run: reports go to the node
    while the run is the current one."""

    def __init__(self, owner: ExecutorNode) -> None:
        self._owner = owner
        self.run: _Run | None = None

    def _current(self) -> bool:
        return self.run is not None and self.run is self._owner.current_run

    def state(self, state: State, loop: int) -> None:
        if self._current():
            self._owner.on_state(self.run, state, loop)

    def statuses(self, changed: Iterable[int]) -> None:
        if self._current():
            self._owner.status_changed()

    def detail(self, index: int, kind: str, value: object) -> None:
        if self._current():
            self._owner.on_detail(index, kind, value)

    def blackboard(self, changes: dict) -> None:
        if self._current():
            self._owner.on_blackboard(changes)

    def log(self, level: int, text: str, index: int = -1) -> None:
        if self.run is not None:
            self._owner.on_log(self.run, level, text, index)


# ---------------------------------------------------------------------------------------------
# The node


class ExecutorNode(Node):
    """The executor node (see the module docs). Made on the thread that will spin ``executor``."""

    def __init__(self, executor: Executor) -> None:
        super().__init__(NODE_NAME)
        self._executor = executor
        executor.add_node(self)
        self.scheduler = RclpyScheduler(self, executor)
        self.full_name = self.get_fully_qualified_name()
        file_name = self.full_name.strip("/").replace("/", "_") or NODE_NAME
        self._read_parameters(file_name)
        self._workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bt_executor")

        # node types: built-in, ROS leaves (if their module is there), leaf modules
        self.registry = Registry()
        ros_leaves = _optional("ros_leaves")
        if ros_leaves is not None:
            ros_leaves.register(self.registry)
        for module in self.leaf_modules:
            try:
                names = self.registry.load_module(module)
                self.get_logger().info(f"leaf module {module!r}: {', '.join(names) or 'no leaf classes'}")
            except RegistryError as error:
                self.get_logger().error(str(error))
        for problem in self.registry.run_setup():
            self.get_logger().warning(problem)
        self._ros_types = _optional("ros_types")
        self._ros_validate = _optional("ros_validate")
        sources = _optional("sources")
        self._sources = sources.SourceManager(self, self.scheduler, self._log_to_run) if sources is not None else None

        # run state
        self.current_run: _Run | None = None
        self._prepared: _Run | None = None
        self._busy = False
        self._shutting_down = False
        self.exit_requested = False
        self._exit_at: float | None = None
        self._last_outcome = 0
        self._last_message = ""
        self._runs: OrderedDict[str, _Run] = OrderedDict()
        self._status_seq = 0
        self._last_status_at = float("-inf")
        self._status_timer: Any = None
        self._heartbeat: Any = None
        self._blackboard_seq = 0
        self._bb_pending: dict[str, dict] = {}
        self._bb_removed: dict[str, None] = {}
        self._bb_order: list[str] | None = None
        self._bb_timer: Any = None
        self._last_bb_at = float("-inf")
        self._details_seq = 0
        self._details_pending: dict[tuple[int, str], dict] = {}
        self._details_timer: Any = None
        self._last_details_at = float("-inf")
        self._log_pending: list[Any] = []
        self._log_pending_run: _Run | None = None
        self._log_timer: Any = None

        # the tree API
        self._run_pub = self.create_publisher(RunInfo, "~/run", RUN_QOS)
        self._status_pub = self.create_publisher(TreeStatus, "~/status", STATUS_QOS)
        self._blackboard_pub = self.create_publisher(BlackboardUpdate, "~/blackboard", BLACKBOARD_QOS)
        self._details_pub = self.create_publisher(NodeDetails, "~/details", DETAILS_QOS)
        self._log_pub = self.create_publisher(LogBatch, "~/log", LOG_QOS)
        self.status_topic = self._status_pub.topic_name
        self._services = [
            self.create_service(GetSnapshot, "~/get_snapshot", self._on_get_snapshot),
            self.create_service(GetLog, "~/get_log", self._on_get_log),
            self.create_service(Apply, "~/apply", self._on_apply),
            self.create_service(Describe, "~/describe", self._on_describe),
            self.create_service(Validate, "~/validate", self._on_validate),
            self.create_service(SetBool, "~/pause", self._on_pause),
            self.create_service(Trigger, "~/reset", self._on_reset),
        ]
        self.client_cache = ClientCache(self, self.scheduler.now)
        self.journal = GoalJournal(self.journal_file, self, self.scheduler, self._log_to_node, self.status_changed)
        self._action_server = ActionServer(
            self, ExecuteTree, "~/execute",
            execute_callback=self._execute,
            goal_callback=self._on_goal,
            cancel_callback=self._on_cancel,
        )

        self._run_pub.publish(RunInfo(stamp=self.get_clock().now().to_msg()))  # "nothing loaded" (4.1)
        self._publish_status()
        self.journal.start()
        self._workers.submit(delete_old_logs, self.log_dir, self.log_keep_days)
        self.scheduler.call_later(_SWEEP_PERIOD_S, self._sweep)
        self.get_logger().info(f"executor {self.full_name} ready; logs in {self.log_dir}, journal {self.journal.path}")

    # ----------------------------------------------------------------- parameters (8.1)
    def _read_parameters(self, file_name: str) -> None:
        base = os.path.join("~", ".ros", "bt_server", file_name)
        self.declare_parameter("leaf_modules", Parameter.Type.STRING_ARRAY)
        self.declare_parameter("log_dir", os.path.join(base, "log"))
        self.declare_parameter("log_keep_days", 30.0)
        self.declare_parameter("max_tick_rate_hz", 200.0)
        self.declare_parameter("status_rate_hz", 30.0)
        self.declare_parameter("heartbeat_period_s", 0.5)
        self.declare_parameter("blackboard_rate_hz", 10.0)
        self.declare_parameter("journal_file", os.path.join(base, "goals.jsonl"))

        def number(name: str) -> float:
            return float(self.get_parameter(name).value)

        try:
            modules = self.get_parameter("leaf_modules").value
        except ParameterUninitializedException:
            modules = None
        self.leaf_modules = [name for name in (modules or []) if name]
        self.log_dir = os.path.expanduser(self.get_parameter("log_dir").value or os.path.join(base, "log"))
        self.log_keep_days = number("log_keep_days")
        self.max_tick_rate_hz = number("max_tick_rate_hz")
        self.status_period = 1.0 / max(number("status_rate_hz"), 0.1)
        self.heartbeat_period = max(number("heartbeat_period_s"), 0.01)
        self.blackboard_period = 1.0 / max(number("blackboard_rate_hz"), 0.1)
        self.journal_file = os.path.expanduser(self.get_parameter("journal_file").value or os.path.join(base, "goals.jsonl"))

    # ----------------------------------------------------------------- logging
    def _log_to_node(self, level: int, text: str) -> None:
        """Executor messages outside a run's tree log: to /rosout."""
        logger = self.get_logger()
        if level == LogLevel.ERROR:
            logger.error(text)
        elif level == LogLevel.WARNING:
            logger.warning(text)
        else:
            logger.info(text)

    def _log_to_run(self, level: int, text: str) -> None:
        """Into the current run's tree log (and /rosout for ERROR and WARNING); /rosout alone
        between runs."""
        run = self.current_run
        if run is not None and run.active:
            self.on_log(run, level, text, -1)
        else:
            self._log_to_node(level, text)

    def on_log(self, run: _Run, level: int, text: str, index: int) -> None:
        if level in (LogLevel.ERROR, LogLevel.WARNING):
            self._log_to_node(level, text)
        if run.writer is None:
            return
        record = run.writer.write(level, text, index=index)
        if record is None:
            return
        if self._log_pending_run is not run:
            self._flush_log()
            self._log_pending_run = run
        self._log_pending.append(record)
        if len(self._log_pending) >= _LOG_BATCH_MAX:
            self._flush_log()
        elif self._log_timer is None:
            self._log_timer = self.scheduler.call_later(_LOG_BATCH_S, self._flush_log)

    def _flush_log(self) -> None:
        if self._log_timer is not None:
            self._log_timer.cancel()
            self._log_timer = None
        records, self._log_pending = self._log_pending, []
        run = self._log_pending_run
        if not records or run is None:
            return
        batch = LogBatch(run_id=_uuid_msg(run.uuid), seq=records[0].seq)
        batch.records = [LogRecord(stamp=_time_msg(item.stamp), level=item.level, node_index=item.index,
                                   text=item.text) for item in records]
        self._log_pub.publish(batch)

    # ----------------------------------------------------------------- ~/status (4.1, 4.2)
    def _unsettled(self) -> int:
        run = self.current_run
        count = self.journal.unsettled_count()
        if run is not None and run.active:
            count += max(0, run.runner.unsettled - self.journal.unsettled_count(run.hex))
        return count

    def _status_msg(self) -> TreeStatus:
        msg = TreeStatus(stamp=self.get_clock().now().to_msg(), seq=self._status_seq)
        run = self.current_run
        msg.unsettled = self._unsettled()
        if run is None:
            return msg
        runner = run.runner
        msg.run_id = _uuid_msg(run.uuid)
        msg.state = int(runner.state)
        msg.loop_count = runner.loop_count
        msg.cancelling = runner.cancelling
        if not run.active:
            msg.outcome = self._last_outcome
            msg.message = self._last_message
        msg.node_status = array.array("B", runner.statuses)
        return msg

    def status_changed(self) -> None:
        """Something ``~/status`` shows changed: publish, at most ``status_rate_hz`` times a second."""
        if self._status_timer is not None:
            return
        wait = self._last_status_at + self.status_period - self.scheduler.now()
        if wait <= 0:
            self._publish_status()
        else:
            self._status_timer = self.scheduler.call_later(wait, self._publish_status)

    def _publish_status(self) -> None:
        if self._status_timer is not None:
            self._status_timer.cancel()
            self._status_timer = None
        self._status_seq += 1
        self._status_pub.publish(self._status_msg())
        self._last_status_at = self.scheduler.now()
        if self._heartbeat is not None:
            self._heartbeat.cancel()
        self._heartbeat = self.scheduler.call_later(self.heartbeat_period, self._publish_status)

    # ----------------------------------------------------------------- ~/blackboard, ~/details
    def on_blackboard(self, changes: dict) -> None:
        for entry_id in changes.get("removed") or ():
            self._bb_pending.pop(entry_id, None)
            self._bb_removed[entry_id] = None
        for entry in changes.get("entries") or ():
            self._bb_removed.pop(entry["id"], None)
            self._bb_pending[entry["id"]] = entry
        if changes.get("order") is not None:
            self._bb_order = list(changes["order"])
        self._bb_timer = self._rate_limited(self._bb_timer, self._last_bb_at, self.blackboard_period,
                                            self._publish_blackboard)

    def _rate_limited(self, timer: Any, last: float, period: float, publish: Callable[[], None]) -> Any:
        if timer is not None:
            return timer
        wait = last + period - self.scheduler.now()
        if wait <= 0:
            publish()
            return None
        return self.scheduler.call_later(wait, publish)

    def _publish_blackboard(self, full: bool = False) -> None:
        if self._bb_timer is not None:
            self._bb_timer.cancel()
            self._bb_timer = None
        run = self.current_run
        if run is None:
            return
        if full:
            board = run.blackboard
            update = {"entries": [board.entry(entry_id) for entry_id in board.ids()], "removed": [],
                      "order": board.ids()}
        else:
            if not self._bb_pending and not self._bb_removed and self._bb_order is None:
                return
            update = {"entries": list(self._bb_pending.values()), "removed": list(self._bb_removed),
                      "order": self._bb_order}
        self._bb_pending, self._bb_removed, self._bb_order = {}, {}, None
        self._blackboard_seq += 1
        self._blackboard_pub.publish(BlackboardUpdate(run_id=_uuid_msg(run.uuid), seq=self._blackboard_seq,
                                                      full=full, update_json=dumps_json(update, indent=None)))
        self._last_bb_at = self.scheduler.now()

    def on_detail(self, index: int, kind: str, value: object) -> None:
        self._details_pending[(index, kind)] = {"i": index, "kind": kind, "value": value, "stamp": time.time()}
        self._details_timer = self._rate_limited(self._details_timer, self._last_details_at, self.blackboard_period,
                                                 self._publish_details)

    def _publish_details(self) -> None:
        if self._details_timer is not None:
            self._details_timer.cancel()
            self._details_timer = None
        run = self.current_run
        if run is None or not self._details_pending:
            return
        items, self._details_pending = list(self._details_pending.values()), {}
        self._details_seq += 1
        self._details_pub.publish(NodeDetails(run_id=_uuid_msg(run.uuid), seq=self._details_seq,
                                              details_json=_json(items)))
        self._last_details_at = self.scheduler.now()

    def on_state(self, run: _Run, state: State, loop: int) -> None:
        if run.goal_handle is not None and run.active:
            run.goal_handle.publish_feedback(ExecuteTree.Feedback(state=int(state), loop_count=loop))
        self.status_changed()

    # ----------------------------------------------------------------- ~/execute (4.4, 8.2)
    def _on_goal(self, goal: ExecuteTree.Goal) -> GoalResponse:
        if self._shutting_down:
            self.get_logger().info("goal rejected: the executor is shutting down")
            return GoalResponse.REJECT
        if self._busy:
            run = self.current_run
            who = f": '{run.label}' started by {run.requested_by}" if run is not None and run.active else ""
            self.get_logger().info(f"goal rejected: a run is active{who}")
            return GoalResponse.REJECT
        try:
            self._prepared = self._prepare(goal)
        except (BundleError, ValueError) as error:
            self.get_logger().warning(f"goal rejected: {error}")
            return GoalResponse.REJECT
        self._busy = True
        return GoalResponse.ACCEPT

    def _prepare(self, goal: ExecuteTree.Goal) -> _Run:
        """Read the goal: the runner is made here, so that a bundle that cannot be read rejects
        the goal (R-13). Raises BundleError or ValueError."""
        entries = _parse_blackboard(goal.blackboard_json)
        blackboard = Blackboard(entries)
        sink = _Sink(self)
        context = RunContext(node=self, executor_name=self.full_name, status_topic=self.status_topic,
                             journal=self.journal, clients=self.client_cache, scheduler=self.scheduler,
                             log=self._log_to_run)
        holder: list[_Run] = []
        runner = TreeRunner(goal.bundle_json, blackboard, self.registry, self.scheduler, sink,
                            lambda result: self._on_finished(holder[0], result), run_context=context,
                            max_tick_rate_hz=self.max_tick_rate_hz)
        context.cancel_timeout_s = runner.config["cancel_timeout_ms"] / 1000.0
        watchdog = None
        if goal.watchdog_topic and goal.watchdog_timeout_s > 0:
            watchdog = (goal.watchdog_topic, bytes(bytearray(goal.watchdog_run_id.uuid)), float(goal.watchdog_timeout_s))
        run = _Run(runner=runner, blackboard=blackboard, context=context, label=goal.label or "tree",
                   requested_by=goal.requested_by, bundle_json=goal.bundle_json, watchdog=watchdog)
        holder.append(run)
        sink.run = run
        for problem in blackboard.problems:
            self.get_logger().warning(f"goal blackboard: {problem}")
        return run

    def _on_cancel(self, goal_handle: Any) -> CancelResponse:
        run = self.current_run
        if run is not None and run.active and run.goal_handle is goal_handle:
            self.scheduler.call_soon(run.runner.stop)
        return CancelResponse.ACCEPT

    async def _execute(self, goal_handle: Any) -> ExecuteTree.Result:
        run = self._prepared
        self._prepared = None
        if run is None:  # cannot happen: every accepted goal was prepared
            goal_handle.abort()
            return ExecuteTree.Result(outcome=Outcome.ERROR, message="the run was not prepared")
        run.goal_handle = goal_handle
        run.uuid = bytes(bytearray(goal_handle.goal_id.uuid))
        run.context.set_run_id(run.uuid)
        run.finished = Future()
        self._start(run)
        if goal_handle.is_cancel_requested and run.active:
            run.runner.stop()
        result: RunResult = await run.finished
        response = ExecuteTree.Result(outcome=int(result.outcome), message=result.message,
                                      loops_completed=result.loops_completed,
                                      unconfirmed_cancels=result.unconfirmed_cancels,
                                      blackboard_json=dumps_json(result.blackboard, indent=None))
        if result.outcome == Outcome.SUCCEEDED:
            goal_handle.succeed()
        elif result.outcome == Outcome.STOPPED and goal_handle.is_cancel_requested and result.stopped_by_command \
                and run.watchdog_fired is None:
            goal_handle.canceled()
        else:
            goal_handle.abort()
        if self._shutting_down:
            self._exit_soon()
        return response

    def _start(self, run: _Run) -> None:
        """Set the run up and make its first tick (8.2)."""
        self._flush_log()
        self._bb_pending, self._bb_removed, self._bb_order = {}, {}, None
        self._details_pending = {}
        self.current_run = run
        self._runs[run.hex] = run
        while len(self._runs) > _RUNS_KEPT:
            self._runs.popitem(last=False)
        run.writer = TreeLogWriter(self.log_dir, run.label, keep_days=self.log_keep_days,
                                   on_error=lambda text: self.scheduler.call_soon(lambda: self._log_to_node(1, text)))
        runner = run.runner
        self._run_pub.publish(RunInfo(stamp=self.get_clock().now().to_msg(), run_id=_uuid_msg(run.uuid),
                                      label=run.label, requested_by=run.requested_by, bundle_json=run.bundle_json,
                                      node_paths=list(runner.node_paths)))
        self._publish_blackboard(full=True)
        self.on_log(run, LogLevel.INFO, f"Run {run.hex} of '{run.label}' requested by {run.requested_by or 'unknown'}", -1)
        self._check_at_start(run)
        self._prepare_leaves(run)
        if run.watchdog is not None:
            topic, watched, timeout = run.watchdog
            run.watchdog_task = _Watchdog(self, self.scheduler, topic, watched, timeout,
                                          lambda: self._watchdog_silent(run))
        if self._sources is not None:
            try:
                self._sources.start(run.blackboard, runner.request_tick)
                run.sources = self._sources
            except Exception as error:  # noqa: BLE001 - sources are optional; the run goes on
                self.on_log(run, LogLevel.ERROR, f"The sources could not be started: {error}", -1)
        runner.start()

    def _check_at_start(self, run: _Run) -> None:
        """The checks of ``~/validate`` (8.3), logged: findings do not block a run."""
        try:
            bundle = loads_json(run.bundle_json)
            entries = run.blackboard.to_list()
            findings = self._findings(bundle, entries)
        except Exception as error:  # noqa: BLE001 - a check that fails must not stop the run
            findings = [{"path": "", "severity": "warning", "text": f"the bundle could not be checked: {error}"}]
        for finding in findings:
            self.on_log(run, LogLevel.WARNING, f"Check: {finding.get('path')}: {finding.get('text')}", -1)

    def _prepare_leaves(self, run: _Run) -> None:
        """Call ``prepare(ctx)`` of every leaf object that has one (see :mod:`.run_context`)."""
        runner = run.runner
        for index in range(len(runner.node_paths)):
            node = runner.node(index)
            prepare = getattr(getattr(node, "leaf", None), "prepare", None)
            if callable(prepare):
                try:
                    prepare(node.ctx)
                except Exception as error:  # noqa: BLE001 - leaf code
                    self.on_log(run, LogLevel.WARNING, f"Node {node.label}: prepare failed: {error}", index)

    def _watchdog_silent(self, run: _Run) -> None:
        if not run.active or run.watchdog is None:
            return
        topic, watched, timeout = run.watchdog
        run.watchdog_fired = (f"Stopped by the watchdog: {topic} has not shown run {watched.hex()} going "
                              f"for {timeout:g} s")
        self.on_log(run, LogLevel.WARNING, run.watchdog_fired, -1)
        run.runner.stop()

    def _on_finished(self, run: _Run, result: RunResult) -> None:
        """The runner ended the run (engine thread)."""
        if run.watchdog_fired is not None and result.outcome == Outcome.STOPPED:
            result.message = run.watchdog_fired
        run.result = result
        run.active = False
        if run.watchdog_task is not None:
            run.watchdog_task.close()
            run.watchdog_task = None
        if run.sources is not None:
            try:
                run.sources.stop()
            except Exception as error:  # noqa: BLE001
                self._log_to_node(LogLevel.ERROR, f"The sources could not be stopped: {error}")
            run.sources = None
        self.journal.run_ended(run.hex)
        self.client_cache.run_ended()
        self._last_outcome = int(result.outcome)
        self._last_message = result.message
        if run is self.current_run:
            self._publish_blackboard()
            self._publish_details()
        self._flush_log()
        if run.writer is not None:
            run.writer.close()
        self._busy = False
        self.status_changed()
        if run.finished is not None and not run.finished.done():
            run.finished.set_result(result)

    # ----------------------------------------------------------------- services
    def _on_get_snapshot(self, request: GetSnapshot.Request, response: GetSnapshot.Response) -> GetSnapshot.Response:
        run = self.current_run
        response.status = self._status_msg()
        response.blackboard_seq = self._blackboard_seq
        response.details_seq = self._details_seq
        if run is None:
            response.run = RunInfo()
            response.blackboard_json = "[]"
            response.details_json = "[]"
            return response
        response.run = RunInfo(stamp=self.get_clock().now().to_msg(), run_id=_uuid_msg(run.uuid), label=run.label,
                               requested_by=run.requested_by, bundle_json=run.bundle_json,
                               node_paths=list(run.runner.node_paths))
        board = run.blackboard
        response.blackboard_json = dumps_json([board.entry(entry_id) for entry_id in board.ids()], indent=None)
        response.details_json = _json([{"i": index, "kind": kind, "value": value}
                                       for index, kinds in sorted(run.runner.details().items())
                                       for kind, value in kinds.items()])
        response.log_seq = max(0, run.writer.next_seq - 1) if run.writer is not None else 0
        return response

    async def _on_get_log(self, request: GetLog.Request, response: GetLog.Response) -> GetLog.Response:
        wanted = bytes(bytearray(request.run_id.uuid))
        run = self.current_run if not any(wanted) else self._runs.get(wanted.hex())
        response.next_seq = request.from_seq
        count = min(request.max_records or _GET_LOG_MAX, _GET_LOG_MAX)
        if run is None or run.writer is None or count <= 0:
            return response
        writer, from_seq = run.writer, int(request.from_seq)

        def read() -> list:
            writer.flush(2.0)
            return read_records(writer.path, from_seq, count)

        records = await self._in_thread(read)
        response.records = [LogRecord(stamp=_time_msg(item.stamp), level=item.level, node_index=item.index,
                                      text=item.text) for item in records]
        response.next_seq = from_seq + len(records)
        return response

    def _on_apply(self, request: Apply.Request, response: Apply.Response) -> Apply.Response:
        # a goal accepted but not started yet already holds the blackboard (design 7.2)
        run = self._prepared if self._prepared is not None else self.current_run
        if run is None or not run.active:
            response.result = Apply.Response.NO_RUN
            response.message = "no run is active"
            return response
        try:
            ops = decode_ops(loads_json(request.ops_json))
            run.runner.apply(ops)
        except (OpError, ValueError, TypeError) as error:
            response.result = Apply.Response.REJECTED
            response.message = str(error)
            return response
        for op in ops:
            if op.get("op") == "node.set_field":
                self._report_field(run.runner, op)
        response.result = Apply.Response.APPLIED
        return response

    @staticmethod
    def _report_field(runner: TreeRunner, op: dict) -> None:
        """A field set through ``~/apply`` goes back as a ``field`` detail of each instance of
        the node: the gateway changes its document only then (design 9.3)."""
        for index in range(len(runner.node_paths)):
            node = runner.node(index)
            fields = getattr(node, "fields", None)
            if not isinstance(fields, dict) or getattr(node, "node_id", None) != op.get("id"):
                continue
            if op.get("tree") is not None and getattr(node, "tree_key", None) != op.get("tree"):
                continue
            try:
                value = encode_value(fields.get(op.get("field")))
            except TypeError:
                value = None
            runner.detail(index, "field", {"tree": node.tree_key, "id": node.node_id, "field": op.get("field"),
                                           "value": value})

    async def _on_describe(self, request: Describe.Request, response: Describe.Response) -> Describe.Response:
        try:
            arg = loads_json(request.arg_json) if request.arg_json.strip() else {}
            if request.kind == "node_types":
                result: Any = {"types": self.registry.catalog()}
            elif self._ros_types is None:
                raise ValueError(f"'{request.kind}' needs the ROS type support, which this executor does not have")
            else:
                result = await self._in_thread(lambda: self._ros_types.describe(self, request.kind, arg))
            response.ok = True
            response.result_json = _json(result)
        except Exception as error:  # noqa: BLE001 - every problem is an answer, not a crash
            response.ok = False
            response.result_json = _json({"reason": str(error)})
        return response

    async def _on_validate(self, request: Validate.Request, response: Validate.Response) -> Validate.Response:
        try:
            bundle = loads_json(request.bundle_json)
            problem = check_bundle(bundle)
            entries = _parse_blackboard(request.blackboard_json)
        except ValueError as error:
            problem = f"the bundle or the blackboard is not JSON: {error}"
        if problem is not None:
            response.ok = False
            response.findings_json = _json([{"path": "", "severity": "error", "text": problem}])
            return response
        board = Blackboard(entries)
        listed = board.to_list()
        findings = await self._in_thread(lambda: self._findings(bundle, listed))
        findings.extend({"path": "", "severity": "warning", "text": text} for text in board.problems)
        response.ok = True
        response.findings_json = _json(findings)
        return response

    def _findings(self, bundle: dict, entries: list[dict]) -> list[dict]:
        findings = validate_bundle(bundle, entries, self.registry.catalog(), object_names=list(self.registry.objects))
        if self._ros_validate is not None:
            findings.extend(self._ros_validate.validate_ros(self, bundle, entries))
        return findings

    def _on_pause(self, request: SetBool.Request, response: SetBool.Response) -> SetBool.Response:
        run = self.current_run
        if run is None or not run.active:
            response.success = False
            response.message = f"{'Pause' if request.data else 'Resume'} is refused: no run is active"
            return response
        try:
            if request.data:
                run.runner.pause()
            else:
                run.runner.resume()
        except CommandRefused as error:
            response.success = False
            response.message = str(error)
            return response
        response.success = True
        return response

    def _on_reset(self, request: Trigger.Request, response: Trigger.Response) -> Trigger.Response:
        run = self.current_run
        if run is not None:
            try:
                run.runner.reset()
            except CommandRefused as error:
                response.success = False
                response.message = str(error)
                return response
            if not run.active:
                self.status_changed()
        response.success = True
        return response

    def _in_thread(self, fn: Callable[[], Any]) -> Future:
        """Run ``fn`` on a worker thread; the returned rclpy Future completes on the engine thread."""
        future: Future = Future()

        def work() -> None:
            try:
                value = fn()
            except Exception as error:  # noqa: BLE001 - handed to the awaiting callback
                self.scheduler.call_soon(lambda: future.set_exception(error))
            else:
                self.scheduler.call_soon(lambda: future.set_result(value))

        self._workers.submit(work)
        return future

    def _sweep(self) -> None:
        self.client_cache.sweep()
        self.scheduler.call_later(_SWEEP_PERIOD_S, self._sweep)

    # ----------------------------------------------------------------- shutting down (8.2)
    def request_shutdown(self) -> None:
        """SIGINT or SIGTERM: stop a running tree as Stop does, wait for its cancels up to the
        cancel timeout, then exit. A second signal exits at once."""
        if self._shutting_down:
            self.exit_requested = True
            return
        self._shutting_down = True
        run = self.current_run
        if run is None or not run.active:
            self.exit_requested = True
            return
        self.on_log(run, LogLevel.WARNING, "The executor is shutting down: stopping the tree", -1)
        self._exit_at = self.scheduler.now() + run.context.cancel_timeout_s
        run.runner.stop()

    def _exit_soon(self) -> None:
        """The last run ended while shutting down: let the result reach its client, then exit."""
        self.scheduler.call_later(0.2, lambda: setattr(self, "exit_requested", True))

    def should_exit(self) -> bool:
        return self.exit_requested or (self._exit_at is not None and self.scheduler.now() >= self._exit_at)

    def close(self) -> None:
        """Release what the node holds (after the last spin)."""
        run = self.current_run
        if run is not None and run.writer is not None:
            self._flush_log()
            run.writer.close()
            run.writer.flush(2.0)
        self.journal.close()
        self.client_cache.close()
        self._workers.shutdown(wait=False, cancel_futures=True)


def main(args: list[str] | None = None) -> int:
    """The executor process: ``python -m bt_server_executor [--ros-args ...]``."""
    rclpy.init(args=args, signal_handler_options=SignalHandlerOptions.NO)
    try:
        from rclpy.experimental import EventsExecutor
        executor: Executor = EventsExecutor()
    except Exception:  # noqa: BLE001 - the default executor is a drop-in fallback (8.2)
        executor = SingleThreadedExecutor()
    node = ExecutorNode(executor)

    def on_signal(signum: int, frame: Any) -> None:
        node.scheduler.call_soon(node.request_shutdown)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    try:
        while not node.should_exit() and rclpy.ok():
            executor.spin_once(timeout_sec=0.1)
    finally:
        node.close()
        executor.shutdown()
        node.destroy_node()
        rclpy.try_shutdown()
    return 0
