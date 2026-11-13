"""Entries fed from ROS topics: Appendix B.7, S-01 to S-12 (design 7.4).

Each case runs a tree that keeps running (an Evaluation waiting on an entry that never comes
true) while a scripted publisher feeds one entry; times count from the first message unless a
case says otherwise.
"""

from __future__ import annotations

import sys
import time

import pytest

pytest.importorskip("rclpy")
pytest.importorskip("bt_server_interfaces.action")

from geometry_msgs.msg import Point, PoseStamped  # noqa: E402
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy  # noqa: E402
from sensor_msgs.msg import LaserScan  # noqa: E402
from std_msgs.msg import Bool, Float64, UInt32  # noqa: E402

from bt_server_core.engine import Outcome  # noqa: E402
from bt_server_core.treelog import LogLevel  # noqa: E402
from bt_server_executor.ros_validate import validate_ros  # noqa: E402
from ros_helpers import Harness, bundle, entry, evaluation  # noqa: E402

pytestmark = pytest.mark.ros

RELIABLE = QoSProfile(history=HistoryPolicy.KEEP_LAST, depth=10, reliability=ReliabilityPolicy.RELIABLE,
                      durability=DurabilityPolicy.VOLATILE)


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path, "sources")
    yield harness
    harness.close()


def hold() -> dict:
    """A tree that keeps running: it waits for ``hold``, which stays false."""
    return bundle(evaluation("hold", "==", True, wait=True, title="hold"))


HOLD = entry("hold", "Bool", False)


def source(topic: str, type_name: str, field: str = "", **settings) -> dict:
    return {"topic": topic, "type": type_name, "field": field, **settings}


class Watch:
    """The values an entry takes, with their times (a blackboard listener)."""

    def __init__(self, harness: Harness, run, name: str) -> None:
        self.values: list[tuple[float, object]] = []
        self._harness, self._board, self._name = harness, run.blackboard, name
        run.blackboard.add_listener(self._changed)

    def _changed(self) -> None:
        value = self._board.get(self._name)
        if self.fed() and (not self.values or self.values[-1][1] != value):
            self.values.append((self._harness.now(), value))

    def fed(self) -> bool:
        return self._board.freshness(self._name) == "ok"


def publisher(h: Harness, msg_type, topic: str, qos=RELIABLE):
    return h.keep(h.peer.create_publisher(msg_type, topic, qos))


def matched(h: Harness, pub) -> None:
    h.wait_for_graph(lambda: pub.get_subscription_count() >= 1)


def test_S_01_float64_into_a_double(h):
    pub = publisher(h, Float64, f"{h.ns}/value")
    h.wait_for_graph(lambda: h.node.get_publishers_info_by_topic(f"{h.ns}/value"))
    run = h.run(hold(), [HOLD, entry("x", "Double", 0.0, source(f"{h.ns}/value", "std_msgs/msg/Float64", "data"))])
    matched(h, pub)
    pub.publish(Float64(data=1.5))
    assert h.spin_until(lambda: run.blackboard.get("x") == 1.5, 3.0)
    assert run.blackboard.freshness("x") == "ok"


def test_S_02_one_number_from_inside_a_message(h):
    pose_pub = publisher(h, PoseStamped, f"{h.ns}/pose")
    scan_pub = publisher(h, LaserScan, f"{h.ns}/scan")
    run = h.run(hold(), [HOLD,
                         entry("x", "Double", 0.0, source(f"{h.ns}/pose", "geometry_msgs/msg/PoseStamped",
                                                          "pose.position.x")),
                         entry("r", "Double", 0.0, source(f"{h.ns}/scan", "sensor_msgs/msg/LaserScan", "ranges[0]"))])
    matched(h, pose_pub)
    matched(h, scan_pub)
    pose = PoseStamped()
    pose.pose.position.x = 4.25
    pose_pub.publish(pose)
    scan_pub.publish(LaserScan(ranges=[0.5, 7.0]))
    assert h.spin_until(lambda: run.blackboard.get("x") == 4.25 and run.blackboard.get("r") == 0.5, 3.0)


