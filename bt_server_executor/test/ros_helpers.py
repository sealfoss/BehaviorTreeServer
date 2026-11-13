"""Helpers for the tests of the ROS leaves, sources, types and Remote Sub-Tree (they need ROS 2
and the built ``bt_server_interfaces``; the domain is the environment's ``ROS_DOMAIN_ID``).

* :class:`Harness`: an executor in the test process. One rclpy context, the executor's node and
  a ``scripted`` node for the test's own servers and publishers, both spun by one executor on
  the test's thread, which is the engine thread: :meth:`Harness.spin_until` and
  :meth:`Harness.spin_for` drive everything. :meth:`Harness.run` starts a ``TreeRunner`` with the
  rclpy scheduler, a ``RunContext``, the goal journal, the client cache and the sources, as the
  executor node does.
* :class:`RecordingSink`: what the engine reports, with times since the run started.
* :class:`ScriptedAction` / :class:`ScriptedService`: ``example_interfaces/action/Fibonacci`` and
  ``std_srvs/srv/Trigger`` servers whose replies, feedback, results and cancels the test sets.
* :class:`ExecutorProc`: an executor process (``python -m bt_server_executor``).
* :func:`bundle`, :func:`leaf`: bundles of one tree whose Root runs the given leaves.
"""

from __future__ import annotations

import itertools
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import rclpy
from example_interfaces.action import Fibonacci
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.task import Future
from std_srvs.srv import Trigger

from bt_server_core.blackboard import Blackboard
from bt_server_core.engine import RunResult, State, Status, TreeRunner
from bt_server_core.registry import Registry
from bt_server_core.values import dumps_json
from bt_server_executor import ros_leaves
from bt_server_executor.journal import GoalJournal
from bt_server_executor.rclpy_scheduler import RclpyScheduler
from bt_server_executor.run_context import ClientCache, RunContext
from bt_server_executor.sources import SourceManager

TEST_DIR = Path(__file__).resolve().parent
REPO = TEST_DIR.parents[1]
_counter = itertools.count(1)

LETTERS = {Status.READY: "-", Status.RUNNING: "R", Status.SUCCEEDED: "S", Status.FAILED: "F",
           Status.CANCELLING: "C"}


def unique_ns(tag: str) -> str:
    return f"/{tag}_{os.getpid()}_{next(_counter)}"


def new_id() -> str:
    return uuid.uuid4().hex


# ============================================================================== bundles
def leaf(type_name: str, title: str | None = None, *, bindings: dict | None = None, outputs: dict | None = None,
         **fields: Any) -> dict:
    """A leaf node; field names with spaces are given as ``Feedback_Timeout`` and so on."""
    node: dict[str, Any] = {"id": new_id(), "type": type_name, "title": title or type_name, "x": 0.0, "y": 0.0,
                            "fields": {name.replace("_", " "): value for name, value in fields.items()}}
    if bindings:
        node["bindings"] = bindings
    if outputs:
        node["outputs"] = outputs
    return node


def tree(*children: dict, composite: str = "Sequence", config: dict | None = None) -> dict:
    """A tree file: Root (``composite``) with ``children`` connected in order. A child given as
    ``(node, [grandchildren])`` gets children of its own."""
    root = {"id": new_id(), "type": "Root", "title": "Root", "composite": composite, "memory": True,
            "x": 0.0, "y": -100.0}
    nodes, connections = [root], []

    def add(parent: dict, items: Iterable[Any]) -> None:
        for item in items:
            node, below = item if isinstance(item, tuple) else (item, ())
            nodes.append(node)
            connections.append({"parent": parent["id"], "child": node["id"]})
            add(node, below)

    add(root, children)
    return {"config": config or {}, "nodes": nodes, "connections": connections}


def bundle(*children: dict, config: dict | None = None, key: str = "main", composite: str = "Sequence",
           extra: dict | None = None, blackboards: dict | None = None) -> dict:
    """A bundle of one tree (plus ``extra`` trees)."""
    result: dict[str, Any] = {"format": "behavior_tree_bundle", "version": 1, "root": key,
                              "trees": {key: tree(*children, composite=composite, config=config), **(extra or {})}}
    if blackboards:
        result["blackboards"] = blackboards
    return result


def guard(name: str = "go", title: str = "c") -> dict:
    """``c`` of Appendix B: an Evaluation of ``<name> == true``, Wait off."""
    return leaf("Evaluation", title, ValueName=name, CompareTo=None, LiteralValue=True, Operator="==")


