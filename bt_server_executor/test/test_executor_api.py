"""Appendix B.9: the executor and the tree API (design 4, 8), against executor processes."""

from __future__ import annotations

import json
import shutil
import signal
import time
import uuid
from pathlib import Path

import pytest

pytest.importorskip("rclpy")
pytest.importorskip("bt_server_interfaces")

from action_msgs.msg import GoalStatus  # noqa: E402
from action_msgs.srv import CancelGoal  # noqa: E402
from bt_server_interfaces.msg import NodeDetails, RunInfo, TreeStatus  # noqa: E402
from bt_server_interfaces.srv import Apply, GetLog, GetSnapshot  # noqa: E402
from executor_helpers import FibServer, goal, leaf, ros, start_executor, tree, unique_ns  # noqa: E402,F401
from rclpy.qos import DurabilityPolicy  # noqa: E402
from unique_identifier_msgs.msg import UUID  # noqa: E402

from bt_server_core.values import dumps_json, loads_json  # noqa: E402
from bt_server_executor.qos import DETAILS_QOS, RUN_QOS, STATUS_QOS, STATUS_SUB_QOS  # noqa: E402

pytestmark = pytest.mark.ros

SUCCEEDED, CANCELED, ABORTED = GoalStatus.STATUS_SUCCEEDED, GoalStatus.STATUS_CANCELED, GoalStatus.STATUS_ABORTED


def stamp(msg) -> float:
    """A message's ``stamp`` in seconds since the epoch (the ROS clock is the system clock here)."""
    return msg.stamp.sec + msg.stamp.nanosec / 1e9


def run_id_of(handle) -> bytes:
    return bytes(bytearray(handle.goal_id.uuid))


def finish(ros, handle, timeout: float = 30.0):
    """The result of a goal: (goal status, result message)."""
    response = ros.result_of(handle.get_result_async(), timeout)
    return response.status, response.result


def wait_status(ros, recorder, run_id: bytes, predicate, timeout: float = 15.0):
    def found():
        for msg in reversed(list(recorder.messages)):
            if bytes(bytearray(msg.run_id.uuid)) == run_id and predicate(msg):
                return msg
        return None

    return ros.wait(found, timeout)


def test_X_01_each_way_of_ending_a_run_gives_its_goal_state_and_outcome(ros, start_executor):
    executor = start_executor("x01")
    client = ros.action_client(executor.ns)
    status = ros.record(TreeStatus, f"{executor.name}/status", STATUS_SUB_QOS)
    cases = [
        ("Root succeeds", tree(leaf("TestSucceed")), None, SUCCEEDED, 1),
        ("Root fails", tree(leaf("TestFail")), None, ABORTED, 2),
        ("Stop", tree(leaf("TestHold")), "cancel", CANCELED, 3),
        ("Stop node", tree(leaf("Stop")), None, ABORTED, 3),
        ("tick raises", tree(leaf("TestBreakTick")), None, ABORTED, 4),
    ]
    try:
        for name, bundle, action, goal_state, outcome in cases:
            handle = ros.send(client, goal(bundle, label=name.replace(" ", "_")))
            if action == "cancel":
                assert wait_status(ros, status, run_id_of(handle), lambda m: m.state == TreeStatus.STATE_RUNNING)
                ros.cancel(handle)
            state, result = finish(ros, handle)
            assert (state, result.outcome) == (goal_state, outcome), name
            shown = wait_status(ros, status, run_id_of(handle),
                                lambda m: m.state == TreeStatus.STATE_IDLE and m.outcome != 0)
            assert shown and shown.outcome == outcome, name
        assert "tick" in result.message.lower()
    finally:
        status.close()
        client.destroy()


