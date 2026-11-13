"""``LocalLink``: the engine inside the gateway process (design 3.3), for development, tests and
use without ROS.

It drives a :class:`bt_server_core.engine.TreeRunner` on an
:class:`~bt_server_core.scheduler.AsyncioScheduler` on the gateway's loop and behaves as the tree
API of design 4 does: the same states and refusals, ``NO_RUN`` answers from :meth:`apply` when
no run is active, results, numbered blackboard, details and log updates, and the rates of 4.1
and 8.1: statuses on change at most 30 times a second and at least every 0.5 s while a run is
going; blackboard changes and details at most 10 batches a second; log records in batches at
the same rate. Each run writes its log file under ``local_log_dir/<executor>`` with
:mod:`bt_server_core.treelog`.

Its catalog is the registry's: the built-in types without the ROS ones, plus the custom leaves
of ``leaf_modules``. ROS questions (``interface``, ``sample``) are refused; ``graph`` is empty.

Listeners are always called from a callback of their own on the loop, never from inside the
engine, so a listener may call back into the link.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from bt_server_core.blackboard import Blackboard, OpError
from bt_server_core.bundle import validate_bundle
from bt_server_core.engine import BundleError, Outcome, RunResult, State, TreeRunner
from bt_server_core.engine import CommandRefused as EngineRefused
from bt_server_core.registry import Registry, RegistryError
from bt_server_core.scheduler import AsyncioScheduler
from bt_server_core.treelog import TreeLogWriter, read_records, sanitize_label
from bt_server_core.values import decode_value, encode_value

from .link import (
    APPLIED,
    NO_RUN,
    REJECTED,
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

__all__ = ["LOCAL_NAME", "LocalLink", "LocalLinkFactory"]

log = logging.getLogger("bt_server_gateway")

#: The name of the executor LocalLinkFactory always offers.
LOCAL_NAME = "local"
STATUS_MIN_GAP = 1.0 / 30.0
HEARTBEAT = 0.5
BATCH_GAP = 0.1


def decoded_entry(entry: dict) -> dict:
    """An entry dict of ``Blackboard`` (file encoding) with its value decoded; an Object
    entry's value (its repr text) is kept as it is."""
    result = dict(entry)
    if entry.get("type") != "Object":
        try:
            result["value"] = decode_value(entry.get("value"))
        except ValueError:
            pass
    return result


