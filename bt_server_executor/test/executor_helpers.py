"""Helpers for the executor tests (they need ROS 2 and the built ``bt_server_interfaces``).

* :class:`RosHelper`: one rclpy node in the test process, spun by a single-threaded executor on
  a background thread; waiting, service calls, goals and recording subscriptions. (rclpy's
  ``MultiThreadedExecutor`` starves the other threads of this process on Lyrical: a 2 s
  ``time.sleep`` took 8 s next to it, and messages arrived seconds late.)
* :class:`ExecutorProcess`: an executor started as ``python -m bt_server_executor`` with a
  parameters file of its own, in a namespace of its own, on ``ROS_DOMAIN_ID`` 41.
* :class:`FibServer`: a scripted ``example_interfaces/action/Fibonacci`` server that accepts
  goals, runs until cancelled (or finishes after ``finish_after`` seconds) and records goals and
  cancel requests with their times.
* :func:`tree`: a bundle whose Root runs the given leaves in a Sequence.
* Fixtures ``ros`` (session) and ``start_executor``: test modules import them by name. They are
  not in a ``conftest.py``: other test folders import their own ``conftest`` as a module, and a
  second ``conftest.py`` on ``sys.path`` would shadow theirs when the whole repository is run.
"""

from __future__ import annotations

import itertools
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import rclpy
from action_msgs.srv import CancelGoal
from example_interfaces.action import Fibonacci
from rclpy.action import ActionClient, ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.task import Future

from bt_server_core.values import dumps_json
from bt_server_interfaces.action import ExecuteTree

TEST_DIR = Path(__file__).resolve().parent
REPO = TEST_DIR.parents[1]
DOMAIN_ID = "41"
_counter = itertools.count(1)


def unique_ns(tag: str) -> str:
    """A namespace no other test (or test run) uses: ``/<tag>_<pid>_<n>``."""
    return f"/{tag}_{os.getpid()}_{next(_counter)}"


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    paths = [str(REPO / "bt_server_core"), str(REPO / "bt_server_executor"), str(REPO / "bt_server_gateway"),
             str(TEST_DIR)]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["ROS_DOMAIN_ID"] = DOMAIN_ID
    env["ROS_AUTOMATIC_DISCOVERY_RANGE"] = "LOCALHOST"
    env["PYTHONUNBUFFERED"] = "1"
    return env


# ---------------------------------------------------------------------------------------------
# Bundles

def new_id() -> str:
    return uuid.uuid4().hex


def leaf(type_name: str, title: str | None = None, **fields: Any) -> dict:
    return {"id": new_id(), "type": type_name, "title": title or type_name, "x": 0.0, "y": 0.0,
            **({"fields": fields} if fields else {})}


def tree(*leaves: dict, composite: str = "Sequence", config: dict | None = None, key: str = "main") -> dict:
    """A bundle: Root (``composite``) with ``leaves`` as its children, in order."""
    root = {"id": new_id(), "type": "Root", "title": "Root", "composite": composite, "memory": True,
            "x": 0.0, "y": -100.0}
    nodes = [root, *leaves]
    connections = [{"parent": root["id"], "child": item["id"]} for item in leaves]
    return {"format": "behavior_tree_bundle", "version": 1, "root": key,
            "trees": {key: {"config": config or {}, "nodes": nodes, "connections": connections}}}


def goal(bundle: dict, blackboard: list | None = None, label: str = "test", **watchdog: Any) -> ExecuteTree.Goal:
    message = ExecuteTree.Goal(bundle_json=dumps_json(bundle, indent=None),
                               blackboard_json=dumps_json(blackboard or [], indent=None),
                               label=label, requested_by="pytest")
    for name, value in watchdog.items():
        setattr(message, name, value)
    return message


# ---------------------------------------------------------------------------------------------
# The test process's node