def test_S_03_whole_message_into_a_dictionary(h):
    pub = publisher(h, Point, f"{h.ns}/point")
    run = h.run(hold(), [HOLD, entry("p", "Dictionary", {}, source(f"{h.ns}/point", "geometry_msgs/msg/Point"))])
    matched(h, pub)
    pub.publish(Point(x=1.0, y=2.0, z=3.0))
    assert h.spin_until(lambda: run.blackboard.get("p") == {"x": 1.0, "y": 2.0, "z": 3.0}, 3.0)


def test_S_04_the_latest_message_is_not_lost_to_the_rate_limit(h):
    pub = publisher(h, Bool, f"{h.ns}/flag")
    run = h.run(hold(), [HOLD, entry("b", "Bool", True, source(f"{h.ns}/flag", "std_msgs/msg/Bool", "data",
                                                                max_rate_hz=2.0))])
    watch = Watch(h, run, "b")
    matched(h, pub)
    h.spin_for(0.2)
    start = h.now()
    pub.publish(Bool(data=False))
    h.spin_until(lambda: start + 0.1 <= h.now())
    pub.publish(Bool(data=True))
    h.spin_until(lambda: run.blackboard.get("b") is True and watch.fed(), 2.0)
    h.spin_for(0.5)
    values = [(at - start, value) for at, value in watch.values]
    assert values[0][1] is False and values[0][0] < 0.1
    assert values[-1][1] is True and 0.45 <= values[-1][0] <= 0.6, values
    assert [value for _, value in values] == [False, True]


def test_S_05_no_data_until_the_first_message(h):
    pub = publisher(h, Float64, f"{h.ns}/battery")
    battery = entry("battery", "Double", 0.0, source(f"{h.ns}/battery", "std_msgs/msg/Float64", "data"))
    check = h.run(bundle(evaluation("battery", ">=", 20.0)), [battery])
    h.finish(check)
    assert check.result.outcome == Outcome.FAILED and check.sink.error_of(1) == "no recent data"
    assert check.sink.status_times(1, "F")[0] < 0.1
    run = h.run(bundle(evaluation("battery", ">=", 20.0, wait=True)), [battery])
    matched(h, pub)
    h.spin_to(1.3)
    assert run.blackboard.freshness("battery") == "nodata"
    assert run.sink.letters() == "R R"
    pub.publish(Float64(data=25.0))
    published = h.now()
    h.finish(run)
    assert run.result.outcome == Outcome.SUCCEEDED
    assert published <= run.sink.status_times(1, "S")[0] <= published + 0.2


def test_S_06_max_age(h):
    pub = publisher(h, Float64, f"{h.ns}/value")
    the_bundle = bundle(evaluation("x", "==", 1.5), config={"repeat": True, "tick_interval_ms": 1000})
    run = h.run(the_bundle, [entry("x", "Double", 0.0, source(f"{h.ns}/value", "std_msgs/msg/Float64", "data",
                                                               max_age_s=2.0))])
    matched(h, pub)
    pub.publish(Float64(data=1.5))
    h.spin_until(lambda: run.blackboard.freshness("x") == "ok", 2.0)
    fed = h.now()
    h.spin_to(3.3)
    statuses = dict((round(at), letters) for at, letters in run.sink.timeline if abs(at - round(at)) < 0.15)
    assert statuses[1] == "S S"
    assert statuses[3] == "F F"
    assert "no recent data" in [value for at, value in run.sink.detail_values(1, "error") if at > 2.5]
    assert any(fed + 2.0 - 0.05 <= at <= fed + 2.15 for at in run.tick_requests), (fed, run.tick_requests)
    h.stop(run)