def test_X_02_status_is_a_heartbeat_while_nothing_changes(ros, start_executor):
    executor = start_executor("x02")
    client = ros.action_client(executor.ns)
    status = ros.record(TreeStatus, f"{executor.name}/status", STATUS_SUB_QOS)
    try:
        handle = ros.send(client, goal(tree(leaf("TestHold"))))
        assert wait_status(ros, status, run_id_of(handle), lambda m: m.state == TreeStatus.STATE_RUNNING)
        time.sleep(0.3)
        start = time.monotonic()
        time.sleep(5.0)
        count = len(status.since(start, start + 5.0))
        assert 9 <= count <= 11, count
        ros.cancel(handle)
        finish(ros, handle)
    finally:
        status.close()
        client.destroy()


def test_X_03_a_thousand_changes_a_second_give_at_most_31_status_messages(ros, start_executor):
    executor = start_executor("x03")
    client = ros.action_client(executor.ns)
    status = ros.record(TreeStatus, f"{executor.name}/status", STATUS_QOS)
    try:
        bundle = tree(leaf("TestSucceed"), config={"repeat": True, "tick_interval_ms": 1, "log_level": 1})
        handle = ros.send(client, goal(bundle))
        assert wait_status(ros, status, run_id_of(handle), lambda m: m.state == TreeStatus.STATE_RUNNING)
        start = time.monotonic()
        time.sleep(1.0)
        count = len(status.since(start, start + 1.0))
        request = Apply.Request(ops_json=dumps_json([{"op": "doc.set_config", "config": {"repeat": False}}]))
        assert ros.call(Apply, f"{executor.name}/apply", request).result == Apply.Response.APPLIED
        state, result = finish(ros, handle)
        assert state == SUCCEEDED
        assert result.loops_completed >= 250, result.loops_completed  # >= 1,000 status changes a second
        assert count <= 31, count
        last = ros.wait(lambda: (m := status.last()) is not None and m.state == TreeStatus.STATE_IDLE and m, 3.0)
        assert last.outcome == 1
        assert bytes(last.node_status) == bytes([2, 2])
    finally:
        status.close()
        client.destroy()


def test_X_04_late_joiners_get_the_last_run_and_status_at_once(ros, start_executor):
    executor = start_executor("x04")
    fresh = start_executor("x04_fresh")
    client = ros.action_client(executor.ns)
    ros.action_client(fresh.ns).destroy()
    try:
        handle = ros.send(client, goal(tree(leaf("TestHold"))))
        time.sleep(1.0)  # the middle of the run
        subscribed = time.time()
        run = ros.record(RunInfo, f"{executor.name}/run", RUN_QOS)
        status = ros.record(TreeStatus, f"{executor.name}/status", STATUS_SUB_QOS)
        assert ros.wait(lambda: run.messages and status.messages, 20.0)
        # "at once": the first message delivered is the one kept for late joiners, published
        # before the subscription existed (~/run is published only when a run starts)
        assert bytes(bytearray(run.messages[0].run_id.uuid)) == run_id_of(handle)
        assert run.messages[0].node_paths and run.messages[0].bundle_json
        assert stamp(run.messages[0]) < subscribed
        assert bytes(bytearray(status.messages[0].run_id.uuid)) == run_id_of(handle)
        assert status.messages[0].state == TreeStatus.STATE_RUNNING
        for topic in ("run", "status"):  # both keep their last message for late joiners (4.1)
            offered = ros.node.get_publishers_info_by_topic(f"{executor.name}/{topic}")
            assert [info.qos_profile.durability for info in offered] == [DurabilityPolicy.TRANSIENT_LOCAL]
        subscribed = time.time()
        never = ros.record(RunInfo, f"{fresh.name}/run", RUN_QOS)
        assert ros.wait(lambda: never.messages, 20.0)
        assert not any(never.messages[0].run_id.uuid) and never.messages[0].bundle_json == ""
        assert stamp(never.messages[0]) < subscribed  # the empty RunInfo of its start-up
        for recorder in (run, status, never):
            recorder.close()
        ros.cancel(handle)
        finish(ros, handle)
    finally:
        client.destroy()


