"""``RosLink`` (the gateway's client of the tree API) against real executor processes, and
``run_tree``."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time

import pytest

pytest.importorskip("rclpy")
pytest.importorskip("bt_server_interfaces")

from executor_helpers import child_env, goal, leaf, new_id, ros, start_executor, tree  # noqa: E402,F401

from bt_server_core.files import write_tree_file  # noqa: E402
from bt_server_gateway.link import APPLIED, NO_RUN, CommandRefused, GoalRejected, LinkStatus  # noqa: E402
from bt_server_gateway.ros_link import RosLinkFactory  # noqa: E402
from bt_server_gateway.settings import Settings  # noqa: E402

pytestmark = pytest.mark.ros


class Listener:
    """Records what a link reports and mirrors the blackboard and the details."""

    def __init__(self) -> None:
        self.online: list[bool] = []
        self.runs: list = []
        self.statuses: list[LinkStatus] = []
        self.catalogs: list = []
        self.results: list = []
        self.records: list = []
        self.blackboard: dict[str, dict] = {}
        self.details: dict[tuple[int, str], object] = {}
        self.full_updates = 0

    def on_online(self, online: bool) -> None:
        self.online.append(online)

    def on_run(self, run) -> None:
        self.runs.append(run)

    def on_status(self, status: LinkStatus) -> None:
        self.statuses.append(status)

    def on_blackboard(self, run_id: str, seq: int, full: bool, update: dict) -> None:
        if full:
            self.full_updates += 1
            self.blackboard = {}
        for entry_id in update["removed"]:
            self.blackboard.pop(entry_id, None)
        for entry in update["entries"]:
            self.blackboard[entry["id"]] = entry

    def on_details(self, run_id: str, seq: int, details: list[dict]) -> None:
        for item in details:
            if item["value"] is None:
                self.details.pop((item["i"], item["kind"]), None)
            else:
                self.details[(item["i"], item["kind"])] = item["value"]

    def on_log(self, run_id: str, records: list) -> None:
        self.records.extend(records)

    def on_result(self, result) -> None:
        self.results.append(result)

    def on_catalog(self, types: list[dict]) -> None:
        self.catalogs.append(types)

    def value(self, name: str):
        return next((entry["value"] for entry in self.blackboard.values() if entry["name"] == name), None)


async def until(predicate, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value or time.monotonic() >= deadline:
            return value
        await asyncio.sleep(0.02)


@pytest.fixture
async def factory(tmp_path):
    made = RosLinkFactory(Settings(trees_dir=tmp_path))
    found: list[list[str]] = []
    await made.start(found.append)
    made.found = found
    yield made
    await made.close()


async def started_link(factory, name: str) -> tuple:
    link = factory.link(name)
    listener = Listener()
    link.subscribe(listener)
    await link.start()
    assert await until(lambda: link.online and listener.catalogs, 20.0)
    return link, listener


async def test_ros_link_discovery_finds_executors(factory, start_executor):
    executor = start_executor("rl_disc")
    assert await until(lambda: factory.found and executor.name in factory.found[-1], 20.0)
    assert factory.link(executor.name) is factory.link(executor.name.strip("/"))
    executor.stop()
    assert await until(lambda: executor.name not in factory.found[-1], 20.0)


async def test_ros_link_execute_statuses_blackboard_apply_stop_and_no_run(factory, start_executor):
    executor = start_executor("rl_exec")
    link, listener = await started_link(factory, executor.name)
    assert any(item["type"] == "TestHold" for item in listener.catalogs[-1])
    count_id = new_id()
    blackboard = [{"id": count_id, "name": "count", "type": "Integer", "value": 0}]
    bundle = tree(leaf("Set", ValueName="count", CompareTo=None, LiteralValue=5), leaf("TestHold"))
    run_id = await link.execute(bundle, blackboard, "rl_exec", "pytest")
    assert len(run_id) == 32
    assert await until(lambda: any(s.run_id == run_id and s.state == 1 for s in listener.statuses))
    assert await until(lambda: listener.value("count") == 5)
    assert listener.runs[-1].run_id == run_id and len(listener.runs[-1].node_paths) == 3
    assert await link.apply([{"op": "bb.set_value", "id": count_id, "value": 7}]) == (APPLIED, "")
    assert await until(lambda: listener.value("count") == 7)
    snapshot = await link.snapshot()
    assert snapshot["run"].run_id == run_id and snapshot["status"].state == 1
    assert {entry["name"]: entry["value"] for entry in snapshot["blackboard"]}["count"] == 7
    await link.stop(run_id)
    assert await until(lambda: listener.results, 20.0)
    result = listener.results[0]
    assert (result.run_id, result.goal_status, result.outcome) == (run_id, "canceled", 3)
    assert [entry["value"] for entry in result.blackboard if entry["name"] == "count"] == [7]
    assert await until(lambda: listener.statuses[-1].state == 0 and listener.statuses[-1].outcome == 3)
    assert (await link.apply([{"op": "bb.set_value", "id": count_id, "value": 8}]))[0] == NO_RUN
    assert await until(lambda: listener.records and listener.records[0].seq == 0)
    assert [record.seq for record in listener.records] == list(range(len(listener.records)))
    records = await link.get_log(run_id, 0, 1000)
    assert [record.text for record in records][: len(listener.records)] == [r.text for r in listener.records]
    await link.close()


async def test_ros_link_stops_a_run_it_did_not_start_by_its_id(factory, start_executor, ros):
    executor = start_executor("rl_stop")
    link, listener = await started_link(factory, executor.name)
    client = ros.action_client(executor.ns)
    try:
        handle = await asyncio.to_thread(ros.send, client, goal(tree(leaf("TestHold")), label="other"))
        run_id = bytes(bytearray(handle.goal_id.uuid)).hex()
        assert await until(lambda: listener.runs and listener.runs[-1].run_id == run_id)
        assert listener.runs[-1].requested_by == "pytest" and listener.runs[-1].bundle is not None
        await link.stop(run_id)
        response = await asyncio.to_thread(ros.result_of, handle.get_result_async())
        assert response.status == 5 and response.result.outcome == 3  # canceled, stopped
    finally:
        client.destroy()
        await link.close()


async def test_ros_link_repairs_a_gap_in_the_details_with_a_snapshot(factory, start_executor):
    executor = start_executor("rl_gap")
    link = factory.link(executor.name)
    listener = Listener()
    link.subscribe(listener)
    received = [0]
    original = link._on_details_msg

    def lossy(msg) -> None:  # this gateway misses three details messages
        received[0] += 1
        if 3 <= received[0] <= 5:
            return
        original(msg)

    link._on_details_msg = lossy
    await link.start()
    assert await until(lambda: link.online, 20.0)
    run_id = await link.execute(tree(leaf("TestDetails", Count=12, Period=0.15)), [], "rl_gap", "pytest")
    assert await until(lambda: received[0] >= 12, 20.0)
    await asyncio.sleep(1.0)
    snapshot = await link.snapshot()
    assert listener.full_updates >= 2  # the start of the run, and the repair
    assert listener.details == {(item["i"], item["kind"]): item["value"] for item in snapshot["details"]}
    assert listener.details[(1, "feedback")] == {"n": 12}
    await link.stop(run_id)
    assert await until(lambda: listener.results)
    await link.close()


def test_run_tree_runs_a_tree_file_and_prints_the_outcome(start_executor, tmp_path):
    executor = start_executor("run_tree")
    bundle = tree(leaf("TestSucceed"), leaf("Set", ValueName="flag", CompareTo=None, LiteralValue=True))
    data = {"format": "behavior_tree_widget", "version": 1, **bundle["trees"]["main"]}
    write_tree_file(tmp_path / "demo_BT.json", data)
    (tmp_path / "demo_BB.json").write_text(
        '{"format": "behavior_tree_widget_blackboard", "version": 1, '
        '"blackboard": [{"name": "flag", "type": "Bool", "value": false}]}', encoding="utf-8")
    done = subprocess.run([sys.executable, "-m", "bt_server_executor.run_tree", str(tmp_path / "demo_BT.json"),
                           "--executor", executor.name, "--log"], env=child_env(), capture_output=True, text=True,
                          timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "run_tree: succeeded" in done.stdout
    assert "Execution started" in done.stdout


async def test_ros_link_validate_describe_pause_reset_and_rejections(factory, start_executor):
    executor = start_executor("rl_cmds")
    link, listener = await started_link(factory, executor.name)
    findings = await link.validate(tree(leaf("NoSuchType")), [])
    assert [item["severity"] for item in findings] == ["error"] and "unknown node type" in findings[0]["text"]
    unreadable = await link.validate({"format": "behavior_tree_bundle", "trees": {}}, [])
    assert unreadable[0]["path"] == "" and unreadable[0]["severity"] == "error"
    assert any(item["type"] == "Sequence" for item in (await link.describe("node_types", {}))["types"])
    with pytest.raises(CommandRefused):
        await link.pause(True)  # refused while idle
    await link.reset()  # no effect while idle
    with pytest.raises(GoalRejected, match="cannot be read"):
        await link.execute({"format": "behavior_tree_bundle", "root": "x", "trees": {}}, [], "bad", "pytest")
    run_id = await link.execute(tree(leaf("TestHold")), [], "first", "browser 10.0.0.7")
    assert await until(lambda: listener.statuses and listener.statuses[-1].state == 1)
    with pytest.raises(GoalRejected, match="busy: 'first' started by browser 10.0.0.7"):
        await link.execute(tree(leaf("TestHold")), [], "second", "pytest")
    await link.pause(True)
    assert await until(lambda: listener.statuses[-1].state == 2)
    await link.pause(False)
    assert await until(lambda: listener.statuses[-1].state == 1)
    await link.reset()  # a Reset during the run passes through RESTARTING and goes on
    assert await until(lambda: listener.statuses[-1].state == 1 and listener.statuses[-1].loop == 0)
    await link.stop(run_id)
    assert await until(lambda: listener.results and listener.results[0].goal_status == "canceled")
    await link.close()


async def test_ros_link_entries_carry_source_problems_while_they_last(factory, start_executor):
    executor = start_executor("rl_problem")
    link, listener = await started_link(factory, executor.name)
    level_id = new_id()
    source = {"topic": f"{executor.ns}_robot/level", "type": "std_msgs/msg/Float64", "field": "nope"}
    blackboard = [{"id": level_id, "name": "level", "type": "Double", "value": 0.0, "source": source}]
    run_id = await link.execute(tree(leaf("TestHold")), blackboard, "rl_problem", "pytest")
    assert await until(lambda: listener.blackboard.get(level_id, {}).get("problem"), 20.0)
    snapshot = await link.snapshot()
    assert [entry.get("problem") for entry in snapshot["blackboard"]] == [listener.blackboard[level_id]["problem"]]
    fixed = {**source, "field": "data"}
    assert (await link.apply([{"op": "bb.set_source", "id": level_id, "source": fixed}]))[0] == APPLIED
    assert await until(lambda: level_id in listener.blackboard and "problem" not in listener.blackboard[level_id], 20.0)
    assert all("problem" not in entry for entry in (await link.snapshot())["blackboard"])
    await link.stop(run_id)
    assert await until(lambda: listener.results)
    await link.close()


async def test_ros_link_describe_failure_answers_with_the_reason(factory, start_executor):
    """A question that fails on the executor's worker thread is answered with the reason at once."""
    executor = start_executor("rl_describe")
    link, _ = await started_link(factory, executor.name)
    start = time.monotonic()
    with pytest.raises(CommandRefused) as refused:
        await link.describe("interface", {"type": "os/msg/Thing"})
    assert time.monotonic() - start < 9.0  # below the link's own 10 s call timeout
    assert str(refused.value)
    await link.close()