def test_S_07_late_joiner(h):
    latched = QoSProfile(history=HistoryPolicy.KEEP_LAST, depth=1, reliability=ReliabilityPolicy.RELIABLE,
                         durability=DurabilityPolicy.TRANSIENT_LOCAL)
    pub = publisher(h, Float64, f"{h.ns}/latched", latched)
    pub.publish(Float64(data=8.0))
    h.wait_for_graph(lambda: h.node.get_publishers_info_by_topic(f"{h.ns}/latched"))
    run = h.run(hold(), [HOLD, entry("x", "Double", 0.0, source(f"{h.ns}/latched", "std_msgs/msg/Float64", "data"))])
    assert h.spin_until(lambda: run.blackboard.get("x") == 8.0, 1.0)


def test_S_08_best_effort_publisher(h):
    best_effort = QoSProfile(history=HistoryPolicy.KEEP_LAST, depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)
    pub = publisher(h, Float64, f"{h.ns}/be", best_effort)
    h.wait_for_graph(lambda: h.node.get_publishers_info_by_topic(f"{h.ns}/be"))
    run = h.run(hold(), [HOLD, entry("x", "Double", 0.0, source(f"{h.ns}/be", "std_msgs/msg/Float64", "data"))])
    matched(h, pub)
    h.spin_for(0.2)
    pub.publish(Float64(data=3.0))
    assert h.spin_until(lambda: run.blackboard.get("x") == 3.0, 2.0)


def test_S_09_publisher_appears_during_the_run(h):
    run = h.run(hold(), [HOLD, entry("x", "Double", 0.0, source(f"{h.ns}/later", "std_msgs/msg/Float64", "data"))])
    h.spin_to(2.0)
    pub = publisher(h, Float64, f"{h.ns}/later")
    h.spin_to(3.0)
    matched(h, pub)
    pub.publish(Float64(data=6.0))
    assert h.spin_until(lambda: run.blackboard.get("x") == 6.0, 2.0)


def test_S_10_values_beyond_32_bits_are_skipped(h):
    pub = publisher(h, UInt32, f"{h.ns}/count")
    run = h.run(hold(), [HOLD, entry("n", "Integer", 1, source(f"{h.ns}/count", "std_msgs/msg/UInt32", "data"))])
    matched(h, pub)
    pub.publish(UInt32(data=3_000_000_000))
    h.spin_for(0.6)
    assert run.blackboard.get("n") == 1
    warnings = [text for _, level, text in h.logs if level == LogLevel.WARNING and "3000000000" in text]
    assert len(warnings) == 1, h.logs
    pub.publish(UInt32(data=3_000_000_000))
    pub.publish(UInt32(data=5))
    assert h.spin_until(lambda: run.blackboard.get("n") == 5, 2.0)
    assert len([text for _, level, text in h.logs if level == LogLevel.WARNING and "does not fit" in text]) == 1


@pytest.mark.slow
def test_S_11_rate_limit_over_ten_seconds(h):
    pub = publisher(h, Float64, f"{h.ns}/fast")
    run = h.run(hold(), [HOLD, entry("x", "Double", 0.0, source(f"{h.ns}/fast", "std_msgs/msg/Float64", "data",
                                                                 max_rate_hz=2.0))])
    watch = Watch(h, run, "x")
    matched(h, pub)
    h.spin_for(0.2)
    start = time.monotonic()
    count = 0
    while time.monotonic() - start < 10.0:
        count += 1
        pub.publish(Float64(data=float(count)))
        h.spin_until(lambda: time.monotonic() >= start + count * 0.1)
    h.spin_for(0.6)
    changes = len(watch.values)
    assert 18 <= changes <= 22, changes
    assert watch.values[-1][1] == float(count)


def test_S_12_type_that_is_no_interface_package(h):
    entries = [HOLD, entry("t", "Double", 0.0, source(f"{h.ns}/thing", "os/msg/Thing", "data"))]
    findings = validate_ros(h.node, hold(), entries)
    assert any(item["path"] == "blackboard:t" and item["severity"] == "error"
               and "not an interface package" in item["text"] for item in findings), findings
    run = h.run(hold(), entries)
    h.spin_for(0.3)
    assert "not an interface package" in h.sources.problems[run.blackboard.id_of("t")]
    assert "os.msg" not in sys.modules