def test_X_05_a_client_that_misses_details_sees_the_gap_and_repairs_it_with_a_snapshot(ros, start_executor):
    executor = start_executor("x05")
    client = ros.action_client(executor.ns)
    received: list = []
    dropped: list = []

    def on_details(msg: NodeDetails) -> None:
        if len(dropped) < 3 and len(received) >= 2:
            dropped.append(msg.seq)  # this client misses three messages
            return
        received.append(msg)

    subscription = ros.node.create_subscription(NodeDetails, f"{executor.name}/details", on_details, DETAILS_QOS,
                                                callback_group=ros.group)
    try:
        assert ros.wait(lambda: subscription.get_publisher_count() > 0, 20.0)
        handle = ros.send(client, goal(tree(leaf("TestDetails", Count=12, Period=0.15))))
        assert ros.wait(lambda: len(received) >= 4, 20.0)
        seqs = [msg.seq for msg in received]
        gap = next((i for i in range(1, len(seqs)) if seqs[i] != seqs[i - 1] + 1), None)
        assert gap is not None, seqs  # the gap is seen in the numbers
        snapshot = ros.call(GetSnapshot, f"{executor.name}/get_snapshot", GetSnapshot.Request())
        mirror = {(item["i"], item["kind"]): item["value"] for item in loads_json(snapshot.details_json)}

        def settled():  # the leaf has stopped changing its details, and the client has all updates
            final = ros.call(GetSnapshot, f"{executor.name}/get_snapshot", GetSnapshot.Request())
            done = {"n": 12} in [item["value"] for item in loads_json(final.details_json)]
            return final if done and received[-1].seq >= final.details_seq else None

        final = ros.wait(settled, 20.0, period=0.2)
        assert final is not None
        for msg in list(received):
            if msg.seq > snapshot.details_seq:
                mirror.update({(item["i"], item["kind"]): item["value"] for item in loads_json(msg.details_json)})
        assert mirror == {(item["i"], item["kind"]): item["value"] for item in loads_json(final.details_json)}
        assert mirror[(1, "feedback")] == {"n": 12}
        ros.cancel(handle)
        finish(ros, handle)
    finally:
        ros.node.destroy_subscription(subscription)
        client.destroy()


def test_X_06_get_log_returns_5000_records_in_order_over_several_calls(ros, start_executor):
    executor = start_executor("x06")
    client = ros.action_client(executor.ns)
    try:
        handle = ros.send(client, goal(tree(leaf("TestLogMany", Count=5000))))
        state, _ = finish(ros, handle, 30.0)
        assert state == SUCCEEDED
        records, seq, calls = [], 0, 0
        while True:
            request = GetLog.Request(run_id=UUID(uuid=list(run_id_of(handle))), from_seq=seq, max_records=1000)
            response = ros.call(GetLog, f"{executor.name}/get_log", request)
            calls += 1
            if not response.records:
                break
            assert response.next_seq == seq + len(response.records)
            records.extend(response.records)
            seq = response.next_seq
        lines = [record.text for record in records if record.text.startswith("line ")]
        assert lines == [f"line {number}" for number in range(5000)]
        assert calls >= 6
    finally:
        client.destroy()


def fib_tree(action: str) -> dict:
    return tree(leaf("TestFib", Action=action))


def test_X_07_a_restarted_executor_cancels_the_goal_its_killed_predecessor_left(ros, start_executor, tmp_path):
    ns = unique_ns("x07")
    server = FibServer(ros, f"{ns}_server/fib")
    executor = start_executor("x07", ns=ns, folder=tmp_path / "x07")
    client = ros.action_client(ns)
    try:
        ros.send(client, goal(fib_tree(server.name)))
        assert ros.wait(lambda: server.goals, 10.0)
        goal_id = server.goals[0][0]
        executor.kill()
        assert not server.cancels
        start_executor("x07", ns=ns, folder=tmp_path / "x07")
        assert ros.wait(lambda: server.cancelled(goal_id), 15.0), executor.text()
    finally:
        client.destroy()
        server.close()


