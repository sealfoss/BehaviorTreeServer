"""Remote Sub-Tree between two executor processes, local and remote: Appendix B.10, M-01 to M-08
(design 6.8). M-09 (Zoom In in the page) belongs to the page's tests.

The test process sends the local executor an ``ExecuteTree`` goal whose tree has a Remote
Sub-Tree node naming the remote executor, and watches both executors' topics.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("rclpy")
pytest.importorskip("bt_server_interfaces.action")

from action_msgs.msg import GoalStatus, GoalStatusArray  # noqa: E402
from rclpy.action import ActionClient  # noqa: E402
from rclpy.qos import QoSProfile  # noqa: E402

from bt_server_core.values import dumps_json, loads_json  # noqa: E402
from bt_server_executor.qos import DETAILS_QOS, RUN_QOS, STATUS_SUB_QOS  # noqa: E402
from bt_server_interfaces.action import ExecuteTree  # noqa: E402
from bt_server_interfaces.msg import NodeDetails, RunInfo, TreeStatus  # noqa: E402
from ros_helpers import (  # noqa: E402
    ExecutorProc,
    Harness,
    ScriptedAction,
    bundle,
    entry,
    evaluation,
    leaf,
    tree,
)

pytestmark = [pytest.mark.ros, pytest.mark.slow]

REMOTE_KEY = "rt"
NODE = 1  # the Remote Sub-Tree node is the Root's only child


class Executor:
    """An executor process seen from the test: its action client and recorded topics."""

    def __init__(self, h: Harness, proc: ExecutorProc) -> None:
        self.h = h
        self.proc = proc
        self.name = proc.name
        self.client = ActionClient(h.node, ExecuteTree, f"{proc.name}/execute")
        self.statuses: list[tuple[float, TreeStatus]] = []
        self.details: list[tuple[float, int, str, object]] = []
        self.runs: list[str] = []
        self.goal_statuses: dict[str, int] = {}
        h.node.create_subscription(TreeStatus, f"{proc.name}/status", self._on_status, STATUS_SUB_QOS)
        h.node.create_subscription(NodeDetails, f"{proc.name}/details", self._on_details, DETAILS_QOS)
        h.node.create_subscription(RunInfo, f"{proc.name}/run", self._on_run, RUN_QOS)
        h.node.create_subscription(GoalStatusArray, f"{proc.name}/execute/_action/status", self._on_goals,
                                   QoSProfile(depth=10))

    def _on_status(self, message: TreeStatus) -> None:
        self.statuses.append((time.monotonic(), message))

    def _on_details(self, message: NodeDetails) -> None:
        for item in loads_json(message.details_json):
            self.details.append((time.monotonic(), item["i"], item["kind"], item.get("value")))

    def _on_run(self, message: RunInfo) -> None:
        run_id = bytes(bytearray(message.run_id.uuid)).hex()
        if run_id != "0" * 32:
            self.runs.append(run_id)

    def _on_goals(self, message: GoalStatusArray) -> None:
        for item in message.status_list:
            self.goal_statuses[bytes(bytearray(item.goal_info.goal_id.uuid)).hex()] = item.status

    def wait_ready(self) -> None:
        if not self.h.spin_until(lambda: self.client.server_is_ready() and self.statuses, 30.0):
            raise TimeoutError(f"{self.name} did not come up:\n{self.proc.text()}")

    def send(self, the_bundle: dict, entries: list[dict]) -> object:
        goal = ExecuteTree.Goal(bundle_json=dumps_json(the_bundle, indent=None),
                                blackboard_json=dumps_json(entries, indent=None), label="m", requested_by="pytest")
        future = self.client.send_goal_async(goal)
        self.h.spin_until(future.done, 10.0)
        handle = future.result()
        assert handle.accepted
        return handle

    def result(self, handle, timeout: float = 30.0):
        future = handle.get_result_async()
        if not self.h.spin_until(future.done, timeout):
            raise TimeoutError(f"no result from {self.name}:\n{self.proc.text()}")
        return future.result()

    def state(self) -> int | None:
        return self.statuses[-1][1].state if self.statuses else None

    def error(self, index: int = NODE) -> tuple[float, object] | None:
        found = [(at, value) for at, i, kind, value in self.details if i == index and kind == "error" and value]
        return found[-1] if found else None


@pytest.fixture
def peers(tmp_path):
    h = Harness(tmp_path, "m")
    procs = [ExecutorProc(tmp_path, "local").start(), ExecutorProc(tmp_path, "remote").start()]
    local, remote = Executor(h, procs[0]), Executor(h, procs[1])
    try:
        local.wait_ready()
        remote.wait_ready()
        yield h, local, remote
    finally:
        for proc in procs:
            proc.stop()
        h.close()


def remote_node(remote: Executor, watchdog: float = 5.0, **links) -> dict:
    node = leaf("RemoteSubTree", "R", Executor=remote.name, File=REMOTE_KEY, Timeout=60.0, Watchdog=watchdog,
                **links)
    node["tree"] = REMOTE_KEY
    return node


def setter() -> dict:
    return leaf("Set", "Set y", ValueName="y", CompareTo="x", LiteralValue=None)


def hold_tree() -> dict:
    return tree(evaluation("hold", "==", True, wait=True, title="hold"))


REMOTE_BOARD = [entry("x", "Integer", 0), entry("y", "Integer", 0), entry("hold", "Bool", False)]
LOCAL_BOARD = [entry("a", "Integer", 5), entry("b", "Integer", 0)]


def local_bundle(remote: Executor, remote_tree: dict, watchdog: float = 5.0) -> dict:
    return bundle(remote_node(remote, watchdog, bindings={"x": "a"}, outputs={"y": "b"}),
                  extra={REMOTE_KEY: remote_tree}, blackboards={REMOTE_KEY: REMOTE_BOARD})


def final(result, name: str):
    return next(item["value"] for item in loads_json(result.result.blackboard_json) if item["name"] == name)


def started_remote_run(h: Harness, remote: Executor) -> float:
    """Wait until the remote executor shows a run going; returns when."""
    if not h.spin_until(lambda: remote.state() == TreeStatus.STATE_RUNNING, 15.0):
        raise TimeoutError(f"the remote run did not start:\n{remote.proc.text()}")
    return next(at for at, message in remote.statuses if message.state == TreeStatus.STATE_RUNNING)


def test_M_01_values_in_and_out(peers):
    h, local, remote = peers
    handle = local.send(local_bundle(remote, tree(setter())), LOCAL_BOARD)
    result = local.result(handle)
    assert result.result.outcome == ExecuteTree.Result.OUTCOME_SUCCEEDED, local.proc.text()
    assert final(result, "b") == 5
    remote_run = [value for _, i, kind, value in local.details if i == NODE and kind == "remote_run"]
    assert remote_run and remote_run[-1]["executor"] == remote.name and remote_run[-1]["run_id"] in remote.runs


def test_M_02_remote_root_fails_and_outputs_are_written(peers):
    h, local, remote = peers
    failing = tree(setter(), evaluation("x", "==", 99, title="fails"))
    result = local.result(local.send(local_bundle(remote, failing), LOCAL_BOARD))
    assert result.result.outcome == ExecuteTree.Result.OUTCOME_FAILED
    assert final(result, "b") == 5
    assert "the remote tree failed" in str(local.error()[1])


def test_M_03_busy_remote_executor(peers):
    h, local, remote = peers
    remote.send(bundle(evaluation("hold", "==", True, wait=True)), [entry("hold", "Bool", False)])
    started_remote_run(h, remote)
    runs_before = list(remote.runs)
    result = local.result(local.send(local_bundle(remote, tree(setter())), LOCAL_BOARD))
    assert result.result.outcome == ExecuteTree.Result.OUTCOME_FAILED
    assert local.error()[1] == "executor busy"
    h.spin_for(0.5)
    assert remote.runs == runs_before
    assert "goal rejected" not in remote.proc.text()


def test_M_04_halt_cancels_the_remote_run(peers):
    h, local, remote = peers
    server = ScriptedAction(h, "fib", cancel_delay=0.3)
    action = leaf("CallAction", "A", Action=f"{h.ns}/fib", Type="example_interfaces/action/Fibonacci",
                  Goal={"order": 2})
    handle = local.send(local_bundle(remote, tree(action)), LOCAL_BOARD)
    h.spin_until(lambda: server.accepted, 15.0)
    assert server.accepted, remote.proc.text()
    h.spin_for(0.5)
    halted = time.monotonic()
    handle.cancel_goal_async()
    result = local.result(handle)
    assert result.status == GoalStatus.STATUS_CANCELED
    assert result.result.outcome == ExecuteTree.Result.OUTCOME_STOPPED and result.result.unconfirmed_cancels == 0
    assert server.cancels, "the remote tree's action got no cancel"
    assert any(message.state == TreeStatus.STATE_STOPPING for at, message in remote.statuses if at >= halted)
    remote_run = [value for _, i, kind, value in local.details if i == NODE and kind == "remote_run"][-1]
    h.spin_until(lambda: remote.goal_statuses.get(remote_run["run_id"]) == GoalStatus.STATUS_CANCELED, 3.0)
    assert remote.goal_statuses.get(remote_run["run_id"]) == GoalStatus.STATUS_CANCELED
    cancelling = [at for at, message in local.statuses if at >= halted and message.node_status
                  and message.node_status[NODE] == 4]
    remote_end = next(at for at, message in remote.statuses
                      if at >= halted and message.state == TreeStatus.STATE_IDLE)
    assert cancelling and cancelling[-1] <= remote_end + 0.3


def _kill_local_during_run(peers, watchdog: float) -> tuple:
    h, local, remote = peers
    local.send(local_bundle(remote, hold_tree(), watchdog), LOCAL_BOARD)
    started = started_remote_run(h, remote)
    h.spin_until(lambda: time.monotonic() >= started + 1.0)
    killed = time.monotonic()
    local.proc.kill()
    return h, remote, killed


def test_M_05_watchdog_stops_the_remote_run(peers):
    h, remote, killed = _kill_local_during_run(peers, 5.0)
    assert h.spin_until(lambda: remote.state() in (TreeStatus.STATE_STOPPING, TreeStatus.STATE_IDLE), 10.0)
    stopped = next(at for at, message in remote.statuses
                   if at > killed and message.state in (TreeStatus.STATE_STOPPING, TreeStatus.STATE_IDLE))
    assert 4.4 <= stopped - killed <= 5.3, stopped - killed
    h.spin_until(lambda: remote.state() == TreeStatus.STATE_IDLE, 5.0)
    assert remote.statuses[-1][1].outcome == ExecuteTree.Result.OUTCOME_STOPPED


def test_M_06_watchdog_0_lets_the_remote_run_go_on(peers):
    h, remote, killed = _kill_local_during_run(peers, 0.0)
    h.spin_until(lambda: time.monotonic() >= killed + 7.0)
    assert remote.state() == TreeStatus.STATE_RUNNING


def test_M_07_remote_executor_killed(peers):
    h, local, remote = peers
    local.send(local_bundle(remote, hold_tree()), LOCAL_BOARD)
    started = started_remote_run(h, remote)
    h.spin_until(lambda: time.monotonic() >= started + 1.0)
    killed = time.monotonic()
    remote.proc.kill()
    assert h.spin_until(lambda: local.error() is not None, 8.0), local.proc.text()
    at, reason = local.error()
    assert reason == "executor lost"
    assert 2.4 <= at - killed <= 3.2, at - killed


def test_M_08_remote_tree_runs_once(peers):
    h, local, remote = peers
    looping = tree(setter(), config={"repeat": True})
    result = local.result(local.send(local_bundle(remote, looping), LOCAL_BOARD), 15.0)
    assert result.result.outcome == ExecuteTree.Result.OUTCOME_SUCCEEDED
    assert final(result, "b") == 5
    h.spin_for(0.5)
    assert remote.state() == TreeStatus.STATE_IDLE
