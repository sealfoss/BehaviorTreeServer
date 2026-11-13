"""The ROS leaves against scripted servers: Appendix B.8, L-01 to L-15 (design 6.5).

Times count from the start of the run, whose first tick starts the leaf. The tolerance of the
timings is a few tenths of a second: these run on a real ROS graph.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys

import pytest

pytest.importorskip("rclpy")
pytest.importorskip("bt_server_interfaces.action")

from action_msgs.srv import CancelGoal  # noqa: E402
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy  # noqa: E402
from std_msgs.msg import Float64, String  # noqa: E402

from bt_server_core.engine import Outcome  # noqa: E402
from bt_server_executor.ros_validate import validate_ros  # noqa: E402
from ros_helpers import (  # noqa: E402
    Harness,
    ScriptedAction,
    ScriptedService,
    bundle,
    entry,
    guard,
    leaf,
    sequence,
)

pytestmark = pytest.mark.ros

ACTION = "fib"


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path, "leaves")
    yield harness
    harness.close()


def call_action(name: str = ACTION, **fields) -> dict:
    return leaf("CallAction", "A", Action=name, Type="example_interfaces/action/Fibonacci", Goal={"order": 3},
                **fields)


def near(value: float | None, expected: float, tolerance: float = 0.35) -> bool:
    return value is not None and expected - 0.05 <= value <= expected + tolerance


def test_L_01_action_succeeds_with_feedback_and_outputs(h):
    server = ScriptedAction(h, ACTION, feedback_at=(0.3, 0.6), finish_after=1.0)
    h.ready_action(f"{h.ns}/{ACTION}")
    node = call_action(f"{h.ns}/{ACTION}", outputs={"Result.sequence": "seq", "Feedback.sequence": "fb"})
    run = h.run(bundle(node))
    result = h.finish(run)
    assert result.outcome == Outcome.SUCCEEDED
    assert near(run.sink.status_times(1, "S")[0], 1.0)
    assert run.blackboard.get("seq") == server.sequence
    feedback = run.sink.detail_values(1, "feedback")
    assert [value["sequence"] for _, value in feedback] == [[0, 1, 1], [0, 1, 1, 2]]
    assert all(at < run.sink.status_times(1, "S")[0] for at, _ in feedback)
    assert run.blackboard.get("fb") == [0, 1, 1, 2]
    assert h.journal.open_goals() == []


def test_L_02_no_server(h):
    run = h.run(bundle(call_action(f"{h.ns}/nobody")))
    h.finish(run)
    assert near(run.sink.status_times(1, "F")[0], 2.0)
    assert run.sink.error_of(1) == "server not available"


def test_L_03_goal_rejected(h):
    ScriptedAction(h, ACTION, accept=False)
    h.ready_action(f"{h.ns}/{ACTION}")
    run = h.run(bundle(call_action(f"{h.ns}/{ACTION}")))
    h.finish(run)
    assert run.sink.error_of(1) == "goal rejected"
    assert h.journal.open_goals() == []


def test_L_04_no_reply_to_the_goal_and_a_late_acceptance_is_cancelled(h):
    server = ScriptedAction(h, ACTION, reply_after=6.0)
    h.ready_action(f"{h.ns}/{ACTION}")
    run = h.run(bundle(call_action(f"{h.ns}/{ACTION}")))
    h.finish(run)
    assert near(run.sink.status_times(1, "F")[0], 5.0)
    assert run.sink.error_of(1) == "no reply to the goal"
    h.spin_until(lambda: server.cancels, 3.0)
    assert server.accepted and near(server.accepted[0][1], 6.0)
    assert server.cancels and server.cancels[0][0] == server.accepted[0][0] and near(server.cancels[0][1], 6.0)
    h.spin_until(lambda: not h.journal.open_goals(), 3.0)
    assert h.journal.open_goals() == []


def test_L_05_aborted(h):
    ScriptedAction(h, ACTION, finish_after=1.0, outcome="abort")
    h.ready_action(f"{h.ns}/{ACTION}")
    run = h.run(bundle(call_action(f"{h.ns}/{ACTION}")))
    h.finish(run)
    assert near(run.sink.status_times(1, "F")[0], 1.0)
    assert run.sink.error_of(1) == "aborted"


def test_L_06_timeout(h):
    server = ScriptedAction(h, ACTION, cancel_delay=0.3)
    h.ready_action(f"{h.ns}/{ACTION}")
    run = h.run(bundle(call_action(f"{h.ns}/{ACTION}", Timeout=2.0)))
    h.finish(run)
    assert near(run.sink.status_times(1, "C")[0], 2.0)
    assert near(run.sink.status_times(1, "F")[0], 2.3)
    assert run.sink.error_of(1) == "timed out"
    assert near(server.cancel_times()[0], 2.0)


def test_L_07_no_feedback(h):
    server = ScriptedAction(h, ACTION)
    h.ready_action(f"{h.ns}/{ACTION}")
    run = h.run(bundle(call_action(f"{h.ns}/{ACTION}", Feedback_Timeout=1.0)))
    h.finish(run)
    assert near(server.cancel_times()[0], 1.0)
    assert near(run.sink.status_times(1, "C")[0], 1.0)
    assert run.sink.status_times(1, "F")[0] >= server.ended[0][1] - 0.01
    assert run.sink.error_of(1) == "no feedback"
    assert run.result.outcome == Outcome.FAILED


def test_L_08_halted_before_the_goal_reply(h):
    server = ScriptedAction(h, ACTION, reply_after=0.3)
    h.ready_action(f"{h.ns}/{ACTION}")
    run = h.run(bundle(call_action(f"{h.ns}/{ACTION}")))
    h.spin_to(0.1)
    run.runner.stop()
    assert run.sink.letters().split()[1] == "C"
    result = h.finish(run)
    assert server.cancels, "the late goal was not cancelled"
    assert server.cancels[0][1] >= server.accepted[0][1] - 0.01 and near(server.cancels[0][1], 0.3)
    assert server.ended and server.ended[0][2] == "canceled"
    assert result.outcome == Outcome.STOPPED and result.unconfirmed_cancels == 0
    assert run.ended_at >= server.ended[0][1] - 0.01


@pytest.mark.slow
def test_L_09_refused_cancel_stays_unsettled_until_the_result(h):
    server = ScriptedAction(h, ACTION, cancel="reject", finish_after=20.0)
    h.ready_action(f"{h.ns}/{ACTION}")
    node = call_action(f"{h.ns}/{ACTION}")
    run = h.run(bundle(sequence(guard(), node, memory=False)), [entry("go", "Bool", True)])
    h.spin_to(1.0)
    h.set_entry(run, "go", False)
    result = h.finish(run, 10.0)
    assert near(run.sink.status_times(3, "C")[0], 1.0)
    assert near(run.ended_at, 6.0)
    assert result.outcome == Outcome.STOPPED and result.unconfirmed_cancels == 1
    assert run.runner.unsettled == 1 and h.journal.unsettled_count() == 1
    h.spin_to(19.5)
    assert len([at for at in server.cancel_times() if 6.0 <= at < 19.5]) >= 3, server.cancels
    assert h.journal.unsettled_count() == 1
    h.spin_until(lambda: h.journal.unsettled_count() == 0, 3.0)
    assert server.ended and near(server.ended[0][1], 20.0)
    assert run.runner.unsettled == 0 and h.journal.open_goals() == []
    with open(h.journal.path, encoding="utf-8") as handle:
        records = [json.loads(line) for line in handle if line.strip()]
    assert records[-1]["e"] == "done"


def test_L_10_call_service(h):
    ScriptedService(h, "ok", success=True)
    ScriptedService(h, "busy", success=False, message="busy")
    ScriptedService(h, "silent", reply_after=None)
    for name in ("ok", "busy", "silent"):
        h.ready_service(f"{h.ns}/{name}")
    runs = {}
    for name in ("ok", "busy", "silent"):
        node = leaf("CallService", "S", Service=f"{h.ns}/{name}", Type="std_srvs/srv/Trigger")
        runs[name] = run = h.run(bundle(node))
        h.finish(run)
    assert runs["ok"].result.outcome == Outcome.SUCCEEDED
    assert runs["busy"].result.outcome == Outcome.FAILED and runs["busy"].sink.error_of(1) == "busy"
    assert runs["silent"].result.outcome == Outcome.FAILED
    assert near(runs["silent"].sink.status_times(1, "F")[0], 5.0)


def test_L_11_halted_service_call_settles_at_once(h):
    server = ScriptedService(h, "slow", reply_after=2.0)
    h.ready_service(f"{h.ns}/slow")
    node = leaf("CallService", "S", Service=f"{h.ns}/slow", Type="std_srvs/srv/Trigger")
    run = h.run(bundle(sequence(guard(), node, memory=False)), [entry("go", "Bool", True)])
    h.spin_to(1.0)
    h.set_entry(run, "go", False)
    h.finish(run)
    assert near(run.sink.status_times(3, "-")[-1], 1.0, 0.2)
    assert "C" not in [letters.split()[3] for _, letters in run.sink.timeline]
    h.spin_to(2.5)
    assert server.replies and near(server.replies[0], 2.0)
    assert run.sink.letters().split()[3] == "-"


def test_L_12_publish_with_a_bound_field(h):
    received = []
    h.peer.create_subscription(Float64, f"{h.ns}/out", received.append, 10)
    qos = QoSProfile(history=HistoryPolicy.KEEP_LAST, depth=10, reliability=ReliabilityPolicy.RELIABLE,
                     durability=DurabilityPolicy.VOLATILE)
    publisher = h.clients.publisher(Float64, f"{h.ns}/out", qos)
    h.wait_for_graph(lambda: publisher.get_subscription_count() == 1)
    node = leaf("Publish", "P", Topic=f"{h.ns}/out", Type="std_msgs/msg/Float64", Message={"data": 0.0},
                bindings={"Message.data": "x"})
    run = h.run(bundle(node), [entry("x", "Double", 2.5)])
    assert h.finish(run).outcome == Outcome.SUCCEEDED
    h.spin_for(1.0)
    assert [message.data for message in received] == [2.5]


def test_L_13_publish_waits_for_subscribers(h):
    node = leaf("Publish", "P", Topic=f"{h.ns}/nobody", Type="std_msgs/msg/Float64", Wait_For_Subscribers=1)
    run = h.run(bundle(node))
    h.finish(run)
    assert near(run.sink.status_times(1, "F")[0], 5.0)
    assert "subscriber" in run.sink.error_of(1)


@pytest.mark.parametrize("case", ["missing", "stale"])
def test_L_14_binding_to_a_missing_or_stale_entry(h, case):
    server = ScriptedAction(h, ACTION, finish_after=0.2)
    h.ready_action(f"{h.ns}/{ACTION}")
    node = call_action(f"{h.ns}/{ACTION}", bindings={"Goal.order": "n"})
    entries = [] if case == "missing" else [
        entry("n", "Integer", 3, source={"topic": f"{h.ns}/quiet", "type": "std_msgs/msg/Int32", "field": "data"})]
    run = h.run(bundle(node), entries)
    h.finish(run)
    reason = run.sink.error_of(1)
    assert run.sink.status_times(1, "F")[0] < 0.2
    assert "'n'" in reason and ("missing" in reason if case == "missing" else "no recent data" in reason)
    h.spin_for(0.3)
    assert server.goals == []


def test_L_15_type_that_is_no_interface_package(h):
    node = leaf("CallAction", "A", Action=f"{h.ns}/{ACTION}", Type="os/action/Thing")
    the_bundle = bundle(node)
    findings = validate_ros(h.node, the_bundle, [])
    assert any(item["severity"] == "error" and "not an interface package" in item["text"] for item in findings)
    run = h.run(the_bundle)
    h.finish(run)
    assert run.result.outcome == Outcome.FAILED
    assert "not an interface package" in run.sink.error_of(1)
    assert "os.action" not in sys.modules



SERVER_SCRIPT = """
import sys
import rclpy
from rclpy.task import Future
from rclpy.action import ActionServer
from example_interfaces.action import Fibonacci
rclpy.init()
node = rclpy.create_node(sys.argv[2] if len(sys.argv) > 2 else "lost_server")
async def execute(handle):
    await Future()
