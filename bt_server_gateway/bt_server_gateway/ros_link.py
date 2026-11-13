"""``RosLink``: the gateway's client of an executor's tree API over ROS (design 3.3, 4, 9.1;
contract 6).

**Threads.** :class:`RosLinkFactory` starts one rclpy node, ``bt_gateway``, in a context of its
own, spun by a ``SingleThreadedExecutor`` on a second thread (rclpy has no asyncio action
client, ``docs/VERIFIED.md``). Every ROS callback is handed to the asyncio loop with
``loop.call_soon_threadsafe``; every call into ROS (creating entities, sending requests and
goals) is queued to the ROS thread (:meth:`_RosThread.call`), whose rclpy futures are bridged to
asyncio futures. Everything else of this module runs on the asyncio loop.

**Discovery.** Once a second the ROS thread lists the action servers of type
``bt_server_interfaces/action/ExecuteTree``; an executor's name is its action name without
``/execute``. ``on_executors(names)`` is called whenever the set changes.

**Liveness.** A link is ``online`` while ``~/status`` arrives: it goes offline when no status
has come for three heartbeats (1.5 s; "executor lost", design 16) and online again with the next
one. On coming online it asks for the catalog (``on_catalog``).

**Mirrors and gaps.** ``~/blackboard`` and ``~/details`` updates are numbered per executor
process (the numbering starts anew when the executor is lost or restarts). A number that skips
one means updates were lost: the link takes a snapshot (``~/get_snapshot``) and
reports the whole blackboard (``full``) and the details that changed, clearing those that are
gone; updates numbered up to the snapshot's are then dropped. ``~/log`` records are numbered per
run; a gap is filled from ``~/get_log`` before the later records are reported, so listeners see
every record once, in order. Updates of a run whose result has arrived are ignored (4.2).

**Values.** Blackboard entries and results reach listeners decoded (Python values), with every
other key the executor sent kept (``freshness``; ``problem`` while an entry's source cannot feed
it, design 7.4); ``apply``
encodes the values of its operations into the file encoding for ``ops_json``, and ``execute``
and ``validate`` send ``blackboard`` lists as they are (file form, encoded values).

**Stop** is a call of ``~/execute/_action/cancel_goal`` with the run id: it works for runs this
gateway did not start and after a restart of the gateway (4.5, X-12).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import rclpy
from action_msgs.msg import GoalStatus
from action_msgs.srv import CancelGoal
from rclpy.action import ActionClient
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.signals import SignalHandlerOptions
from rclpy.task import Future as RosFuture
from std_srvs.srv import SetBool, Trigger
from unique_identifier_msgs.msg import UUID

from bt_server_core.values import decode_value, dumps_json, encode_value, loads_json
from bt_server_executor.qos import BLACKBOARD_QOS, DETAILS_QOS, LOG_QOS, RUN_QOS, STATUS_SUB_QOS
from bt_server_interfaces.action import ExecuteTree
from bt_server_interfaces.msg import BlackboardUpdate, LogBatch, NodeDetails, RunInfo, TreeStatus
from bt_server_interfaces.srv import Apply, Describe, GetLog, GetSnapshot, Validate

from .link import (
    CommandRefused,
    GoalRejected,
    LinkDown,
    LinkRecord,
    LinkResult,
    LinkRun,
    LinkStatus,
    RunListener,
)
from .settings import Settings

__all__ = ["RosLink", "RosLinkFactory"]

log = logging.getLogger("bt_server_gateway")

ACTION_TYPE = "bt_server_interfaces/action/ExecuteTree"
#: An executor counts as lost after this long without ``~/status`` (three heartbeats).
LOST_AFTER_S = 1.5
#: How often the factory looks for executors.
DISCOVERY_PERIOD_S = 1.0
#: How long a service call or a goal reply may take.
CALL_TIMEOUT_S = 10.0
#: Records asked for per ``~/get_log`` call when a gap is filled.
_LOG_PAGE = 1000
_GOAL_STATUS = {GoalStatus.STATUS_SUCCEEDED: "succeeded", GoalStatus.STATUS_CANCELED: "canceled",
                GoalStatus.STATUS_ABORTED: "aborted"}


def _hex(uuid_msg: Any) -> str | None:
    """32 hex digits of a ``unique_identifier_msgs/UUID``; None for the all-zero id."""
    data = bytes(bytearray(uuid_msg.uuid))
    return data.hex() if any(data) else None


def _uuid(run_id: str | None) -> UUID:
    return UUID(uuid=list(bytes.fromhex(run_id)) if run_id else [0] * 16)


def _decoded_entry(entry: dict) -> dict:
    """An entry with its value decoded from the file encoding (an Object entry's ``repr`` stays)."""
    result = dict(entry)
    if entry.get("type") != "Object":
        try:
            result["value"] = decode_value(entry.get("value"))
        except ValueError:
            pass
    return result


def _decoded_update(update: dict) -> dict:
    return {"entries": [_decoded_entry(entry) for entry in update.get("entries") or ()],
            "removed": list(update.get("removed") or ()), "order": update.get("order")}


def _encoded_ops(ops: list[dict]) -> list[dict]:
    """Operations with their values in the file encoding (the inverse of the executor's decoding)."""
    result = []
    for op in ops:
        op = dict(op)
        kind = op.get("op")
        if kind == "bb.add" and isinstance(op.get("entry"), dict) and "value" in op["entry"]:
            op["entry"] = {**op["entry"], "value": encode_value(op["entry"]["value"])}
        elif kind in ("bb.set_value", "node.set_field") and "value" in op:
            op["value"] = encode_value(op["value"])
        result.append(op)
    return result


def _run_of(msg: RunInfo) -> LinkRun:
    bundle = None
    if msg.bundle_json:
        try:
            bundle = loads_json(msg.bundle_json)
        except ValueError:
            bundle = None
    return LinkRun(_hex(msg.run_id), msg.label, msg.requested_by, bundle, list(msg.node_paths))


def _status_of(msg: TreeStatus) -> LinkStatus:
    return LinkStatus(_hex(msg.run_id), msg.seq, msg.state, msg.loop_count, msg.cancelling, msg.unsettled,
                      msg.outcome, msg.message, bytes(bytearray(msg.node_status)))


def _record_of(seq: int, msg: Any) -> LinkRecord:
    return LinkRecord(seq, msg.stamp.sec + msg.stamp.nanosec / 1e9, msg.level, msg.node_index, msg.text)


# ---------------------------------------------------------------------------------------------
# The ROS thread


class _RosThread:
    """The ``bt_gateway`` node and the thread that spins it (see the module docs)."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.context = rclpy.Context()
        rclpy.init(context=self.context, signal_handler_options=SignalHandlerOptions.NO)
        self.node = Node("bt_gateway", context=self.context)
        self.executor = SingleThreadedExecutor(context=self.context)
        self.executor.add_node(self.node)
        self._queue: deque[Callable[[], None]] = deque()
        self._thread = threading.Thread(target=self._spin, name="bt_gateway:ros", daemon=True)
        self._thread.start()

    def _spin(self) -> None:
        try:
            self.executor.spin()
        except Exception:  # noqa: BLE001 - the context was shut down
            pass

    def _drain(self) -> None:
        for _ in range(len(self._queue)):
            fn = self._queue.popleft()
            try:
                fn()
            except Exception:  # noqa: BLE001 - reported through the future of call()
                log.exception("a call on the ROS thread raised")

    def submit(self, fn: Callable[[], None]) -> None:
        """Run ``fn`` on the ROS thread (from any thread)."""
        self._queue.append(fn)
        try:
            self.executor.create_task(self._drain)
        except Exception:  # noqa: BLE001 - shut down
            pass

    def call(self, fn: Callable[[], Any]) -> asyncio.Future:
        """Run ``fn`` on the ROS thread; the asyncio future gets its value. When the value is an
        rclpy future, the asyncio future gets that future's result once it is done."""
        future: asyncio.Future = self.loop.create_future()

        def deliver(setter: Callable[[Any], None], value: Any) -> None:
            self.loop.call_soon_threadsafe(lambda: None if future.done() else setter(value))

        def run() -> None:
            try:
                value = fn()
            except Exception as error:  # noqa: BLE001 - handed to the awaiting coroutine
                deliver(future.set_exception, error)
                return
            if isinstance(value, RosFuture):
                def done(ros_future: RosFuture) -> None:
                    error = ros_future.exception()
                    if error is not None:
                        deliver(future.set_exception, error)
                    else:
                        deliver(future.set_result, ros_future.result())
                value.add_done_callback(done)
            else:
                deliver(future.set_result, value)

        self.submit(run)
        return future

    def to_loop(self, fn: Callable[..., None]) -> Callable[..., None]:
        """A ROS callback that hands its arguments to ``fn`` on the asyncio loop."""
        def callback(*args: Any) -> None:
            try:
                self.loop.call_soon_threadsafe(fn, *args)
            except RuntimeError:  # the loop is closed
                pass
        return callback

    def close(self) -> None:
        try:
            self.executor.shutdown(timeout_sec=2.0)
        except Exception:  # noqa: BLE001
            pass
        self.node.destroy_node()
        rclpy.try_shutdown(context=self.context)
        self._thread.join(timeout=2.0)


# ---------------------------------------------------------------------------------------------
# One executor


class RosLink:
    """One executor reached over ROS (see the module docs and ``link.ExecutorLink``)."""

    def __init__(self, name: str, ros: _RosThread) -> None:
        self.name = "/" + name.strip("/")
        self.online = False
        self._ros = ros
        self._loop = ros.loop
        self._listeners: list[RunListener] = []
        self._started = False
        self._entities: list[tuple[str, Any]] = []
        self._action: ActionClient | None = None
        self._clients: dict[str, Any] = {}
        self._last_status_at = float("-inf")
        self._status: LinkStatus | None = None
        self._run = LinkRun(None)
        self._finished_runs: deque[str] = deque(maxlen=50)
        self._bb_seq: int | None = None
        self._details_seq: int | None = None
        self._details: dict[tuple[int, str], Any] = {}
        self._details_run: str | None = None
        self._repairing: asyncio.Task | None = None
        self._log_next: dict[str, int] = {}
        self._log_queue: asyncio.Queue | None = None
        self._log_task: asyncio.Task | None = None
        self._watch: asyncio.TimerHandle | None = None

    # ----------------------------------------------------------------- lifecycle
    def subscribe(self, listener: RunListener) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    def _emit(self, call: Callable[[RunListener], None]) -> None:
        for listener in list(self._listeners):
            try:
                call(listener)
            except Exception:  # noqa: BLE001 - one listener must not break the others
                log.exception("%s: a listener raised", self.name)

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._log_queue = asyncio.Queue()
        self._log_task = asyncio.create_task(self._log_worker())
        await self._ros.call(self._make_entities)
        self._watch = self._loop.call_later(0.5, self._check_alive)

    def _make_entities(self) -> None:
        """On the ROS thread: the subscriptions, the action client and the service clients."""
        node, to_loop, base = self._ros.node, self._ros.to_loop, self.name
        self._entities = [
            ("sub", node.create_subscription(RunInfo, f"{base}/run", to_loop(self._on_run_msg), RUN_QOS)),
            ("sub", node.create_subscription(TreeStatus, f"{base}/status", to_loop(self._on_status_msg),
                                             STATUS_SUB_QOS)),
            ("sub", node.create_subscription(BlackboardUpdate, f"{base}/blackboard", to_loop(self._on_blackboard_msg),
                                             BLACKBOARD_QOS)),
            ("sub", node.create_subscription(NodeDetails, f"{base}/details", to_loop(self._on_details_msg),
                                             DETAILS_QOS)),
            ("sub", node.create_subscription(LogBatch, f"{base}/log", to_loop(self._on_log_msg), LOG_QOS)),
        ]
        self._action = ActionClient(node, ExecuteTree, f"{base}/execute")
        for key, srv_type, service in (
            ("snapshot", GetSnapshot, "get_snapshot"), ("get_log", GetLog, "get_log"), ("apply", Apply, "apply"),
            ("describe", Describe, "describe"), ("validate", Validate, "validate"), ("pause", SetBool, "pause"),
            ("reset", Trigger, "reset"), ("cancel", CancelGoal, "execute/_action/cancel_goal"),
        ):
            self._clients[key] = node.create_client(srv_type, f"{base}/{service}")

    def _destroy_entities(self) -> None:
        node = self._ros.node
        for _, entity in self._entities:
            node.destroy_subscription(entity)
        for client in self._clients.values():
            node.destroy_client(client)
        if self._action is not None:
            self._action.destroy()
        self._entities, self._clients, self._action = [], {}, None

    async def close(self) -> None:
        if not self._started:
            return
        self._started = False
        if self._watch is not None:
            self._watch.cancel()
        if self._log_task is not None:
            self._log_task.cancel()
        if self._repairing is not None:
            self._repairing.cancel()
        try:
            await asyncio.wait_for(self._ros.call(self._destroy_entities), 3.0)
        except (TimeoutError, RuntimeError):
            pass
        self._set_online(False)

    # ----------------------------------------------------------------- liveness
    def _check_alive(self) -> None:
        self._watch = self._loop.call_later(0.5, self._check_alive) if self._started else None
        if self.online and time.monotonic() - self._last_status_at > LOST_AFTER_S:
            self._set_online(False)

    def _set_online(self, online: bool) -> None:
        if online == self.online:
            return
        self.online = online
        if not online:
            self._forget_numbers()
        self._emit(lambda listener: listener.on_online(online))
        if online:
            asyncio.ensure_future(self._fetch_catalog())

    async def _fetch_catalog(self) -> None:
        try:
            result = await self.describe("node_types", {})
        except (LinkDown, CommandRefused) as error:
            log.warning("%s: the catalog cannot be had: %s", self.name, error)
            return
        types = result.get("types", [])
        self._emit(lambda listener: listener.on_catalog(list(types)))

    def _check_online(self) -> None:
        if not self.online:
            raise LinkDown(f"the executor {self.name} is not reachable")

    # ----------------------------------------------------------------- topics (on the loop)
    def _on_run_msg(self, msg: RunInfo) -> None:
        run = _run_of(msg)
        if run.run_id != self._run.run_id:
            self._details = {}
            self._details_run = run.run_id
        self._run = run
        self._emit(lambda listener: listener.on_run(run))

    def _forget_numbers(self) -> None:
        """The executor was lost or restarted: its numbering of updates starts anew."""
        self._bb_seq = self._details_seq = None

    def _on_status_msg(self, msg: TreeStatus) -> None:
        self._last_status_at = time.monotonic()
        if self._status is not None and msg.seq < self._status.seq:
            self._forget_numbers()  # a restarted executor numbers its messages from 1 again
        status = _status_of(msg)
        self._status = status
        self._set_online(True)
        self._emit(lambda listener: listener.on_status(status))

    def _closed_run(self, run_id: str | None) -> bool:
        return run_id is not None and run_id in self._finished_runs

    def _on_blackboard_msg(self, msg: BlackboardUpdate) -> None:
        run_id = _hex(msg.run_id)
        expected = self._bb_seq
        if expected is not None and msg.seq <= expected:
            return  # covered by a snapshot already
        self._bb_seq = msg.seq
        if expected is not None and msg.seq != expected + 1 and not msg.full:
            self._repair()
            return
        if self._closed_run(run_id) or run_id is None:
            return
        try:
            update = _decoded_update(loads_json(msg.update_json))
        except ValueError:
            return
        self._emit(lambda listener: listener.on_blackboard(run_id, msg.seq, msg.full, update))

    def _on_details_msg(self, msg: NodeDetails) -> None:
        run_id = _hex(msg.run_id)
        expected = self._details_seq
        if expected is not None and msg.seq <= expected:
            return
        self._details_seq = msg.seq
        if expected is not None and msg.seq != expected + 1:
            self._repair()
            return
        if self._closed_run(run_id) or run_id is None:
            return
        try:
            items = loads_json(msg.details_json)
        except ValueError:
            return
        if run_id != self._details_run:
            self._details, self._details_run = {}, run_id
        for item in items:
            key = (item.get("i"), item.get("kind"))
            if item.get("value") is None:
                self._details.pop(key, None)
            else:
                self._details[key] = item.get("value")
        self._emit(lambda listener: listener.on_details(run_id, msg.seq, items))

    def _repair(self) -> None:
        """Updates were lost: report the state of a snapshot instead."""
        if self._repairing is None or self._repairing.done():
            self._repairing = asyncio.ensure_future(self._repair_now())

    async def _repair_now(self) -> None:
        try:
            snap = await self.snapshot()
        except (LinkDown, CommandRefused, TimeoutError) as error:
            log.warning("%s: a gap in the updates could not be repaired: %s", self.name, error)
            return
        run_id = snap["run"].run_id
        self._bb_seq = max(self._bb_seq or 0, snap["blackboard_seq"])
        self._details_seq = max(self._details_seq or 0, snap["details_seq"])
        if run_id is None or self._closed_run(run_id):
            return
        entries = snap["blackboard"]
        full = {"entries": entries, "removed": [], "order": [entry["id"] for entry in entries]}
        self._emit(lambda listener: listener.on_blackboard(run_id, snap["blackboard_seq"], True, full))
        current = {(item["i"], item["kind"]): item["value"] for item in snap["details"]}
        old = self._details if self._details_run == run_id else {}
        items = [{"i": index, "kind": kind, "value": None} for (index, kind) in old if (index, kind) not in current]
        items += [{"i": index, "kind": kind, "value": value} for (index, kind), value in current.items()
                  if old.get((index, kind)) != value]
        self._details, self._details_run = current, run_id
        if items:
            self._emit(lambda listener: listener.on_details(run_id, snap["details_seq"], items))

    def _on_log_msg(self, msg: LogBatch) -> None:
        if self._log_queue is not None:
            self._log_queue.put_nowait(msg)

    async def _log_worker(self) -> None:
        """Reports log batches in order, filling gaps from ``~/get_log`` first."""
        assert self._log_queue is not None
        while True:
            msg = await self._log_queue.get()
            run_id = _hex(msg.run_id)
            if run_id is None or self._closed_run(run_id):
                continue
            records = [_record_of(msg.seq + offset, item) for offset, item in enumerate(msg.records)]
            expected = self._log_next.get(run_id)
            if expected is None:
                expected = 0 if msg.seq == 0 else msg.seq  # joined late: earlier records come from get_log
            if msg.seq > expected:
                try:
                    missing = await self._fetch_log(run_id, expected, msg.seq)
                except (LinkDown, CommandRefused, TimeoutError) as error:
                    log.warning("%s: log records %d to %d are missing: %s", self.name, expected, msg.seq - 1, error)
                    missing = []
                records = missing + records
            records = [record for record in records if record.seq >= expected]
            if records:
                self._log_next[run_id] = records[-1].seq + 1
                self._emit(lambda listener, run_id=run_id, records=records: listener.on_log(run_id, records))

    async def _fetch_log(self, run_id: str, start: int, end: int) -> list[LinkRecord]:
        records: list[LinkRecord] = []
        while start < end:
            page = await self.get_log(run_id, start, min(_LOG_PAGE, end - start))
            if not page:
                break
            records.extend(page)
            start = page[-1].seq + 1
        return records

    # ----------------------------------------------------------------- calls
    async def _call(self, key: str, request: Any, timeout: float = CALL_TIMEOUT_S) -> Any:
        client = self._clients.get(key)
        if client is None:
            raise LinkDown(f"the link to {self.name} is not started")
        if not client.service_is_ready():
            raise LinkDown(f"the executor {self.name} does not answer ({key})")
        try:
            return await asyncio.wait_for(self._ros.call(lambda: client.call_async(request)), timeout)
        except TimeoutError as error:
            raise LinkDown(f"the executor {self.name} did not answer in time ({key})") from error

    async def describe(self, kind: str, arg: dict) -> dict:
        response = await self._call("describe", Describe.Request(kind=kind, arg_json=dumps_json(arg, indent=None)))
        try:
            result = loads_json(response.result_json) if response.result_json else {}
        except ValueError as error:
            raise CommandRefused(f"the executor's answer cannot be read: {error}") from error
        if not response.ok:
            raise CommandRefused(result.get("reason", "refused") if isinstance(result, dict) else "refused")
        return result

    async def validate(self, bundle: dict, blackboard: list[dict]) -> list[dict]:
        response = await self._call("validate", Validate.Request(bundle_json=dumps_json(bundle, indent=None),
                                                                 blackboard_json=dumps_json(blackboard, indent=None)))
        try:
            return list(loads_json(response.findings_json))
        except ValueError:
            return [{"path": "", "severity": "error", "text": "the executor's findings cannot be read"}]

    async def execute(self, bundle: dict, blackboard: list[dict], label: str, requested_by: str) -> str:
        self._check_online()
        action = self._action
        if action is None or not action.server_is_ready():
            raise LinkDown(f"the executor {self.name} does not offer execute")
        goal = ExecuteTree.Goal(bundle_json=dumps_json(bundle, indent=None),
                                blackboard_json=dumps_json(blackboard, indent=None),
                                label=label, requested_by=requested_by)
        try:
            handle = await asyncio.wait_for(self._ros.call(lambda: action.send_goal_async(goal)), CALL_TIMEOUT_S)
        except TimeoutError as error:
            raise LinkDown(f"the executor {self.name} did not answer the goal") from error
        if not handle.accepted:
            raise GoalRejected(await self._rejection_reason(bundle, blackboard))
        run_id = bytes(bytearray(handle.goal_id.uuid)).hex()
        result_future = self._ros.call(handle.get_result_async)
        result_future.add_done_callback(lambda done: self._on_result(run_id, done))
        return run_id

    async def _rejection_reason(self, bundle: dict, blackboard: list[dict]) -> str:
        status, run = self._status, self._run
        if status is not None and status.state != 0:
            who = f"'{run.label}' started by {run.requested_by or 'someone'}" if run.run_id else "a run"
            return f"the executor is busy: {who} is going"
        try:
            findings = await self.validate(bundle, blackboard)
        except LinkDown:
            findings = []
        unreadable = [item["text"] for item in findings if item.get("path") == "" and item.get("severity") == "error"]
        if unreadable:
            return f"the bundle cannot be read: {unreadable[0]}"
        return "the executor rejected the tree (it may have become busy)"

    def _on_result(self, run_id: str, future: asyncio.Future) -> None:
        if future.cancelled() or future.exception() is not None:
            log.warning("%s: the result of run %s did not arrive", self.name, run_id)
            return
        response = future.result()
        result = response.result
        try:
            entries = [_decoded_entry(entry) for entry in loads_json(result.blackboard_json or "[]")]
        except ValueError:
            entries = []
        link_result = LinkResult(run_id, result.outcome, result.message, result.loops_completed,
                                 result.unconfirmed_cancels, entries, _GOAL_STATUS.get(response.status, "aborted"))
        self._finished_runs.append(run_id)
        self._emit(lambda listener: listener.on_result(link_result))

    async def stop(self, run_id: str) -> None:
        request = CancelGoal.Request()
        request.goal_info.goal_id = _uuid(run_id)
        await self._call("cancel", request)

    async def pause(self, paused: bool) -> None:
        response = await self._call("pause", SetBool.Request(data=paused))
        if not response.success:
            raise CommandRefused(response.message or "refused")

    async def reset(self) -> None:
        response = await self._call("reset", Trigger.Request())
        if not response.success:
            raise CommandRefused(response.message or "refused")

    async def apply(self, ops: list[dict]) -> tuple[int, str]:
        response = await self._call("apply", Apply.Request(ops_json=dumps_json(_encoded_ops(ops), indent=None)))
        return int(response.result), response.message

    async def snapshot(self) -> dict:
        response = await self._call("snapshot", GetSnapshot.Request())
        try:
            entries = [_decoded_entry(entry) for entry in loads_json(response.blackboard_json or "[]")]
            details = list(loads_json(response.details_json or "[]"))
        except ValueError as error:
            raise CommandRefused(f"the snapshot cannot be read: {error}") from error
        return {
            "run": _run_of(response.run),
            "status": _status_of(response.status),
            "blackboard_seq": response.blackboard_seq,
            "blackboard": entries,
            "details_seq": response.details_seq,
            "details": details,
            "log_seq": response.log_seq,
        }

    async def get_log(self, run_id: str | None, from_seq: int, max_records: int) -> list[LinkRecord]:
        response = await self._call("get_log", GetLog.Request(run_id=_uuid(run_id), from_seq=max(0, from_seq),
                                                              max_records=max(0, max_records)))
        return [_record_of(from_seq + offset, item) for offset, item in enumerate(response.records)]


# ---------------------------------------------------------------------------------------------
# The factory


class RosLinkFactory:
    """Makes the RosLinks of one gateway and finds executors on the ROS network (contract 6)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._ros: _RosThread | None = None
        self._links: dict[str, RosLink] = {}
        self._found: list[str] = []
        self._on_executors: Callable[[list[str]], None] | None = None
        self._timer: Any = None

    async def start(self, on_executors: Callable[[list[str]], None]) -> None:
        self._ros = _RosThread(asyncio.get_running_loop())
        self._on_executors = on_executors
        await self._ros.call(self._start_discovery)

    def _start_discovery(self) -> None:
        assert self._ros is not None
        self._discover()
        self._timer = self._ros.node.create_timer(DISCOVERY_PERIOD_S, self._discover)

    def _discover(self) -> None:
        """On the ROS thread: the executors on the network now."""
        assert self._ros is not None
        names = sorted(name[: -len("/execute")] for name, types in self._ros.node.get_action_names_and_types()
                       if ACTION_TYPE in types and name.endswith("/execute"))
        if names != self._found:
            self._found = names
            self._ros.loop.call_soon_threadsafe(self._report, list(names))

    def _report(self, names: list[str]) -> None:
        if self._on_executors is not None:
            self._on_executors(names)

    def link(self, name: str) -> RosLink:
        if self._ros is None:
            raise RuntimeError("RosLinkFactory.start() was not called")
        key = "/" + name.strip("/")
        if key not in self._links:
            self._links[key] = RosLink(key, self._ros)
        return self._links[key]

    async def close(self) -> None:
        for link in self._links.values():
            await link.close()
        if self._ros is not None:
            ros, self._ros = self._ros, None
            await asyncio.to_thread(ros.close)