class _Throttle:
    """Calls ``fn`` on the loop, at most once per ``gap`` seconds; requests in between merge."""

    def __init__(self, loop: asyncio.AbstractEventLoop, gap: float, fn: Callable[[], None]) -> None:
        self._loop, self._gap, self._fn = loop, gap, fn
        self._last = float("-inf")
        self._handle: asyncio.Handle | asyncio.TimerHandle | None = None

    def request(self) -> None:
        if self._handle is not None:
            return
        wait = self._last + self._gap - time.monotonic()
        self._handle = self._loop.call_soon(self._run) if wait <= 0 else self._loop.call_later(wait, self._run)

    def run_now(self) -> None:
        """Run at once (pending requests included)."""
        self.cancel()
        self._run()

    def cancel(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None

    @property
    def last(self) -> float:
        return self._last

    def _run(self) -> None:
        self._handle = None
        self._last = time.monotonic()
        self._fn()


class LocalLink:
    """One in-process executor (see the module docs)."""

    def __init__(self, name: str, settings: Settings) -> None:
        self.name = name
        self.online = False
        self._settings = settings
        self._listeners: list[RunListener] = []
        self._registry = Registry()
        self._catalog: list[dict] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._scheduler: AsyncioScheduler | None = None
        self._runner: TreeRunner | None = None
        self._finished = True
        self._run = LinkRun(None)
        self._status_seq = 0
        self._bb_seq = 0
        self._details_seq = 0
        self._outcome = 0
        self._message = ""
        self._writer: TreeLogWriter | None = None
        self._log_paths: dict[str, str] = {}
        self._pending_entries: dict[str, dict] = {}
        self._pending_removed: list[str] = []
        self._pending_order: list[str] | None = None
        self._pending_details: dict[tuple[int, str], dict] = {}
        self._pending_log: list[LinkRecord] = []
        self._status_throttle: _Throttle | None = None
        self._batch_throttle: _Throttle | None = None
        self._heartbeat: asyncio.TimerHandle | None = None
        folder = sanitize_label(name.strip("/").replace("/", "_")) or LOCAL_NAME
        self.log_dir = Path(settings.local_log_dir or settings.trees_dir / ".logs") / folder

    # ----------------------------------------------------------------- lifecycle
    def subscribe(self, listener: RunListener) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._scheduler = AsyncioScheduler(self._loop)
        self._status_throttle = _Throttle(self._loop, STATUS_MIN_GAP, self._publish_status)
        self._batch_throttle = _Throttle(self._loop, BATCH_GAP, self._publish_batches)
        try:
            self._registry = Registry(self._settings.leaf_modules)
        except RegistryError as error:
            log.error("%s: the leaf modules cannot be loaded (%s); only the built-in node types are offered",
                      self.name, error)
            self._registry = Registry()
        self._registry.run_setup()
        self._catalog = self._registry.catalog()
        self.online = True
        self._emit(lambda listener: listener.on_online(True))
        self._emit(lambda listener: listener.on_catalog(list(self._catalog)))
        self._emit(lambda listener: listener.on_run(self._run))
        self._status_throttle.request()

    async def close(self) -> None:
        runner = self._runner
        if runner is not None and runner.state != State.IDLE:
            try:
                runner.stop()
            except EngineRefused:
                pass
        for throttle in (self._status_throttle, self._batch_throttle):
            if throttle is not None:
                throttle.cancel()
        if self._heartbeat is not None:
            self._heartbeat.cancel()
        if self._writer is not None:
            self._writer.close()
        self.online = False

    # ----------------------------------------------------------------- questions
    def _check_online(self) -> None:
        if not self.online:
            raise LinkDown(f"the executor {self.name} is not running")

    async def describe(self, kind: str, arg: dict) -> dict:
        self._check_online()
        if kind == "node_types":
            return {"types": list(self._catalog)}
        if kind == "graph":
            return {"actions": [], "services": [], "topics": []}
        if kind in ("interface", "sample"):
            raise CommandRefused("this executor runs inside the gateway without ROS: there is no ROS network to ask")
        raise CommandRefused(f"unknown question {kind!r}")

    async def validate(self, bundle: dict, blackboard: list[dict]) -> list[dict]:
        self._check_online()
        try:
            return validate_bundle(bundle, blackboard, self._catalog, object_names=self._registry.objects)
        except (KeyError, TypeError, AttributeError, ValueError) as error:
            return [{"path": "", "severity": "error", "text": f"the bundle cannot be read: {error}"}]

    # ----------------------------------------------------------------- runs
    def _active(self) -> bool:
        return self._runner is not None and not self._finished

    async def execute(self, bundle: dict, blackboard: list[dict], label: str, requested_by: str) -> str:
        self._check_online()
        if self._active():
            raise GoalRejected(f"the executor is busy: a run started by {self._run.requested_by or 'someone'} is going")
        assert self._scheduler is not None and self._loop is not None
        run_id = uuid.uuid4().hex
        board = Blackboard(blackboard)
        try:
            runner = TreeRunner(bundle, board, self._registry, self._scheduler, _Sink(self), self._on_finished,
                                run_context={"executor": self.name, "run_id": run_id})
        except BundleError as error:
            raise GoalRejected(f"the bundle cannot be read: {error}") from error
        if self._writer is not None:
            self._writer.close()
        self._flush_batches()
        self._runner, self._finished = runner, False
        self._run = LinkRun(run_id, label, requested_by, bundle, list(runner.node_paths))
        self._outcome, self._message = 0, ""
        self._writer = TreeLogWriter(str(self.log_dir), label, keep_days=self._settings.local_log_keep_days,
                                     on_error=lambda text: log.warning("%s: %s", self.name, text))
        self._log_paths[run_id] = self._writer.path
        run = self._run
        entries = [decoded_entry(board.entry(entry_id)) for entry_id in board.ids()]
        self._bb_seq += 1
        full = {"entries": entries, "removed": [], "order": [entry["id"] for entry in entries]}
        seq = self._bb_seq
        self._emit(lambda listener: listener.on_run(run))
        self._emit(lambda listener: listener.on_blackboard(run_id, seq, True, full))
        self._loop.call_soon(self._start_runner, runner)
        return run_id

    def _start_runner(self, runner: TreeRunner) -> None:
        if runner is not self._runner or self._finished:
            return
        runner.start()
        self._arm_heartbeat()

    def _on_finished(self, result: RunResult) -> None:
        """The run ended (engine thread = the loop): publish what is pending, then the result."""
        self._finished = True
        outcome = int(result.outcome)
        self._outcome, self._message = outcome, result.message
        if outcome == Outcome.SUCCEEDED:
            goal_status = "succeeded"
        elif outcome == Outcome.STOPPED and result.stopped_by_command:
            goal_status = "canceled"
        else:
            goal_status = "aborted"
        link_result = LinkResult(self._run.run_id or "", outcome, result.message, result.loops_completed,
                                 result.unconfirmed_cancels, [decoded_entry(item) for item in result.blackboard],
                                 goal_status)
        assert self._loop is not None
        self._loop.call_soon(self._finish, link_result)

    def _finish(self, result: LinkResult) -> None:
        self._flush_batches()
        if self._status_throttle is not None:
            self._status_throttle.run_now()
        self._emit_now(lambda listener: listener.on_result(result))

    async def stop(self, run_id: str) -> None:
        self._check_online()
        if self._runner is not None and self._run.run_id == run_id and not self._finished:
            self._runner.stop()

    async def pause(self, paused: bool) -> None:
        self._check_online()
        if self._runner is None:
            raise CommandRefused(f"{'Pause' if paused else 'Resume'} is refused while IDLE")
        try:
            self._runner.pause() if paused else self._runner.resume()
        except EngineRefused as error:
            raise CommandRefused(str(error)) from error

    async def reset(self) -> None:
        self._check_online()
        if self._runner is None:
            return
        try:
            self._runner.reset()
        except EngineRefused as error:
            raise CommandRefused(str(error)) from error

    async def apply(self, ops: list[dict]) -> tuple[int, str]:
        self._check_online()
        runner = self._runner
        if runner is None or not self._active():
            return NO_RUN, "no run is active"
        try:
            runner.apply(ops)
        except (OpError, ValueError) as error:
            return REJECTED, str(error)
        for op in ops:
            if op.get("op") == "node.set_field":
                self._report_field(runner, op)
        return APPLIED, ""

    @staticmethod
    def _report_field(runner: TreeRunner, op: dict) -> None:
        """A field set through ``apply`` goes back as a ``field`` detail of each instance."""
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

    async def snapshot(self) -> dict:
        self._check_online()
        runner = self._runner
        entries: list[dict] = []
        details: list[dict] = []
        if runner is not None:
            entries = [decoded_entry(runner.blackboard.entry(entry_id)) for entry_id in runner.blackboard.ids()]
            details = [{"i": index, "kind": kind, "value": value}
                       for index, kinds in runner.details().items() for kind, value in kinds.items()]
        return {
            "run": self._run,
            "status": self._status(),
            "blackboard_seq": self._bb_seq,
            "blackboard": entries,
            "details_seq": self._details_seq,
            "details": details,
            "log_seq": (self._writer.next_seq - 1) if self._writer is not None else -1,
        }

    async def get_log(self, run_id: str | None, from_seq: int, max_records: int) -> list[LinkRecord]:
        self._check_online()
        path = self._log_paths.get(run_id or self._run.run_id or "")
        if path is None:
            return []
        if self._writer is not None and self._writer.path == path:
            await asyncio.to_thread(self._writer.flush)
        records = await asyncio.to_thread(read_records, path, from_seq, max_records)
        return [LinkRecord(record.seq, record.stamp, record.level, record.index, record.text) for record in records]

    def log_path(self, run_id: str) -> str | None:
        """The log file of a run of this executor (for downloads)."""
        return self._log_paths.get(run_id)

    # ----------------------------------------------------------------- reporting
    def _status(self) -> LinkStatus:
        runner = self._runner
        if runner is None:
            return LinkStatus(self._run.run_id, self._status_seq, 0, 0, 0, 0, self._outcome, self._message, b"")
        return LinkStatus(self._run.run_id, self._status_seq, int(runner.state), runner.loop_count, runner.cancelling,
                          runner.unsettled, self._outcome if self._finished else 0,
                          self._message if self._finished else "", bytes(runner.statuses))

    def _publish_status(self) -> None:
        self._status_seq += 1
        status = self._status()
        self._emit_now(lambda listener: listener.on_status(status))

    def _arm_heartbeat(self) -> None:
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            self._heartbeat = None
        runner = self._runner
        if runner is None or self._finished or self._loop is None:
            return
        self._heartbeat = self._loop.call_later(HEARTBEAT, self._beat)

    def _beat(self) -> None:
        self._heartbeat = None
        throttle = self._status_throttle
        if throttle is not None and time.monotonic() - throttle.last >= HEARTBEAT - 0.02:
            throttle.request()
        self._arm_heartbeat()

    def status_changed(self) -> None:
        if self._status_throttle is not None:
            self._status_throttle.request()

    def blackboard_changed(self, changes: dict) -> None:
        for entry_id in changes.get("removed", ()):
            self._pending_entries.pop(entry_id, None)
            self._pending_removed.append(entry_id)
        for entry in changes.get("entries", ()):
            self._pending_entries[entry["id"]] = decoded_entry(entry)
        if changes.get("order") is not None:
            self._pending_order = list(changes["order"])
        self._request_batch()

    def detail(self, index: int, kind: str, value: object) -> None:
        self._pending_details[(index, kind)] = {"i": index, "kind": kind, "value": value, "stamp": time.time()}
        self._request_batch()

    def record(self, level: int, text: str, index: int) -> None:
        if self._writer is None:
            return
        record = self._writer.write(level, text, index=index)
        if record is not None:
            self._pending_log.append(LinkRecord(record.seq, record.stamp, record.level, index, record.text))
            self._request_batch()

    def _request_batch(self) -> None:
        if self._batch_throttle is not None:
            self._batch_throttle.request()

    def _flush_batches(self) -> None:
        if self._batch_throttle is not None:
            self._batch_throttle.cancel()
        self._publish_batches()

    def _publish_batches(self) -> None:
        run_id = self._run.run_id
        if run_id is None:
            return
        if self._pending_entries or self._pending_removed or self._pending_order is not None:
            update = {"entries": list(self._pending_entries.values()), "removed": self._pending_removed,
                      "order": self._pending_order}
            self._pending_entries, self._pending_removed, self._pending_order = {}, [], None
            self._bb_seq += 1
            seq = self._bb_seq
            self._emit_now(lambda listener: listener.on_blackboard(run_id, seq, False, update))
        if self._pending_details:
            details = list(self._pending_details.values())
            self._pending_details = {}
            self._details_seq += 1
            seq = self._details_seq
            self._emit_now(lambda listener: listener.on_details(run_id, seq, details))
        if self._pending_log:
            records, self._pending_log = self._pending_log, []
            self._emit_now(lambda listener: listener.on_log(run_id, records))

    def _emit(self, call: Callable[[RunListener], None]) -> None:
        """Call every listener from a loop callback of its own."""
        assert self._loop is not None
        self._loop.call_soon(self._emit_now, call)

    def _emit_now(self, call: Callable[[RunListener], None]) -> None:
        for listener in list(self._listeners):
            try:
                call(listener)
            except Exception:  # noqa: BLE001 - one listener's error must not stop the others
                log.exception("%s: a run listener failed", self.name)


class _Sink:
    """The engine's :class:`~bt_server_core.engine.Sink` for a LocalLink (engine thread = loop)."""

    def __init__(self, link: LocalLink) -> None:
        self._link = link

    def state(self, state: State, loop: int) -> None:
        self._link.status_changed()
        self._link._arm_heartbeat()

    def statuses(self, changed) -> None:  # noqa: ANN001 - Iterable[int]
        self._link.status_changed()

    def detail(self, index: int, kind: str, value: object) -> None:
        self._link.detail(index, kind, value)

    def blackboard(self, changes: dict) -> None:
        self._link.blackboard_changed(changes)

    def log(self, level: int, text: str, index: int = -1) -> None:
        self._link.record(level, text, index)


class LocalLinkFactory:
    """Offers the in-process executor ``local``, and one more LocalLink for each name in
    ``executors`` (each with its own engine, blackboard and log folder)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._links: dict[str, LocalLink] = {}

    async def start(self, on_executors: Callable[[list[str]], None]) -> None:
        on_executors([LOCAL_NAME, *[name for name in self._settings.executors if name != LOCAL_NAME]])

    def link(self, name: str) -> LocalLink:
        if name not in self._links:
            self._links[name] = LocalLink(name, self._settings)
        return self._links[name]

    async def close(self) -> None:
        for link in self._links.values():
            if link.online:
                await link.close()