def test_X_08_a_restarted_executor_does_not_cancel_the_goals_of_another(ros, start_executor, tmp_path):
    a_ns, b_ns = unique_ns("x08a"), unique_ns("x08b")
    server = FibServer(ros, f"{a_ns}_server/fib")
    homes = [Path.home() / ".ros" / "bt_server" / f"{ns.strip('/')}_bt_executor" for ns in (a_ns, b_ns)]
    a = start_executor("x08a", ns=a_ns, own_files=False)  # default journal files: one per executor name
    b = start_executor("x08b", ns=b_ns, own_files=False)
    client = ros.action_client(a_ns)
    ros.action_client(b_ns).destroy()
    try:
        handle = ros.send(client, goal(fib_tree(server.name)))
        assert ros.wait(lambda: server.goals, 10.0)
        b.kill()
        restarted = start_executor("x08b", ns=b_ns, own_files=False, folder=b.folder)
        ros.action_client(b_ns).destroy()
        time.sleep(4.0)
        assert server.cancels == [], restarted.text()
        ros.cancel(handle)
        state, _ = finish(ros, handle)
        assert state == CANCELED
        assert [goal_id for goal_id, _ in server.cancels] == [server.goals[0][0]]
    finally:
        client.destroy()
        server.close()
        a.stop()
        for home in homes:
            shutil.rmtree(home, ignore_errors=True)


def test_X_09_sigint_cancels_the_running_action_before_the_process_ends(ros, start_executor, tmp_path):
    ns = unique_ns("x09")
    server = FibServer(ros, f"{ns}_server/fib")
    executor = start_executor("x09", ns=ns)
    client = ros.action_client(ns)
    try:
        ros.send(client, goal(fib_tree(server.name)))
        assert ros.wait(lambda: server.goals, 10.0)
        sent = time.monotonic()
        executor.signal(signal.SIGINT)
        executor.proc.wait(timeout=10)
        ended = time.monotonic()
        cancelled = server.cancelled(server.goals[0][0])
        assert cancelled is not None and cancelled < ended, executor.text()
        assert ended - sent < 5.0, ended - sent  # within the cancel timeout (5 s)
        assert executor.proc.returncode == 0, executor.text()
    finally:
        client.destroy()
        server.close()


def watchdog_case(ros, start_executor, tag: str, after_three) -> None:
    """A run with a watchdog of 2 s on a topic that shows the run going until 3 s, then
    ``after_three(publisher, run_uuid)`` publishes (or not) until the end."""
    executor = start_executor(tag)
    client = ros.action_client(executor.ns)
    topic = f"{executor.ns}_parent/bt_executor/status"
    publisher = ros.node.create_publisher(TreeStatus, topic, STATUS_QOS)
    watched = uuid.uuid4().bytes
    stop = [False]
    started = [0.0]

    def beat() -> None:
        now = time.monotonic()
        if stop[0] or not started[0]:
            return
        if now - started[0] < 3.0:
            publisher.publish(TreeStatus(run_id=UUID(uuid=list(watched)), state=TreeStatus.STATE_RUNNING))
        else:
            after_three(publisher, watched)

    timer = ros.node.create_timer(0.1, beat, callback_group=ros.group)
    try:
        time.sleep(0.5)
        started[0] = time.monotonic()
        handle = ros.send(client, goal(tree(leaf("TestHold")), watchdog_topic=topic,
                                       watchdog_run_id=UUID(uuid=list(watched)), watchdog_timeout_s=2.0))
        state, result = finish(ros, handle, 15.0)
        elapsed = time.monotonic() - started[0]
        assert (state, result.outcome) == (ABORTED, 3)
        assert 4.6 <= elapsed <= 6.0, elapsed
        assert "watchdog" in result.message
    finally:
        stop[0] = True
        ros.node.destroy_timer(timer)
        ros.node.destroy_publisher(publisher)
        client.destroy()