async def test_ros_link_repair_reports_updates_newer_than_the_snapshot(factory, start_executor):
    """4.2: after a gap the snapshot is reported first, then the updates numbered above it, also
    those that arrived while the snapshot was asked for."""
    executor = start_executor("rl_order")
    link, listener = await started_link(factory, executor.name)
    count_id = new_id()
    run_id = await link.execute(tree(leaf("TestHold")), [{"id": count_id, "name": "c", "type": "Integer", "value": 0}],
                                "rl_order", "pytest")
    for value in (1, 2):
        await link.apply([{"op": "bb.set_value", "id": count_id, "value": value}])
        assert await until(lambda: listener.value("c") == value)
    original = link.snapshot

    async def slow_snapshot():
        snap = await original()
        await asyncio.sleep(0.6)
        return snap

    link.snapshot = slow_snapshot
    link._bb_seq -= 1  # as if the last update had been lost: the next one shows a gap
    await link.apply([{"op": "bb.set_value", "id": count_id, "value": 3}])  # reveals the gap
    await asyncio.sleep(0.25)
    await link.apply([{"op": "bb.set_value", "id": count_id, "value": 4}])  # arrives during the repair
    assert await until(lambda: listener.full_updates >= 2 and link._held is None)  # the repair is over
    assert await until(lambda: listener.value("c") == 4)
    await asyncio.sleep(0.5)
    assert listener.value("c") == 4  # nothing older came after it
    link.snapshot = original
    await link.stop(run_id)
    await link.close()


