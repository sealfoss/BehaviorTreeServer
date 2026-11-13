"""Files and compatibility with the widget (design 12; Appendix B.12, F-01 to F-11).

``data/widget/`` holds the widget's own test files (``tests/data``) and ``all_types_*.json``,
written by the widget 0.10.0 itself: every built-in widget node type, a custom leaf with fields of
every kind (a choice among them), notes, a group, a Sub-Tree, tagged values and NaN.
``data/widget_reference/serialization.py`` is the widget's file module, unchanged.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from bt_server_core import node_types
from bt_server_core.document import TreeDocument
from bt_server_core.files import (
    blackboard_file_data,
    load_tree,
    read_blackboard_file,
    read_tree_file,
    write_blackboard_file,
    write_tree_file,
)

DATA = Path(__file__).parent / "data"
WIDGET_FILES = sorted((DATA / "widget").glob("*.json"))
TREE_FILES = [path for path in WIDGET_FILES if not path.name.endswith("_BB.json")]

#: The custom leaf of all_types_BT.json, as an executor's catalog would declare it.
MOVER = {
    "type": "Mover", "title": "Mover", "kind": "leaf", "category": "Custom", "builtin": False,
    "fields": [
        {"name": "x", "kind": "float", "default": 1.5},
        {"name": "speed", "kind": "int", "default": 5},
        {"name": "mode", "kind": "choice", "default": ["walk", "run", "crawl"]},
        {"name": "avoid", "kind": "bool", "default": True},
        {"name": "label", "kind": "str", "default": "target"},
        {"name": "extra", "kind": "other", "default": {"a": (1, 2), "b": {3, 4}}},
    ],
}
CATALOG = [*node_types.builtin_catalog(), MOVER]
ROOT_ID = "a" * 32


def base_tree(nodes=(), connections=()) -> dict:
    """A tree with only the widget's features: Root (Sequence) and the given nodes."""
    return {
        "format": "behavior_tree_widget",
        "version": 1,
        "config": {"tick_interval_ms": 100, "repeat": False, "restore_blackboard": False, "default_memory": True},
        "view": {"zoom": 1.0, "center": [0.0, 0.0]},
        "nodes": [
            {"id": ROOT_ID, "type": "Root", "title": "Root", "x": 0.0, "y": 0.0, "composite": "Sequence", "memory": True},
            *nodes,
        ],
        "connections": [{"parent": parent, "child": child} for parent, child in connections],
    }


def leaf(node_id: str, type_name: str, x: float, fields: dict | None = None, **extra) -> dict:
    return {"id": node_id, "type": type_name, "title": type_name, "x": x, "y": 200.0, "fields": fields or {}, **extra}


def by_id(data: dict) -> dict:
    return {item["id"]: item for item in data["nodes"]}


def widget_resaved(original: dict) -> dict:
    """What the widget itself writes back: the blackboard of an early single-file tree goes to a
    file of its own, and the empty ``fields`` that 0.5.0 added to a Notes node it kept as a
    placeholder is not written again (the widget's Notes node saves text, width and height)."""
    expected = copy.deepcopy(original)
    expected.pop("blackboard", None)
    for item in expected["nodes"]:
        if item["type"] == "Notes" and item.get("fields") == {}:
            del item["fields"]
    return expected


# ============================================================================== F-01
@pytest.mark.parametrize("path", TREE_FILES, ids=lambda path: path.name)
def test_F_01_widget_files_load_and_save_with_the_same_data(path, tmp_path):
    original = read_tree_file(path)
    document = TreeDocument(original, CATALOG, key=path.stem, path=path.name)
    document.legacy_blackboard = None  # Save writes the tree's blackboard to its own file
    target = tmp_path / "resaved_BT.json"
    write_tree_file(target, document.to_file())
    resaved = json.loads(target.read_text(encoding="utf-8"))
    expected = widget_resaved(original)
    assert resaved["version"] == 1
    for key in ("format", "version", "config", "view", "connections"):
        assert resaved[key] == expected[key], key
    assert sorted(resaved["nodes"], key=lambda item: item["id"]) == sorted(expected["nodes"], key=lambda item: item["id"])
    assert set(resaved) == set(expected)
    # loaded again: nothing changes any more
    again = TreeDocument(read_tree_file(target), CATALOG, key="resaved")
    assert again.to_file() == resaved