def test_X_10_a_silent_watchdog_stops_the_run(ros, start_executor):
    watchdog_case(ros, start_executor, "x10", lambda publisher, watched: None)


def test_X_11_messages_of_another_run_or_idle_do_not_count(ros, start_executor):
    other = uuid.uuid4().bytes
    watchdog_case(ros, start_executor, "x11a", lambda publisher, watched: publisher.publish(
        TreeStatus(run_id=UUID(uuid=list(other)), state=TreeStatus.STATE_RUNNING)))
    watchdog_case(ros, start_executor, "x11b", lambda publisher, watched: publisher.publish(
        TreeStatus(run_id=UUID(uuid=list(watched)), state=TreeStatus.STATE_IDLE)))


def test_X_12_a_run_is_stopped_by_its_id_through_the_cancel_service(ros, start_executor):
    executor = start_executor("x12")
    client = ros.action_client(executor.ns)
    status = ros.record(TreeStatus, f"{executor.name}/status", STATUS_SUB_QOS)
    try:
        handle = ros.send(client, goal(tree(leaf("TestHold"))))
        assert wait_status(ros, status, run_id_of(handle), lambda m: m.state == TreeStatus.STATE_RUNNING)
        response = ros.cancel_by_id(f"{executor.name}/execute", run_id_of(handle))  # another client, by id
        assert response.return_code == 0
        state, result = finish(ros, handle)
        assert (state, result.outcome) == (CANCELED, 3)
    finally:
        status.close()
        client.destroy()


def test_X_09b_shutdown_waits_for_the_longest_cancel_timeout_of_a_node(ros, start_executor, tmp_path):
    """8.2 with a node whose cancel timeout (9 s) is longer than the tree's (5 s): the cancel,
    confirmed after 6.5 s, is waited for; the goal gets its result; the journal closes the goal."""
    ns = unique_ns("x09b")
    server = FibServer(ros, f"{ns}_server/fib", cancel_delay=6.5)
    executor = start_executor("x09b", ns=ns)
    client = ros.action_client(ns)
    try:
        fib = leaf("TestFib", Action=server.name)
        fib["cancel_timeout_ms"] = 9000
        handle = ros.send(client, goal(tree(fib)))
        result_future = handle.get_result_async()  # asked for now: the executor is gone afterwards
        assert ros.wait(lambda: server.goals, 10.0)
        sent = time.monotonic()
        executor.signal(signal.SIGTERM)
        executor.proc.wait(timeout=15)
        ended = time.monotonic()
        assert server.confirmed and server.confirmed[0][1] < ended, executor.text()
        assert 6.5 <= ended - sent < 9.0 + 1.5, ended - sent
        response = ros.result_of(result_future, 3.0)
        assert (response.status, response.result.outcome) == (ABORTED, 3)
        lines = [json.loads(line) for line in (executor.folder / "goals.jsonl").read_text().splitlines()]
        assert {item["goal"] for item in lines if item["e"] == "sent"} <= {item["goal"] for item in lines if item["e"] == "done"}
    finally:
        client.destroy()
        server.close()


def test_X_09c_sigterm_while_the_tree_ticks_fast_ends_the_process(ros, start_executor):
    """The signal handler takes no lock the interrupted main thread may hold (it only counts)."""
    for attempt in range(3):
        executor = start_executor(f"x09c{attempt}")
        client = ros.action_client(executor.ns)
        try:
            bundle = tree(leaf("TestSucceed"), config={"repeat": True, "tick_interval_ms": 1, "log_level": 1})
            ros.send(client, goal(bundle))
            time.sleep(0.5)
            sent = time.monotonic()
            executor.signal(signal.SIGTERM)
            executor.proc.wait(timeout=15)  # a deadlock would hang here
            assert time.monotonic() - sent < 6.0
        finally:
            client.destroy()