class DetailRecorder(Listener):
    def __init__(self) -> None:
        super().__init__()
        self.items: list[dict] = []

    def on_details(self, run_id: str, seq: int, details: list[dict]) -> None:
        super().on_details(run_id, seq, details)
        self.items.extend(details)


async def choice_run(factory, start_executor, tag: str):
    executor = start_executor(tag)
    link = factory.link(executor.name)
    listener = DetailRecorder()
    link.subscribe(listener)
    await link.start()
    assert await until(lambda: link.online, 20.0)
    node = leaf("TestChoice")
    run_id = await link.execute(tree(node), [], tag, "pytest")
    assert await until(lambda: listener.statuses and listener.statuses[-1].state == 1)
    return link, listener, node, run_id


async def test_ros_link_every_field_change_comes_back(factory, start_executor):
    """Two field edits of one node within 100 ms both come back as field details (9.3, G-05)."""
    link, listener, node, run_id = await choice_run(factory, start_executor, "rl_fields")
    for value in (2.5, 3.5):
        op = {"op": "node.set_field", "tree": "main", "id": node["id"], "field": "Speed", "value": value}
        assert (await link.apply([op]))[0] == APPLIED
    assert await until(lambda: len([i for i in listener.items if i["kind"] == "field"]) >= 2, 20.0)
    values = [item["value"]["value"] for item in listener.items if item["kind"] == "field"]
    assert values == [2.5, 3.5]
    await link.stop(run_id)
    await link.close()


async def test_ros_link_selection_change_comes_back(factory, start_executor):
    """node.set_selection through ~/apply comes back as a field detail with the selection."""
    link, listener, node, run_id = await choice_run(factory, start_executor, "rl_selection")
    op = {"op": "node.set_selection", "tree": "main", "id": node["id"], "field": "Mode", "index": 1}
    assert (await link.apply([op]))[0] == APPLIED
    assert await until(lambda: any(i["kind"] == "field" for i in listener.items), 20.0)
    detail = next(item for item in listener.items if item["kind"] == "field")
    assert detail["i"] == 1
    assert detail["value"] == {"tree": "main", "id": node["id"], "field": "Mode", "selection": 1}
    bad = {"op": "node.set_selection", "tree": "main", "id": node["id"], "field": "Mode", "index": 5}
    assert (await link.apply([bad]))[0] == 2  # REJECTED
    await link.stop(run_id)
    await link.close()
