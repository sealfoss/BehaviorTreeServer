"""ROS types: the type-name check (design 13, S-12, L-15), ``~/describe`` (contract 5.5, 5.8
item 4), the field and entry-type rules of 7.4, and the ROS checks of 8.3 (``ros_validate``)."""

from __future__ import annotations

import sys

import pytest

pytest.importorskip("rclpy")
pytest.importorskip("bt_server_interfaces.action")

from example_interfaces.action import Fibonacci  # noqa: E402
from rclpy.action import ActionServer  # noqa: E402
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy  # noqa: E402
from sensor_msgs.msg import BatteryState, LaserScan  # noqa: E402
from std_msgs.msg import Float64, UInt32  # noqa: E402
from std_srvs.srv import Trigger  # noqa: E402

from bt_server_executor import ros_types  # noqa: E402
from bt_server_executor.ros_validate import validate_ros  # noqa: E402
from ros_helpers import Harness, bundle, entry, leaf  # noqa: E402

pytestmark = pytest.mark.ros

LATCHED = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL)


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path, "types")
    yield harness
    harness.close()


@pytest.mark.parametrize("name", ["os/msg/Thing", "os/action/Thing", "../os/msg/Thing", "std_msgs/Bool",
                                  "std_msgs/msg/Nope", "subprocess/srv/Run", "", None])
def test_type_names_are_checked_before_any_import(name):
    for kind in ("msg", "srv", "action"):
        with pytest.raises(ValueError):
            ros_types.load_interface(name, kind)
    assert "os.msg" not in sys.modules and "os.action" not in sys.modules and "subprocess.srv" not in sys.modules


def test_installed_interfaces_load():
    assert ros_types.type_name_problem("std_msgs/msg/Bool", "msg") is None
    assert ros_types.load_interface("example_interfaces/action/Fibonacci", "action") is Fibonacci
    assert "not a ROS service type" in ros_types.type_name_problem("std_msgs/msg/Bool", "srv")


def test_describe_interface_of_an_action_a_service_and_a_message(h):
    action = ros_types.describe(h.node, "interface", {"type": "example_interfaces/action/Fibonacci"})
    assert set(action) == {"goal", "result", "feedback"}
    assert action["goal"] == [{"name": "order", "kind": "int32", "default": 0, "min": -2**31, "max": 2**31 - 1}]
    assert action["result"][0]["kind"] == "sequence<int32>"
    service = ros_types.describe(h.node, "interface", {"type": "std_srvs/srv/Trigger"})
    assert service == {"request": [], "response": [{"name": "success", "kind": "bool", "default": False},
                                                   {"name": "message", "kind": "string", "default": ""}]}
    message = ros_types.describe(h.node, "interface", {"type": "sensor_msgs/msg/BatteryState"})["message"]
    fields = {item["name"]: item for item in message}
    assert fields["header"]["kind"] == "std_msgs/msg/Header" and "fields" in fields["header"]
    assert fields["percentage"]["kind"] == "float32"
    choices = [item["name"] for item in fields["power_supply_status"]["constants"]]
    assert "POWER_SUPPLY_STATUS_CHARGING" in choices and "POWER_SUPPLY_HEALTH_GOOD" not in choices
    with pytest.raises(ValueError, match="not an interface package"):
        ros_types.describe(h.node, "interface", {"type": "os/msg/Thing"})


def test_describe_graph(h):
    h.keep(ActionServer(h.peer, Fibonacci, "graph_fib", execute_callback=lambda handle: Fibonacci.Result()))
    h.keep(h.peer.create_service(Trigger, "graph_trigger", lambda request, response: response))
    h.keep(h.peer.create_publisher(Float64, "graph_topic", 1))
    h.wait_for_graph(lambda: [f"{h.ns}/graph_fib", "example_interfaces/action/Fibonacci"]
                     in ros_types.describe(h.node, "graph", {})["actions"])
    graph = ros_types.describe(h.node, "graph", {})
    assert [f"{h.ns}/graph_trigger", "std_srvs/srv/Trigger"] in graph["services"]
    assert [f"{h.ns}/graph_topic", "std_msgs/msg/Float64"] in graph["topics"]
    assert not any("/_action/" in name for name, _ in graph["services"] + graph["topics"])


def test_describe_sample(h):
    pub = h.keep(h.peer.create_publisher(BatteryState, "battery", LATCHED))
    pub.publish(BatteryState(percentage=0.5))
    big = h.keep(h.peer.create_publisher(UInt32, "big", LATCHED))
    big.publish(UInt32(data=3_000_000_000))
    h.wait_for_graph(lambda: h.node.get_publishers_info_by_topic(f"{h.ns}/big"))
    answer = ros_types.describe(h.node, "sample", {"topic": f"{h.ns}/battery", "field": "percentage"})
    assert answer["value"] == {"type": "Double", "value": "0.5"}
    whole = ros_types.describe(h.node, "sample", {"topic": f"{h.ns}/battery",
                                                  "type": "sensor_msgs/msg/BatteryState"})
    assert whole["value"]["type"] == "Dictionary"
    assert ros_types.describe(h.node, "sample", {"topic": f"{h.ns}/big", "field": "data"})["value"]["type"] == "Double"
    with pytest.raises(ValueError, match="not on the network"):
        ros_types.describe(h.node, "sample", {"topic": f"{h.ns}/nothing", "field": "data"})