def test_F_01_the_widget_written_file_round_trips_byte_for_byte(tmp_path):
    source = DATA / "widget" / "all_types_BT.json"
    document = TreeDocument(read_tree_file(source), CATALOG, key="all_types")
    assert document.problems == []
    target = tmp_path / "all_types_BT.json"
    write_tree_file(target, document.to_file())
    assert target.read_bytes() == source.read_bytes()


def test_F_01_the_widget_written_blackboard_round_trips_byte_for_byte(tmp_path):
    source = DATA / "widget" / "all_types_BB.json"
    target = tmp_path / "all_types_BB.json"
    write_blackboard_file(target, read_blackboard_file(source))
    assert target.read_bytes() == source.read_bytes()


def test_F_01_an_early_single_file_tree_loads_as_in_the_widget(tmp_path):
    (tmp_path / "old.json").write_bytes((DATA / "widget" / "tree_0_5_0.json").read_bytes())
    loaded = load_tree(tmp_path, "old")
    assert (loaded.key, loaded.path, loaded.has_blackboard_file) == ("old", "old.json", False)
    assert [item["name"] for item in loaded.blackboard] == ["counter", "speed", "names"]
    document = TreeDocument(loaded.data, CATALOG, key=loaded.key, path=loaded.path)
    assert document.legacy_blackboard == loaded.blackboard
    assert document.to_file()["blackboard"] == loaded.blackboard  # kept while it is set (a sub-tree's file)


# ============================================================================== F-02, F-03
def test_F_02_a_tree_with_only_the_widget_features_is_version_1():
    data = base_tree(
        [leaf("b" * 32, "Evaluation", -200, {"ValueName": "go", "CompareTo": None, "LiteralValue": True}),
         {"id": "c" * 32, "type": "Selector", "title": "Selector", "x": 200.0, "y": 200.0, "memory": False}],
        [(ROOT_ID, "b" * 32), (ROOT_ID, "c" * 32)],
    )
    document = TreeDocument(data, CATALOG, key="plain")
    assert document.to_file()["version"] == 1
    assert document.uses_new_features() == []


F_03_ADDITIONS = {
    "parallel": lambda data: data["nodes"].append(
        {"id": "d" * 32, "type": "Parallel", "title": "Parallel", "x": 400.0, "y": 200.0, "policy": "any"}),
    "operator": lambda data: data["nodes"][1]["fields"].update({"Operator": ">="}),
    "wait": lambda data: data["nodes"][1]["fields"].update({"Wait": True}),
    "binding": lambda data: data["nodes"][1].update({"bindings": {"LiteralValue": "limit"}}),
    "cancel_timeout": lambda data: data["config"].update({"cancel_timeout_ms": 3000}),
}


@pytest.mark.parametrize("addition", list(F_03_ADDITIONS))
def test_F_03_each_addition_alone_makes_version_2(addition):
    data = base_tree([leaf("b" * 32, "Evaluation", -200, {"ValueName": "go", "CompareTo": None, "LiteralValue": 3})],
                     [(ROOT_ID, "b" * 32)])
    assert TreeDocument(data, CATALOG).to_file()["version"] == 1
    F_03_ADDITIONS[addition](data)
    document = TreeDocument(data, CATALOG)
    assert document.problems == []
    saved = document.to_file()
    assert saved["version"] == 2
    assert TreeDocument(saved, CATALOG).to_file() == saved  # the addition survives a save and a load


def test_F_03_new_node_types_and_options_make_version_2():
    for item in (leaf("e" * 32, "CallAction", 0, {}), leaf("e" * 32, "Publish", 0, {}),
                 leaf("e" * 32, "Stop", 0, {}, wait_for_cancels=False)):
        data = base_tree([item], [(ROOT_ID, "e" * 32)])
        assert TreeDocument(data, CATALOG).to_file()["version"] == 2, item
    # also where the ROS types are unknown (LocalLink) and their nodes are placeholders
    local = node_types.builtin_catalog(ros=False)
    document = TreeDocument(base_tree([leaf("e" * 32, "CallAction", 0, {})]), local)
    assert document.is_placeholder("e" * 32) and document.file_version() == 2
    custom = {**leaf("e" * 32, "Laser", 0, {}), "bindings": {"range": "r"}}
    assert TreeDocument(base_tree([custom]), local).file_version() == 2