class RosHelper:
    """The test process's rclpy node (see the module docs)."""

    def __init__(self) -> None:
        self.context = rclpy.Context()
        rclpy.init(context=self.context, domain_id=int(DOMAIN_ID))
        self.node = Node(f"bt_test_{os.getpid()}_{next(_counter)}", context=self.context)
        self.group = ReentrantCallbackGroup()
        self.executor = SingleThreadedExecutor(context=self.context)
        self.executor.add_node(self.node)
        self._thread = threading.Thread(target=self.executor.spin, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.executor.shutdown(timeout_sec=2.0)
        self.node.destroy_node()
        rclpy.shutdown(context=self.context)

    @staticmethod
    def wait(predicate: Callable[[], Any], timeout: float = 10.0, period: float = 0.02) -> Any:
        """Poll ``predicate`` until it is truthy (returned) or ``timeout`` passes (its last value)."""
        deadline = time.monotonic() + timeout
        while True:
            value = predicate()
            if value or time.monotonic() >= deadline:
                return value
            time.sleep(period)

    def result_of(self, future: Any, timeout: float = 10.0) -> Any:
        if not self.wait(future.done, timeout):
            raise TimeoutError("no answer in time")
        return future.result()

    def call(self, srv_type: Any, name: str, request: Any, timeout: float = 10.0) -> Any:
        client = self.node.create_client(srv_type, name, callback_group=self.group)
        try:
            if not client.wait_for_service(timeout_sec=timeout):
                raise TimeoutError(f"service {name} not available")
            return self.result_of(client.call_async(request), timeout)
        finally:
            self.node.destroy_client(client)

    def action_client(self, ns: str) -> ActionClient:
        client = ActionClient(self.node, ExecuteTree, f"{ns}/bt_executor/execute", callback_group=self.group)
        if not client.wait_for_server(timeout_sec=20.0):
            raise TimeoutError(f"{ns}/bt_executor/execute not available")
        return client

    def send(self, client: ActionClient, message: ExecuteTree.Goal, feedback: list | None = None) -> Any:
        """Send a goal; returns the accepted goal handle (or raises for a rejected one)."""
        callback = (lambda msg: feedback.append(msg.feedback)) if feedback is not None else None
        handle = self.result_of(client.send_goal_async(message, feedback_callback=callback))
        if not handle.accepted:
            raise RuntimeError("goal rejected")
        return handle

    def record(self, msg_type: Any, topic: str, qos: Any) -> Recorder:
        return Recorder(self, msg_type, topic, qos)

    def cancel_by_id(self, action: str, goal_id: bytes) -> Any:
        request = CancelGoal.Request()
        request.goal_info.goal_id.uuid = list(goal_id)
        return self.call(CancelGoal, f"{action}/_action/cancel_goal", request)


class Recorder:
    """Every message of a topic, with its arrival time (``time.monotonic``)."""

    def __init__(self, ros: RosHelper, msg_type: Any, topic: str, qos: Any) -> None:
        self.messages: list[Any] = []
        self.times: list[float] = []
        self._ros = ros
        self._lock = threading.Lock()
        self.subscription = ros.node.create_subscription(msg_type, topic, self._on_message, qos,
                                                         callback_group=ros.group)

    def _on_message(self, msg: Any) -> None:
        with self._lock:
            self.messages.append(msg)
            self.times.append(time.monotonic())

    def since(self, start: float, end: float | None = None) -> list[Any]:
        with self._lock:
            return [msg for msg, at in zip(self.messages, self.times) if at >= start and (end is None or at <= end)]

    def last(self) -> Any:
        with self._lock:
            return self.messages[-1] if self.messages else None

    def close(self) -> None:
        self._ros.node.destroy_subscription(self.subscription)


# ---------------------------------------------------------------------------------------------
# Executor processes

class ExecutorProcess:
    """An executor process (see the module docs)."""

    def __init__(self, ns: str, folder: Path, params: dict | None = None, *, own_files: bool = True) -> None:
        self.ns = ns
        self.name = f"{ns}/bt_executor"
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        values = {"leaf_modules": ["bt_test_leaves"]}
        if own_files:
            values["log_dir"] = str(folder / "log")
            values["journal_file"] = str(folder / "goals.jsonl")
        values.update(params or {})
        lines = ["/**:", "  ros__parameters:"]
        for key, value in values.items():
            lines.append(f"    {key}: {dumps_json(value, indent=None)}")
        self.params_file = folder / "params.yaml"
        self.params_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.output = folder / "executor.out"
        self.proc: subprocess.Popen | None = None

    def start(self) -> ExecutorProcess:
        handle = open(self.output, "ab")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "bt_server_executor", "--ros-args", "-r", f"__ns:={self.ns}",
             "--params-file", str(self.params_file)],
            env=child_env(), stdout=handle, stderr=subprocess.STDOUT, cwd=str(self.folder))
        handle.close()
        return self

    def signal(self, signum: int) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(signum)

    def kill(self) -> None:
        self.signal(signal.SIGKILL)
        if self.proc is not None:
            self.proc.wait(timeout=10)

    def stop(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        self.proc.send_signal(signal.SIGINT)
        try:
            self.proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.kill()

    def text(self) -> str:
        return self.output.read_text(encoding="utf-8", errors="replace") if self.output.exists() else ""


# ---------------------------------------------------------------------------------------------
# A scripted action server

class FibServer:
    """A Fibonacci action server that accepts every goal and runs until it is cancelled, or
    finishes after ``finish_after`` seconds; cancels are accepted (see the module docs)."""

    def __init__(self, ros: RosHelper, name: str, finish_after: float | None = None) -> None:
        self.name = name
        self.finish_after = finish_after
        self.goals: list[tuple[str, float]] = []
        self.cancels: list[tuple[str, float]] = []
        self._closed = False
        self._server = ActionServer(ros.node, Fibonacci, name, execute_callback=self._execute,
                                    goal_callback=lambda request: GoalResponse.ACCEPT,
                                    cancel_callback=self._on_cancel, callback_group=ros.group)
        self._ros = ros

    def _on_cancel(self, handle: Any) -> CancelResponse:
        self.cancels.append((bytes(bytearray(handle.goal_id.uuid)).hex(), time.monotonic()))
        return CancelResponse.ACCEPT

    async def _execute(self, handle: Any) -> Fibonacci.Result:
        self.goals.append((bytes(bytearray(handle.goal_id.uuid)).hex(), time.monotonic()))
        start = time.monotonic()
        while not self._closed:
            if handle.is_cancel_requested:
                handle.canceled()
                return Fibonacci.Result()
            if self.finish_after is not None and time.monotonic() - start >= self.finish_after:
                handle.succeed()
                return Fibonacci.Result(sequence=[0, 1, 1])
            await self._sleep(0.02)
        handle.abort()
        return Fibonacci.Result()

    def _sleep(self, seconds: float) -> Future:
        future: Future = Future()
        node = self._ros.node

        def done() -> None:
            timer.cancel()
            node.destroy_timer(timer)
            future.set_result(None)

        timer = node.create_timer(seconds, done, callback_group=self._ros.group)
        return future

    def cancelled(self, goal_id: str) -> float | None:
        return next((at for goal, at in self.cancels if goal == goal_id), None)

    def close(self) -> None:
        self._closed = True
        time.sleep(0.05)
        self._server.destroy()


# ---------------------------------------------------------------------------------------------
# Fixtures (imported by the test modules)

@pytest.fixture(scope="session")
def ros() -> Iterator[RosHelper]:
    """The test process's rclpy node."""
    helper = RosHelper()
    yield helper
    helper.close()


@pytest.fixture
def start_executor(tmp_path: Path) -> Iterator[Callable[..., ExecutorProcess]]:
    """``start_executor(tag, params=None, *, own_files=True, ns=None, folder=None)``: an executor
    process in a namespace of its own; stopped after the test."""
    started: list[ExecutorProcess] = []

    def start(tag: str, params: dict | None = None, *, own_files: bool = True, ns: str | None = None,
              folder: Path | None = None) -> ExecutorProcess:
        ns = ns or unique_ns(tag)
        process = ExecutorProcess(ns, folder or tmp_path / ns.strip("/"), params, own_files=own_files)
        process.start()
        started.append(process)
        return process

    yield start
    for process in started:
        process.stop()