def evaluation(name: str, operator: str, literal: Any, *, wait: bool = False, title: str = "W") -> dict:
    """An Evaluation of ``<name> <operator> <literal>`` (``W`` of Appendix B with ``wait``)."""
    return leaf("Evaluation", title, ValueName=name, CompareTo=None, LiteralValue=literal, Operator=operator,
                Wait=wait)


def sequence(*children: dict, memory: bool = True) -> tuple[dict, list[dict]]:
    """A Sequence node with ``children`` (``Seq~`` with ``memory=False``), for :func:`tree`."""
    node = {"id": new_id(), "type": "Sequence", "title": "Sequence", "memory": memory, "x": 0.0, "y": 0.0}
    return node, list(children)


def entry(name: str, type_name: str, value: Any, source: dict | None = None) -> dict:
    item: dict[str, Any] = {"name": name, "type": type_name, "value": value}
    if source is not None:
        item["source"] = source
    return item


# ============================================================================== the in-process executor
class RecordingSink:
    """The engine's reports, each with its time since the run started (``Harness.run``)."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self.runner: TreeRunner | None = None
        self.timeline: list[tuple[float, str]] = []  # (time, status letters)
        self.details: list[tuple[float, int, str, Any]] = []
        self.logs: list[tuple[float, int, str, int]] = []
        self.states: list[tuple[float, State, int]] = []
        self.changes: list[tuple[float, dict]] = []

    def letters(self) -> str:
        assert self.runner is not None
        return " ".join(LETTERS[Status(value)] for value in self.runner.statuses)

    def state(self, state: State, loop: int) -> None:
        self.states.append((self._clock(), state, loop))

    def statuses(self, changed: Iterable[int]) -> None:
        self.timeline.append((self._clock(), self.letters()))

    def detail(self, index: int, kind: str, value: object) -> None:
        self.details.append((self._clock(), index, kind, value))

    def blackboard(self, changes: dict) -> None:
        self.changes.append((self._clock(), changes))

    def log(self, level: int, text: str, index: int = -1) -> None:
        self.logs.append((self._clock(), level, text, index))

    # ----------------------------------------------------------------- questions of the tests
    def first(self, letters_at: Callable[[str], bool]) -> float | None:
        """The first time the statuses matched."""
        return next((at for at, letters in self.timeline if letters_at(letters)), None)

    def status_times(self, index: int, letter: str) -> list[float]:
        """The times at which node ``index`` began to show ``letter``."""
        result, before = [], None
        for at, letters in self.timeline:
            current = letters.split()[index]
            if current == letter and before != letter:
                result.append(at)
            before = current
        return result

    def detail_values(self, index: int, kind: str) -> list[tuple[float, Any]]:
        return [(at, value) for at, i, k, value in self.details if i == index and k == kind]

    def error_of(self, index: int) -> Any:
        values = self.detail_values(index, "error")
        return values[-1][1] if values else None

    def log_texts(self, level: int | None = None) -> list[str]:
        return [text for _, lvl, text, _ in self.logs if level is None or lvl == level]


class Run:
    """One run started by :meth:`Harness.run`."""

    def __init__(self, runner: TreeRunner, sink: RecordingSink, blackboard: Blackboard, context: RunContext) -> None:
        self.runner = runner
        self.sink = sink
        self.blackboard = blackboard
        self.context = context
        self.result: RunResult | None = None
        self.ended_at: float | None = None
        self.tick_requests: list[float] = []  # times of request_tick calls (from sources, the stale timer)

    @property
    def done(self) -> bool:
        return self.result is not None


class Harness:
    """An executor in the test process (see the module docs)."""

    def __init__(self, tmp_path: Path, tag: str = "t") -> None:
        self.context = rclpy.Context()
        rclpy.init(context=self.context)
        self.ns = unique_ns(tag)
        self.node = Node("bt_executor", namespace=self.ns, context=self.context)
        self.peer = Node("scripted", namespace=self.ns, context=self.context)
        try:
            from rclpy.experimental import EventsExecutor
            self.executor: Any = EventsExecutor(context=self.context)
        except Exception:  # noqa: BLE001 - the fallback the node also uses
            self.executor = SingleThreadedExecutor(context=self.context)
        self.executor.add_node(self.node)
        self.executor.add_node(self.peer)
        self.scheduler = RclpyScheduler(self.node, self.executor)
        self.logs: list[tuple[float, int, str]] = []
        self.journal = GoalJournal(str(tmp_path / "goals.jsonl"), self.node, self.scheduler, self.log)
        self.journal.start()
        self.clients = ClientCache(self.node)
        self.registry = Registry()
        ros_leaves.register(self.registry)
        self.sources = SourceManager(self.node, self.scheduler, self.log)
        self.full_name = self.node.get_fully_qualified_name()
        self.t0 = time.monotonic()
        self.runs: list[Run] = []
        self._closed: list[Any] = []

    def now(self) -> float:
        """Seconds since the latest run started."""
        return time.monotonic() - self.t0

    def log(self, level: int, text: str) -> None:
        self.logs.append((self.now(), int(level), str(text)))

    # ----------------------------------------------------------------- spinning
    def spin_once(self, timeout: float = 0.01) -> None:
        self.executor.spin_once(timeout_sec=timeout)

    def spin_until(self, predicate: Callable[[], Any], timeout: float = 10.0) -> Any:
        """Spin until ``predicate()`` is truthy (returned) or ``timeout`` seconds have passed."""
        deadline = time.monotonic() + timeout
        while True:
            value = predicate()
            if value or time.monotonic() >= deadline:
                return value
            self.spin_once(min(0.01, max(0.0, deadline - time.monotonic())))

    def spin_for(self, seconds: float) -> None:
        self.spin_until(lambda: False, seconds)

    def spin_to(self, at: float) -> None:
        """Spin until ``at`` seconds after the latest run started."""
        self.spin_for(max(0.0, at - self.now()))

    def ready_action(self, name: str, action_type: Any = Fibonacci) -> Any:
        """The cached action client of ``name``, once it sees its server (as after an earlier run)."""
        client = self.clients.action_client(action_type, name)
        self.wait_for_graph(client.server_is_ready)
        ros_leaves._matched(client, True)  # noqa: SLF001 - as a client of an earlier run
        self.spin_for(ros_leaves.FIRST_MATCH_GRACE_S)
        return client

    def confirm_action(self, name: str, action_type: Any = Fibonacci, timeout: float = 15.0) -> Any:
        """The cached client of a server in another process, once that server has matched this
        client too: a probe goal is answered (and cancelled). Under load a server drops its first
        replies to a client it has not matched yet."""
        client = self.ready_action(name, action_type)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            future = client.send_goal_async(action_type.Goal())
            if self.spin_until(future.done, 2.0):
                handle = future.result()
                if handle.accepted:
                    self.spin_until(handle.cancel_goal_async().done, 2.0)
                return client
        raise TimeoutError(f"{name} never answered a goal")

    def ready_service(self, name: str, srv_type: Any = Trigger) -> Any:
        """The cached service client of ``name``, once it sees its service (as after an earlier run)."""
        client = self.clients.service_client(srv_type, name)
        self.wait_for_graph(client.service_is_ready)
        ros_leaves._matched(client, True)  # noqa: SLF001 - as a client of an earlier run
        self.spin_for(ros_leaves.FIRST_MATCH_GRACE_S)
        return client

    def wait_for_graph(self, predicate: Callable[[], Any], timeout: float = 5.0) -> None:
        if not self.spin_until(predicate, timeout):
            raise TimeoutError("the ROS graph did not show what the test needs")

    # ----------------------------------------------------------------- runs
    def run(self, the_bundle: dict, entries: Iterable[dict] = (), *, cancel_timeout_s: float | None = None) -> Run:
        """Start a run as the executor node does (prepare, sources, first tick)."""
        context = RunContext(node=self.node, executor_name=self.full_name, status_topic=f"{self.full_name}/status",
                             journal=self.journal, clients=self.clients, scheduler=self.scheduler, log=self.log)
        context.bundle = the_bundle  # what the Remote Sub-Tree leaf reads (see the report)
        context.set_run_id(uuid.uuid4().bytes)
        sink = RecordingSink(self.now)
        board = Blackboard(list(entries))
        holder: list[Run] = []
        runner = TreeRunner(the_bundle, board, self.registry, self.scheduler, sink,
                            lambda result: self._finished(holder[0], result), run_context=context)
        context.cancel_timeout_s = runner.config["cancel_timeout_ms"] / 1000.0
        sink.runner = runner
        run = Run(runner, sink, board, context)
        holder.append(run)
        self.runs.append(run)
        request_tick = runner.request_tick

        def recorded_request_tick() -> None:
            run.tick_requests.append(self.now())
            request_tick()

        runner.request_tick = recorded_request_tick  # type: ignore[method-assign]
        for index in range(len(runner.node_paths)):
            node = runner.node(index)
            prepare = getattr(getattr(node, "leaf", None), "prepare", None)
            if callable(prepare):
                prepare(node.ctx)
        self.t0 = time.monotonic()
        self.sources.start(board, runner.request_tick)
        runner.start()
        return run

    @staticmethod
    def set_entry(run: Run, name: str, value: Any) -> None:
        """Change an entry from outside the tree, as a browser does."""
        run.runner.apply([{"op": "bb.set_value", "id": run.blackboard.id_of(name), "value": value}])

    def _finished(self, run: Run, result: RunResult) -> None:
        run.result = result
        run.ended_at = self.now()
        self.sources.stop()
        self.journal.run_ended(run.context.run_id)
        self.clients.run_ended()

    def finish(self, run: Run, timeout: float = 15.0) -> RunResult:
        if not self.spin_until(lambda: run.done, timeout):
            raise TimeoutError("the run did not end in time")
        assert run.result is not None
        return run.result

    def stop(self, run: Run, timeout: float = 15.0) -> RunResult:
        run.runner.stop()
        return self.finish(run, timeout)

    # ----------------------------------------------------------------- scripted peers
    def sleep(self, seconds: float) -> Future:
        """A future that the scripted node completes after ``seconds`` (for async callbacks)."""
        future: Future = Future()
        holder: list[Any] = []

        def done() -> None:
            holder[0].cancel()
            self.peer.destroy_timer(holder[0])
            if not future.done():
                future.set_result(None)

        holder.append(self.peer.create_timer(max(seconds, 0.001), done))
        return future

    def keep(self, thing: Any) -> Any:
        """Destroy ``thing`` (a server, publisher, ...) when the harness closes."""
        self._closed.append(thing)
        return thing

    def close(self) -> None:
        for run in self.runs:
            if not run.done:
                run.runner.stop()
                self.spin_until(lambda run=run: run.done, 8.0)
        self.sources.stop()
        for thing in reversed(self._closed):
            try:
                closer = getattr(thing, "close", None) or getattr(thing, "destroy", None)
                closer()
            except Exception:  # noqa: BLE001 - closing what the test made
                pass
        self.journal.close()
        self.clients.close()
        self.scheduler.destroy()
        self.executor.shutdown(timeout_sec=1.0)
        self.node.destroy_node()
        self.peer.destroy_node()
        rclpy.shutdown(context=self.context)


# ============================================================================== scripted servers
class ScriptedAction:
    """A Fibonacci action server on the harness's scripted node.

    reply_after: seconds before the goal reply (None: never). accept: accept or reject.
    feedback_at: times (from the goal's acceptance) of feedback messages.
    finish_after: seconds to the end (None: never); outcome ``succeed`` or ``abort``.
    cancel: ``accept`` or ``reject``; cancel_delay: seconds from the cancel to the canceled result.
    """

    def __init__(self, harness: Harness, name: str, *, accept: bool = True, reply_after: float | None = 0.0,
                 feedback_at: Iterable[float] = (), finish_after: float | None = None, outcome: str = "succeed",
                 cancel: str = "accept", cancel_delay: float = 0.0, sequence: Iterable[int] = (0, 1, 1, 2)) -> None:
        self.harness = harness
        self.name = name
        self.accept = accept
        self.reply_after = reply_after
        self.feedback_at = sorted(feedback_at)
        self.finish_after = finish_after
        self.outcome = outcome
        self.cancel = cancel
        self.cancel_delay = cancel_delay
        self.sequence = list(sequence)
        self.goals: list[tuple[str, float]] = []      # goal requests: (goal id, time)
        self.accepted: list[tuple[str, float]] = []
        self.cancels: list[tuple[str, float]] = []
        self.ended: list[tuple[str, float, str]] = []
        self._closed = False
        self.server = ActionServer(harness.peer, Fibonacci, name, execute_callback=self._execute,
                                   goal_callback=self._on_goal, cancel_callback=self._on_cancel,
                                   handle_accepted_callback=self._on_accepted)
        harness.keep(self)

    async def _on_goal(self, request: Any) -> GoalResponse:
        self.goals.append(("", self.harness.now()))
        if self.reply_after is None:
            await Future()  # never answered
        if self.reply_after:
            await self.harness.sleep(self.reply_after)
        return GoalResponse.ACCEPT if self.accept else GoalResponse.REJECT

    def _on_accepted(self, handle: Any) -> None:
        self.accepted.append((bytes(bytearray(handle.goal_id.uuid)).hex(), self.harness.now()))
        handle.execute()

    def _on_cancel(self, handle: Any) -> CancelResponse:
        self.cancels.append((bytes(bytearray(handle.goal_id.uuid)).hex(), self.harness.now()))
        return CancelResponse.ACCEPT if self.cancel == "accept" else CancelResponse.REJECT

    async def _execute(self, handle: Any) -> Fibonacci.Result:
        goal_id = bytes(bytearray(handle.goal_id.uuid)).hex()
        start = self.harness.now()
        feedback = list(self.feedback_at)
        cancel_seen: float | None = None
        while not self._closed:
            now = self.harness.now() - start
            if handle.is_cancel_requested:
                cancel_seen = cancel_seen if cancel_seen is not None else self.harness.now()
                if self.harness.now() - cancel_seen >= self.cancel_delay:
                    handle.canceled()
                    self.ended.append((goal_id, self.harness.now(), "canceled"))
                    return Fibonacci.Result()
            while feedback and now >= feedback[0]:
                feedback.pop(0)
                sent = len(self.feedback_at) - len(feedback)  # the n-th feedback carries n + 2 numbers
                handle.publish_feedback(Fibonacci.Feedback(sequence=self.sequence[:2 + sent]))
            if self.finish_after is not None and now >= self.finish_after:
                if self.outcome == "succeed":
                    handle.succeed()
                else:
                    handle.abort()
                self.ended.append((goal_id, self.harness.now(), self.outcome))
                return Fibonacci.Result(sequence=self.sequence)
            await self.harness.sleep(0.01)
        handle.abort()
        return Fibonacci.Result()

    def cancel_times(self, goal_id: str | None = None) -> list[float]:
        return [at for goal, at in self.cancels if goal_id is None or goal == goal_id]

    def close(self) -> None:
        self._closed = True
        self.server.destroy()


class ScriptedService:
    """A ``std_srvs/srv/Trigger`` service: replies after ``reply_after`` seconds (None: never)."""

    def __init__(self, harness: Harness, name: str, *, success: bool = True, message: str = "",
                 reply_after: float | None = 0.0) -> None:
        self.harness = harness
        self.success = success
        self.message = message
        self.reply_after = reply_after
        self.requests: list[float] = []
        self.replies: list[float] = []
        self.service = harness.peer.create_service(Trigger, name, self._on_request)
        harness.keep(self)

    async def _on_request(self, request: Any, response: Any) -> Any:
        self.requests.append(self.harness.now())
        if self.reply_after is None:
            await Future()
        if self.reply_after:
            await self.harness.sleep(self.reply_after)
        response.success = self.success
        response.message = self.message
        self.replies.append(self.harness.now())
        return response

    def close(self) -> None:
        self.harness.peer.destroy_service(self.service)


# ============================================================================== executor processes
def child_env() -> dict[str, str]:
    env = dict(os.environ)
    paths = [str(REPO / "bt_server_core"), str(REPO / "bt_server_executor"), str(TEST_DIR)]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["PYTHONUNBUFFERED"] = "1"
    return env


class ExecutorProc:
    """An executor process in a namespace of its own, with its own log folder and journal."""

    def __init__(self, folder: Path, tag: str) -> None:
        self.ns = unique_ns(tag)
        self.name = f"{self.ns}/bt_executor"
        self.folder = folder / self.ns.strip("/")
        self.folder.mkdir(parents=True, exist_ok=True)
        params = {"log_dir": str(self.folder / "log"), "journal_file": str(self.folder / "goals.jsonl")}
        lines = ["/**:", "  ros__parameters:"] + [f"    {key}: {dumps_json(value, indent=None)}"
                                                 for key, value in params.items()]
        self.params_file = self.folder / "params.yaml"
        self.params_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.output = self.folder / "executor.out"
        self.proc: subprocess.Popen | None = None

    def start(self) -> ExecutorProc:
        with open(self.output, "ab") as handle:
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "bt_server_executor", "--ros-args", "-r", f"__ns:={self.ns}",
                 "--params-file", str(self.params_file)],
                env=child_env(), stdout=handle, stderr=subprocess.STDOUT, cwd=str(self.folder))
        return self

    def kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait(timeout=10)

    def stop(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        self.proc.send_signal(signal.SIGINT)
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.kill()

    def text(self) -> str:
        return self.output.read_text(encoding="utf-8", errors="replace") if self.output.exists() else ""