def test_fields_and_entry_types_of_7_4():
    def types(cls, path):
        return ros_types.entry_types(ros_types.resolve_field(cls, path))

    assert types(BatteryState, "present") == ("Bool",)
    assert types(BatteryState, "power_supply_status") == ("Integer", "Double")
    assert types(BatteryState, "header.stamp.nanosec") == ("Double", "Integer")  # uint32
    assert types(BatteryState, "percentage") == ("Double",)
    assert types(BatteryState, "location") == ("String",)
    assert types(LaserScan, "ranges") == ("List",)
    assert types(LaserScan, "ranges[0]") == ("Double",)
    assert types(LaserScan, "header") == ("Dictionary",)
    assert types(LaserScan, "") == ("Dictionary",)
    with pytest.raises(ValueError, match="no field"):
        ros_types.resolve_field(LaserScan, "pose.x")
    with pytest.raises(ros_types.SkipValue):
        ros_types.entry_value(3_000_000_000, "Integer")
    assert ros_types.entry_value(5, "Double") == 5.0


def test_validate_ros_checks_leaves_and_sources(h):
    h.keep(ActionServer(h.peer, Fibonacci, "fib", execute_callback=lambda handle: Fibonacci.Result()))
    h.wait_for_graph(lambda: [f"{h.ns}/fib", "example_interfaces/action/Fibonacci"]
                     in ros_types.describe(h.node, "graph", {})["actions"])
    good = leaf("CallAction", "A", Action=f"{h.ns}/fib", Type="example_interfaces/action/Fibonacci",
                Goal={"order": 2}, Timeout=5.0, bindings={"Goal.order": "n"}, outputs={"Result.sequence": "seq"})
    wrong = leaf("CallAction", "B", Action=f"{h.ns}/fib", Type="std_srvs/action/Nope", Goal={})
    other_type = leaf("CallAction", "C", Action=f"{h.ns}/fib", Type="nav2_msgs/action/Wait", Goal={})
    absent = leaf("CallAction", "D", Action=f"{h.ns}/nobody", Type="example_interfaces/action/Fibonacci",
                  Goal={"order": 2, "speed": 1}, bindings={"Goal.order": "label"}, outputs={"Result.sequence": "x"})
    service = leaf("CallService", "S", Service=f"{h.ns}/nothing", Type="std_srvs/srv/Trigger",
                   outputs={"Response.success": "label", "Result.x": "x"})
    entries = [entry("n", "Integer", 2), entry("seq", "List", []), entry("label", "String", ""),
               entry("x", "Double", 0.0),
               entry("ok", "Double", 0.0, {"topic": "/t", "type": "sensor_msgs/msg/BatteryState",
                                           "field": "percentage"}),
               entry("badfield", "Double", 0.0, {"topic": "/t", "type": "sensor_msgs/msg/BatteryState",
                                                 "field": "voltage.x"}),
               entry("badtype", "String", "", {"topic": "/t", "type": "sensor_msgs/msg/BatteryState",
                                               "field": "percentage"})]
    findings = validate_ros(h.node, bundle(good, wrong, other_type, absent, service), entries)
    by_node: dict[str, list[str]] = {}
    for item in findings:
        by_node.setdefault(item["path"].rsplit("/", 1)[-1].split(":")[-1], []).append(
            f"{item['severity']}: {item['text']}")
    assert good["id"] not in by_node, by_node.get(good["id"])
    assert any("has no action" in text for text in by_node[wrong["id"]])
    assert any("has the type example_interfaces/action/Fibonacci" in text for text in by_node[other_type["id"]])
    texts = by_node[absent["id"]]
    assert any(text.startswith("warning: the action") and "not on the network" in text for text in texts)
    assert any("no field 'speed'" in text for text in texts)
    assert any("String entry 'label' does not suit Goal.order" in text for text in texts)
    assert any("Result.sequence" in text and "List" in text for text in texts)
    texts = by_node[service["id"]]
    assert any("Response.success" in text and "Bool" in text for text in texts)
    assert any("Result.x" in text and "does not start with Response." in text for text in texts)
    assert "blackboard:ok" not in {item["path"] for item in findings if item["severity"] == "error"}
    assert any(item["path"] == "blackboard:badfield" and "no field" in item["text"] for item in findings)
    assert any(item["path"] == "blackboard:badtype" and "Double" in item["text"] for item in findings)
    assert any(item["path"] == "blackboard:ok" and item["severity"] == "warning" for item in findings)