server = ActionServer(node, Fibonacci, sys.argv[1], execute_callback=execute)
try:
    rclpy.spin(node)
except BaseException:
    pass
"""


def test_call_action_server_lost(h):
    """6.5, Running: a server that exits is noticed by the check once a second; the node shows
    Cancelling, and Failed "server lost" when the cancel nobody confirms has timed out."""
    proc = subprocess.Popen([sys.executable, "-c", SERVER_SCRIPT, f"{h.ns}/lost"])
    try:
        h.ready_action(f"{h.ns}/lost")
        config = {"cancel_timeout_ms": 1000, "on_unconfirmed_cancel": "continue"}
        run = h.run(bundle(call_action(f"{h.ns}/lost"), config=config))
        h.spin_for(0.5)
        assert run.sink.letters() == "R R"
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=10)
        exited = h.now()
        h.finish(run, 10.0)
        cancelling = run.sink.status_times(1, "C")[0]
        assert cancelling - exited <= 2.5
        assert near(run.sink.status_times(1, "F")[0], cancelling + 1.0)
        assert run.sink.error_of(1) == "server lost"
        assert run.result.unconfirmed_cancels == 1
    finally:
        if proc.poll() is None:
            proc.kill()


def test_call_action_cancelled_by_the_server(h):
    """6.5, Result: a goal cancelled by someone else fails the node "cancelled by the server"."""
    server = ScriptedAction(h, ACTION)
    h.ready_action(f"{h.ns}/{ACTION}")
    run = h.run(bundle(call_action(f"{h.ns}/{ACTION}")))
    assert h.spin_until(lambda: server.accepted, 3.0)
    other = h.peer.create_client(CancelGoal, f"{h.ns}/{ACTION}/_action/cancel_goal")
    request = CancelGoal.Request()
    request.goal_info.goal_id.uuid = list(bytes.fromhex(server.accepted[0][0]))
    other.call_async(request)
    h.finish(run)
    assert run.sink.error_of(1) == "cancelled by the server"


def test_call_action_timeout_cancel_answered_unknown_goal(h):
    """A restarted server answers the Timeout's cancel "unknown goal": the node settles at once
    and fails "timed out", and the Selector goes on (no unconfirmed cancel, no stopped tree).

    The server is killed only once it has accepted the goal (killed before its reply, the leaf
    is halted before the goal reply and rightly waits 5 s for it, design 6.5 and 16), and the
    Timeout passes only after the restarted server is on the network."""
    timeout = 6.0
    procs = [subprocess.Popen([sys.executable, "-c", SERVER_SCRIPT, f"{h.ns}/restart"])]
    try:
        h.ready_action(f"{h.ns}/restart")
        node = call_action(f"{h.ns}/restart", Timeout=timeout)
        fallback = leaf("Set", "ok", ValueName="ok", CompareTo=None, LiteralValue=True)
        run = h.run(bundle(node, fallback, composite="Selector"), [entry("ok", "Bool", False)])
        action_leaf = run.runner.node(1).leaf
        accepted = lambda: action_leaf._goal is not None and action_leaf._goal.handle is not None  # noqa: E731
        assert h.spin_until(accepted, 5.0), "the server did not accept the goal"
        procs[0].send_signal(signal.SIGKILL)
        procs[0].wait(timeout=10)
        procs.append(subprocess.Popen([sys.executable, "-c", SERVER_SCRIPT, f"{h.ns}/restart", "restarted_server"]))
        cancel_service = f"{h.ns}/restart/_action/cancel_goal"

        def restarted() -> bool:
            try:
                services = h.node.get_service_names_and_types_by_node("restarted_server", "/")
            except Exception:  # noqa: BLE001 - the node is not known yet
                return False
            return any(name == cancel_service for name, _ in services)

        assert h.spin_until(restarted, timeout - 1.0), "the restarted server did not come up in time"
        assert h.now() < timeout - 0.5
        result = h.finish(run, 20.0)
        cancelling = run.sink.status_times(1, "C")[0]
        assert near(cancelling, timeout)
        assert run.sink.status_times(1, "F")[0] - cancelling < 1.0
        assert run.sink.error_of(1) == "timed out"
        assert result.outcome == Outcome.SUCCEEDED and result.unconfirmed_cancels == 0
        assert run.blackboard.get("ok") is True
        assert h.journal.open_goals() == []
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()


def test_call_action_halted_before_a_reply_that_never_comes(h):
    """Halted while the goal reply is outstanding, and the server never answers: the node settles
    when the reply wait (5 s) ends, not at the cancel timeout (design 16)."""
    ScriptedAction(h, ACTION, reply_after=None)
    h.ready_action(f"{h.ns}/{ACTION}")
    run = h.run(bundle(call_action(f"{h.ns}/{ACTION}"), config={"cancel_timeout_ms": 8000}))
    h.spin_to(0.1)
    run.runner.stop()
    result = h.finish(run, 12.0)
    assert result.outcome == Outcome.STOPPED and result.unconfirmed_cancels == 0
    assert near(run.ended_at, 5.0)
    assert len(h.journal.open_goals()) == 1  # kept: its cancel is sent again, a late acceptance cancelled


def test_publish_keep_for_late_joiners_hands_on_the_last_message_only(h):
    """The cached publisher outlives its runs: a late subscriber gets the last message, not the
    ones of earlier runs (6.5)."""
    for text in ("first", "second"):
        node = leaf("Publish", "P", Topic=f"{h.ns}/cmd", Type="std_msgs/msg/String", Message={"data": text},
                    Keep_For_Late_Joiners=True)
        assert h.finish(h.run(bundle(node))).outcome == Outcome.SUCCEEDED
    received = []
    late = QoSProfile(history=HistoryPolicy.KEEP_LAST, depth=10, reliability=ReliabilityPolicy.RELIABLE,
                      durability=DurabilityPolicy.TRANSIENT_LOCAL)
    h.peer.create_subscription(String, f"{h.ns}/cmd", lambda message: received.append(message.data), late)
    h.spin_until(lambda: received, 3.0)
    h.spin_for(0.5)
    assert received == ["second"]