def test_X_12b_a_goal_cancelled_before_it_starts_never_ticks(ros, start_executor):
    """A cancel that arrives between the acceptance and the start: canceled, outcome stopped, and
    no leaf has run (the Fibonacci server never gets a goal of that run)."""
    ns = unique_ns("x12b")
    server = FibServer(ros, f"{ns}_server/fib")
    executor = start_executor("x12b", ns=ns)
    client = ros.action_client(ns)
    canceller = ros.node.create_client(CancelGoal, f"{executor.name}/execute/_action/cancel_goal",
                                       callback_group=ros.group)
    assert canceller.wait_for_service(timeout_sec=10.0)
    early = 0
    try:
        for _ in range(5):
            goal_id = uuid.uuid4()
            sent = client.send_goal_async(goal(tree(leaf("TestFib", Action=server.name))),
                                          goal_uuid=UUID(uuid=list(goal_id.bytes)))
            request = CancelGoal.Request()
            request.goal_info.goal_id.uuid = list(goal_id.bytes)
            cancelled = canceller.call_async(request)  # right behind the goal
            handle = ros.result_of(sent)
            answered = ros.wait(cancelled.done, 10.0)
            if not answered or not cancelled.result().goals_canceling:
                # the cancel overtook the goal (two services, two paths) and found nothing to
                # cancel: the run starts normally and is cancelled now
                ros.cancel(handle)
            state, result = finish(ros, handle)
            assert (state, result.outcome) == (CANCELED, 3)
            if result.message == "Stopped before it started":
                early += 1
                time.sleep(0.3)
                assert len(server.goals) == 0, "a run cancelled before it started sent its goal"
            else:
                server.goals.clear()
        assert early >= 1  # the cancel did arrive before the start at least once
    finally:
        ros.node.destroy_client(canceller)
        client.destroy()
        server.close()


def test_X_12c_no_feedback_while_the_goal_is_canceling(ros, start_executor):
    """The state changes of a cancelled run (STOPPING, IDLE) are not sent as feedback of a goal
    that is canceling: rclpy would refuse them with a WARNING."""
    executor = start_executor("x12c")
    client = ros.action_client(executor.ns)
    status = ros.record(TreeStatus, f"{executor.name}/status", STATUS_SUB_QOS)
    try:
        feedback: list = []
        handle = ros.send(client, goal(tree(leaf("TestHold"))), feedback)
        assert wait_status(ros, status, run_id_of(handle), lambda m: m.state == TreeStatus.STATE_RUNNING)
        ros.cancel(handle)
        state, _ = finish(ros, handle)
        assert state == CANCELED
        time.sleep(0.3)
        assert "not in executing state" not in executor.text()
        assert all(item.state == TreeStatus.STATE_RUNNING for item in feedback), feedback
    finally:
        status.close()
        client.destroy()


def test_X_07b_a_journal_line_with_an_invalid_action_name_is_dropped(ros, start_executor, tmp_path):
    """A damaged journal line cannot keep the executor from starting (16: a crash loop)."""
    folder = tmp_path / "x07b"
    folder.mkdir()
    bad, good = uuid.uuid4().hex, uuid.uuid4().hex
    (folder / "goals.jsonl").write_text(
        json.dumps({"e": "sent", "action": "/rv6/x y", "goal": bad, "run": "", "time": 1.0}) + "\n"
        + json.dumps({"e": "sent", "action": "/rv6/fine", "goal": good, "run": "", "time": 1.0}) + "\n",
        encoding="utf-8")
    executor = start_executor("x07b", folder=folder)
    ros.action_client(executor.ns).destroy()
    assert ros.wait(lambda: "cannot be cancelled" in executor.text(), 20.0), executor.text()
    time.sleep(1.0)  # it keeps running
    assert executor.proc.poll() is None, executor.text()
    lines = [json.loads(line) for line in (folder / "goals.jsonl").read_text().splitlines()]
    assert {"e": "done", "goal": bad} in lines
    assert not any(item["e"] == "done" and item["goal"] == good for item in lines)  # still unsettled