# ============================================================================== F-04
def test_F_04_a_blackboard_with_a_source_is_version_2_and_the_tree_stays_version_1(tmp_path):
    items = [
        {"name": "battery", "type": "Double", "value": 0.0,
         "source": {"topic": "/robot1/battery_state", "type": "sensor_msgs/msg/BatteryState", "field": "percentage",
                    "max_rate_hz": 2.0, "max_age_s": 5.0}},
        {"name": "counter", "type": "Integer", "value": 3, "reset": True, "id": "f" * 32},
    ]
    write_blackboard_file(tmp_path / "robot_BB.json", items)
    document = TreeDocument(base_tree(), CATALOG, key="robot")
    write_tree_file(tmp_path / "robot_BT.json", document.to_file())
    blackboard = json.loads((tmp_path / "robot_BB.json").read_text(encoding="utf-8"))
    assert blackboard["version"] == 2
    assert blackboard["blackboard"][1] == {"name": "counter", "type": "Integer", "value": 3, "reset": True}
    assert json.loads((tmp_path / "robot_BT.json").read_text(encoding="utf-8"))["version"] == 1
    assert blackboard_file_data(items[1:])["version"] == 1
    assert load_tree(tmp_path, "robot_BB.json").blackboard == blackboard["blackboard"]


# ============================================================================== F-05
def widget_serialization():
    """The widget's ``serialization.py``, loaded from the unchanged copy (standard library only)."""
    spec = importlib.util.spec_from_file_location("widget_serialization", DATA / "widget_reference" / "serialization.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_F_05_the_widget_refuses_a_version_2_tree_file_with_its_newer_version_message(tmp_path):
    data = base_tree([{"id": "d" * 32, "type": "Parallel", "title": "Parallel", "x": 0.0, "y": 200.0, "policy": "all"}],
                     [(ROOT_ID, "d" * 32)])
    path = tmp_path / "par_BT.json"
    write_tree_file(path, TreeDocument(data, CATALOG).to_file())
    widget = widget_serialization()
    with pytest.raises(widget.TreeFileError, match="was written by a newer version of behavior_tree_widget"):
        widget.read_tree_file(str(path))
    plain = tmp_path / "plain_BT.json"
    write_tree_file(plain, TreeDocument(base_tree(), CATALOG).to_file())
    assert widget.read_tree_file(str(plain))["version"] == 1  # version 1 still opens there


def test_F_05_the_widget_refuses_a_version_2_blackboard_file(tmp_path):
    path = tmp_path / "robot_BB.json"
    write_blackboard_file(path, [{"name": "b", "type": "Bool", "value": True,
                                  "source": {"topic": "/door", "type": "std_msgs/msg/Bool", "field": "data"}}])
    widget = widget_serialization()
    with pytest.raises(widget.TreeFileError, match="newer version"):
        widget.read_blackboard_file(str(path))


# ============================================================================== F-06
def test_F_06_the_written_order_rules_and_is_kept():
    parent, a, b = "1" * 32, "2" * 32, "3" * 32
    data = base_tree(
        [{"id": parent, "type": "Sequence", "title": "P", "x": 0.0, "y": 200.0, "memory": True},
         leaf(a, "Stop", -300), leaf(b, "Pause", 300)],
        [(ROOT_ID, parent), (parent, b), (parent, a)],
    )
    document = TreeDocument(data, CATALOG)
    assert document.children(parent) == [b, a]  # a lies left of b, but b is written first
    saved = document.to_file()
    assert [link["child"] for link in saved["connections"] if link["parent"] == parent] == [b, a]
    assert [link["child"] for link in document.to_wire()["connections"] if link["parent"] == parent] == [b, a]


# ============================================================================== F-07
def test_F_07_backslashes_are_read_as_slashes_and_absolute_files_are_reported():
    first, second = "4" * 32, "5" * 32
    data = base_tree([leaf(first, "Sub-Tree", -200, {"File": "sub\\patrol_BT.json"}),
                      leaf(second, "Sub-Tree", 200, {"File": "C:\\trees\\patrol_BT.json"})],
                     [(ROOT_ID, first), (ROOT_ID, second)])
    document = TreeDocument(data, CATALOG, key="cells/robot")
    from bt_server_core.files import subtree_key

    assert subtree_key(document.key, document.fields(first)["File"]) == "cells/sub/patrol"
    (problem,) = document.subtree_problems()
    assert problem["node"] == second and problem["file"] == "C:\\trees\\patrol_BT.json"
    assert "absolute" in problem["text"]
    assert document.fields(first)["File"] == "sub\\patrol_BT.json"  # the file keeps what it holds
    assert document.problems == [f"node 'Sub-Tree': {problem['text']}"]


def test_F_07_a_posix_absolute_file_and_one_leading_outside_are_reported():
    data = base_tree([leaf("4" * 32, "Sub-Tree", -200, {"File": "/srv/trees/patrol_BT.json"}),
                      leaf("5" * 32, "Sub-Tree", 200, {"File": "../../patrol_BT.json"})])
    problems = TreeDocument(data, CATALOG, key="robot").subtree_problems()
    assert [problem["node"] for problem in problems] == ["4" * 32, "5" * 32]


# ============================================================================== F-08
def test_F_08_an_unknown_type_is_a_placeholder_and_saved_unchanged():
    item = {"id": "6" * 32, "type": "Laser Scan", "title": "Scan", "x": 10, "y": 200.5,
            "fields": {"range": 3, "weird": {"__set__": [3, 1, 2]}, "bad": {"__tuple__": 5}},
            "field_selections": {"range": True}, "calibration": {"k": [1, 2, None]}, "z_order": 7}
    data = base_tree([item], [(ROOT_ID, item["id"])])
    document = TreeDocument(data, CATALOG)
    assert document.is_placeholder(item["id"])
    assert any("Laser Scan" in problem for problem in document.problems)
    saved = by_id(document.to_file())[item["id"]]
    assert saved == item
    assert json.dumps(saved) == json.dumps(item)  # key order and numbers as read
    wire = document.node_to_wire(item["id"])
    assert wire["unknown"] is True and wire["fields"] == item["fields"]
    assert document.children(ROOT_ID) == [item["id"]]


def test_F_08_a_built_in_name_used_by_an_application_type_stays_a_placeholder():
    item = {"id": "7" * 32, "type": "Notes", "title": "Mail", "x": 0.0, "y": 0.0, "fields": {"recipient": "bob"}}
    document = TreeDocument(base_tree([item], [(ROOT_ID, item["id"])]), CATALOG)
    assert document.is_placeholder(item["id"]) and document.children(ROOT_ID) == [item["id"]]
    assert by_id(document.to_file())[item["id"]] == item


# ============================================================================== F-09
def test_F_09_a_double_stays_a_double_through_wire_op_and_save():
    node = "8" * 32
    data = base_tree([leaf(node, "Mover", 0, {"x": 1.0})], [(ROOT_ID, node)])
    document = TreeDocument(data, CATALOG)
    wire = document.node_to_wire(node)["fields"]["x"]
    assert wire == "1.0"
    applied = document.apply({"op": "node.set_field", "id": node, "field": "x", "value": json.loads(json.dumps(wire))})
    assert applied.op["value"] == "1.0"
    text = json.dumps(document.to_file(), allow_nan=True)
    assert '"x": 1.0' in text
    document.apply({"op": "node.set_field", "id": node, "field": "x", "value": "nan"})
    assert '"x": NaN' in json.dumps(document.to_file(), allow_nan=True)
    assert document.node_to_wire(node)["fields"]["x"] == "nan"


def test_F_09_a_double_literal_keeps_its_type_through_the_wire():
    node = "9" * 32
    data = base_tree([leaf(node, "Set", 0, {"ValueName": "speed", "CompareTo": None, "LiteralValue": 1.0})])
    document = TreeDocument(data, CATALOG)
    typed = document.node_to_wire(node, {"speed": "Double"})["fields"]["LiteralValue"]
    assert typed == {"type": "Double", "value": "1.0"}
    document.apply({"op": "node.set_field", "id": node, "field": "LiteralValue", "value": typed})
    assert by_id(document.to_file())[node]["fields"]["LiteralValue"] == 1.0
    assert isinstance(by_id(document.to_file())[node]["fields"]["LiteralValue"], float)


# ============================================================================== F-11
def test_F_11_siblings_ten_pixels_apart_in_a_version_1_file_are_named():
    a, b, c = "1" * 32, "2" * 32, "3" * 32
    data = base_tree([{**leaf(a, "Stop", -100), "title": "Left"}, {**leaf(b, "Pause", -90), "title": "Right"},
                      {**leaf(c, "Restart", 400), "title": "Far"}],
                     [(ROOT_ID, a), (ROOT_ID, b), (ROOT_ID, c)])
    document = TreeDocument(data, CATALOG)
    (warning,) = document.save_warnings()
    assert "'Left'" in warning and "'Right'" in warning and "Far" not in warning


def test_F_11_no_warning_for_a_version_2_file():
    data = base_tree([leaf("1" * 32, "Stop", -100), leaf("2" * 32, "Pause", -90)],
                     [(ROOT_ID, "1" * 32), (ROOT_ID, "2" * 32)])
    data["config"]["on_unconfirmed_cancel"] = "continue"
    assert TreeDocument(data, CATALOG).save_warnings() == []
