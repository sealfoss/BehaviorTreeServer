"""Bundles (design 4.3) and the checks of design 8.3 that need no ROS."""

from __future__ import annotations

import uuid

import pytest

from bt_server_core import node_types
from bt_server_core.bundle import build_bundle, sub_bundle, validate_bundle
from bt_server_core.document import TreeDocument
from bt_server_core.files import (
    load_tree,
    parse_blackboard,
    read_blackboard_file,
    write_blackboard_file,
    write_tree_file,
)

CATALOG = node_types.builtin_catalog()


def ids(count: int) -> list[str]:
    return [uuid.uuid4().hex for _ in range(count)]


def tree_file(nodes: list[dict], connections: list[tuple[str, str]], root: str) -> dict:
    return {
        "format": "behavior_tree_widget",
        "version": 1,
        "config": {"tick_interval_ms": 100, "repeat": False, "restore_blackboard": False, "default_memory": True},
        "nodes": [{"id": root, "type": "Root", "title": "Root", "x": 0.0, "y": 0.0, "composite": "Sequence",
                   "memory": True}, *nodes],
        "connections": [{"parent": parent, "child": child} for parent, child in connections],
    }


def node(node_id: str, type_name: str, x: float = 0.0, **extra) -> dict:
    return {"id": node_id, "type": type_name, "title": type_name, "x": x, "y": 200.0, **extra}


@pytest.fixture
def trees(tmp_path):
    """robot: Sub-Tree cells\\patrol (Windows separators), Remote Sub-Tree dock, a note and a group;
    cells/patrol: Sub-Tree of itself, of ../common/arm, and of a missing file; common/arm: a
    Stop; dock: a Sub-Tree of robot (a run of its own, so no self-containment), with a
    blackboard file."""
    r, sub, remote, note, group = ids(5)
    robot = tree_file([node(sub, "Sub-Tree", -200, fields={"File": "cells\\patrol_BT.json"}),
                       node(remote, "RemoteSubTree", 200, fields={"Executor": "/r2/bt_executor", "File": "dock_BT.json",
                                                                  "Timeout": 60.0}),
                       {"id": note, "type": "Notes", "title": "Notes", "x": 500.0, "y": 0.0, "text": "hi", "width": 220,
                        "height": 120},
                       {"id": group, "type": "Group", "title": "group_001", "x": -400.0, "y": 150.0, "width": 600,
                        "height": 300, "color": "#3d7cc9"}],
                      [(r, sub), (r, remote)], r)
    p, itself, arm, missing = ids(4)
    patrol = tree_file([node(itself, "Sub-Tree", fields={"File": "patrol_BT.json"}),
                        node(arm, "Sub-Tree", fields={"File": "../common/arm.json"}),
                        node(missing, "Sub-Tree", fields={"File": "nothere"})],
                       [(p, itself), (p, arm), (p, missing)], p)
    a, stop = ids(2)
    arm_tree = tree_file([node(stop, "Stop")], [(a, stop)], a)
    d, back = ids(2)
    dock = tree_file([node(back, "Sub-Tree", fields={"File": "robot"})], [(d, back)], d)
    (tmp_path / "cells").mkdir()
    (tmp_path / "common").mkdir()
    for key, data in (("robot", robot), ("cells/patrol", patrol), ("common/arm", arm_tree), ("dock", dock)):
        write_tree_file(tmp_path / f"{key}_BT.json", data)
    write_blackboard_file(tmp_path / "dock_BB.json", [{"name": "dock_id", "type": "Integer", "value": 1}])
    return tmp_path, {"sub": sub, "remote": remote, "itself": itself, "arm": arm, "missing": missing, "back": back,
                      "note": note, "group": group}


def getter(root, open_documents=None):
    def get_document(key):
        if open_documents and key in open_documents:
            return open_documents[key]
        loaded = load_tree(root, key)
        return TreeDocument(loaded.data, CATALOG, key=key, path=loaded.path)

    return get_document


def nodes_of(bundle, key) -> dict:
    return {item["id"]: item for item in bundle["trees"][key]["nodes"]}


def test_build_bundle_resolves_sub_trees_and_remote_trees(trees):
    root, names = trees
    robot = TreeDocument(load_tree(root, "robot").data, CATALOG, key="robot")
    robot.apply({"op": "node.set_title", "id": names["sub"], "title": "Patrol"})  # unsaved: what is shown runs
    bundle = build_bundle("robot", getter(root, {"robot": robot}),
                          lambda key: read_blackboard_file(root / f"{key}_BB.json"))
    assert (bundle["format"], bundle["version"], bundle["root"]) == ("behavior_tree_bundle", 1, "robot")
    assert list(bundle["trees"]) == ["robot", "cells/patrol", "common/arm", "dock"]
    robot_nodes = nodes_of(bundle, "robot")
    assert robot_nodes[names["sub"]]["tree"] == "cells/patrol" and robot_nodes[names["sub"]]["title"] == "Patrol"
    assert robot_nodes[names["sub"]]["fields"]["File"] == "cells\\patrol_BT.json"
    assert robot_nodes[names["remote"]]["tree"] == "dock"
    assert robot_nodes[names["note"]]["text"] == "hi" and robot_nodes[names["group"]]["width"] == 600
    patrol = nodes_of(bundle, "cells/patrol")
    assert patrol[names["itself"]]["tree"] == "cells/patrol"  # the engine refuses it per chain
    assert patrol[names["arm"]]["tree"] == "common/arm"
    assert "tree" not in patrol[names["missing"]]
    assert nodes_of(bundle, "dock")[names["back"]]["tree"] == "robot"  # the remote run is a run of its own
    assert bundle["blackboards"] == {"dock": [{"name": "dock_id", "type": "Integer", "value": 1}]}
    assert robot.to_file()["nodes"][1].get("tree") is None  # the document itself is not changed


def test_a_sub_tree_cycle_fails_only_the_node_that_closes_it(tmp_path):
    a_root, a_sub, b_root, b_sub = ids(4)
    write_tree_file(tmp_path / "a_BT.json", tree_file([node(a_sub, "Sub-Tree", fields={"File": "b"})], [(a_root, a_sub)], a_root))
    write_tree_file(tmp_path / "b_BT.json", tree_file([node(b_sub, "Sub-Tree", fields={"File": "a"})], [(b_root, b_sub)], b_root))
    bundle = build_bundle("a", getter(tmp_path))
    assert nodes_of(bundle, "a")[a_sub]["tree"] == "b" and nodes_of(bundle, "b")[b_sub]["tree"] == "a"
    findings = validate_bundle(bundle, [], CATALOG)
    assert findings == [{"path": f"tree:b/{b_sub}", "severity": "error", "text": "the sub-tree 'a' contains itself"}]


def test_the_root_tree_must_exist(tmp_path):
    with pytest.raises(KeyError):
        build_bundle("nothing", getter(tmp_path))


def test_sub_bundle_is_the_part_a_remote_executor_runs(trees):
    root, _ = trees
    bundle = build_bundle("robot", getter(root), lambda key: read_blackboard_file(root / f"{key}_BB.json"))
    part = sub_bundle(bundle, "dock")
    assert part["root"] == "dock"
    assert set(part["trees"]) == {"dock", "robot", "cells/patrol", "common/arm"}
    assert part["blackboards"] == bundle["blackboards"]
    assert "blackboards" not in sub_bundle(bundle, "common/arm")


def test_validate_reports_the_checks_that_need_no_ros(trees):
    root, names = trees
    bundle = build_bundle("robot", getter(root), lambda key: read_blackboard_file(root / f"{key}_BB.json"))
    findings = validate_bundle(bundle, [], CATALOG)
    by_path = {(item["path"], item["text"]) for item in findings}
    assert (f"tree:cells/patrol/{names['itself']}", "the sub-tree 'cells/patrol' contains itself") in by_path
    assert (f"tree:cells/patrol/{names['missing']}", "the sub-tree file 'cells/nothere_BT.json' is missing") in by_path
    assert all(item["severity"] == "error" for item in findings) and len(findings) == 2  # dock runs elsewhere


def test_validate_node_checks(tmp_path):
    r, unknown, neg, wait, call, remote, bound, absolute, nofile, ok_wait = ids(10)
    data = tree_file(
        [node(unknown, "Laser"),
         node(neg, "Negation"),
         node(wait, "Evaluation", fields={"ValueName": "go", "CompareTo": None, "LiteralValue": True, "Wait": True}),
         node(ok_wait, "Evaluation", fields={"ValueName": "go", "CompareTo": None, "LiteralValue": True, "Wait": True,
                                             "WaitTimeout": 3.0}),
         node(call, "CallAction", fields={"Action": "/a"}, bindings={"Goal.pose": "nothing", "Timeout": "label"},
              outputs={"Result.x": "created_later"}),
         node(remote, "RemoteSubTree", fields={"Executor": "", "File": "dock_BT.json", "Timeout": 5.0}),
         node(bound, "UDP Message", bindings={"Destination Port": "port", "Message": "label"}),
         node(absolute, "Sub-Tree", fields={"File": "C:\\trees\\x_BT.json"}),
         node(nofile, "Sub-Tree", fields={"File": ""}),
         node(ids(1)[0], "Laser")],  # not connected: never runs, never checked
        [(r, unknown), (r, neg), (r, wait), (r, ok_wait), (r, call), (r, remote), (r, bound), (r, absolute), (r, nofile)], r)
    catalog = [item for item in CATALOG if item["type"] != "Laser"]
    document = TreeDocument(data, catalog, key="robot")
    bundle = build_bundle("robot", lambda key: document if key == "robot" else None)
    entries = [{"name": "go", "type": "Bool"}, {"name": "port", "type": "String"}, {"name": "label", "type": "String"}]
    found = {(item["path"].split("/")[-1], item["severity"], item["text"]) for item in validate_bundle(bundle, entries, catalog)}
    expected = {
        (unknown, "error", "unknown node type 'Laser'"),
        (neg, "error", "the Negation has no child"),
        (wait, "warning", "the Evaluation waits without a timeout"),
        (call, "warning", "the Call Action node has no Timeout: it may wait for ever"),
        (call, "error", "the binding of 'Goal.pose' names the missing entry 'nothing'"),
        (call, "error", "the String entry 'label' does not suit the field 'Timeout'"),
        (remote, "error", "the sub-tree file 'dock_BT.json' is missing"),
        (bound, "error", "the String entry 'port' does not suit the field 'Destination Port'"),
        (absolute, "error", "the file 'C:\\\\trees\\\\x_BT.json' is an absolute path; choose the file again"),
        (nofile, "error", "the node has no file"),
    }
    assert found == expected
    assert all(item["path"].startswith("tree:robot/") for item in validate_bundle(bundle, entries, catalog))


def test_validate_remote_sub_tree_links(tmp_path):
    r, remote, d = ids(3)
    write_tree_file(tmp_path / "robot_BT.json", tree_file(
        [node(remote, "RemoteSubTree", fields={"Executor": "/r2/bt_executor", "File": "dock"},
              bindings={"battery": "level", "dock_id": "dock", "pose": "missing"},
              outputs={"result": "outcome", "count": "fresh"})], [(r, remote)], r))
    write_tree_file(tmp_path / "dock_BT.json", tree_file([], [], d))
    write_blackboard_file(tmp_path / "dock_BB.json", [
        {"name": "battery", "type": "Double", "value": 0.0, "source": {"topic": "/b", "type": "std_msgs/msg/Float64",
                                                                        "field": "data"}},
        {"name": "dock_id", "type": "Integer", "value": 1},
        {"name": "result", "type": "String", "value": ""},
        {"name": "count", "type": "Integer", "value": 0}])
    bundle = build_bundle("robot", getter(tmp_path), lambda key: read_blackboard_file(tmp_path / f"{key}_BB.json"))
    entries = [{"name": "level", "type": "Double"}, {"name": "dock", "type": "String"}, {"name": "outcome", "type": "Bool"}]
    texts = sorted(item["text"] for item in validate_bundle(bundle, entries, CATALOG))
    assert texts == sorted([
        "the Remote Sub-Tree node has no Timeout: it may wait for ever",
        "the remote entry 'battery' has a source and cannot be bound",
        "the remote entry 'dock_id' is Integer, the entry 'dock' String",
        "the binding of 'pose' names the missing entry 'missing'",
        "the remote entry 'result' is String, the entry 'outcome' Bool",
    ])


def test_validate_blackboard_sources_and_object_names():
    r = ids(1)[0]
    bundle = {"format": "behavior_tree_bundle", "version": 1, "root": "t", "trees": {"t": tree_file([], [], r)}}
    entries, problems = parse_blackboard([
        {"name": "flags", "type": "Set", "value": {"__set__": []}, "source": {"topic": "/f", "type": "x/msg/F", "field": "a"}},
        {"name": "whole", "type": "Double", "value": 0.0, "source": {"topic": "/w", "type": "x/msg/W"}},
        {"name": "msg", "type": "Dictionary", "value": {}, "source": {"topic": "/w", "type": "x/msg/W"}},
        {"name": "level", "type": "Double", "value": 0.0, "source": {"topic": "/w", "type": "x/msg/W", "field": "a.b"}},
        {"name": "node", "type": "String", "value": ""},
    ])
    assert problems == []
    findings = validate_bundle(bundle, entries, CATALOG, object_names=["node", "other"])
    assert findings == [
        {"path": "blackboard:flags", "severity": "error", "text": "a Set entry cannot take its value from a topic"},
        {"path": "blackboard:whole", "severity": "error", "text": "the whole message needs a Dictionary entry"},
        {"path": "blackboard:node", "severity": "error", "text": "the entry 'node' has the name of an Object entry of the executor"},
    ]


def test_review_1_self_containment_is_decided_per_chain():
    """R -> [X, Y]; X (Selector) -> [Y, a leaf that succeeds]; Y -> X. On R -> X -> Y the node
    Y -> X closes the cycle and fails, X falls back to its leaf; on R -> Y -> X it is X -> Y that
    fails. The widget succeeds; so must the run of the bundle."""
    from engine_helpers import EngineRun

    from bt_server_core.engine import Outcome

    def tree(composite: str, *files: str, leaf: bool = False) -> dict:
        root = "r" * 32
        items = [node(str(index) * 32, "Sub-Tree", 300.0 * index, title=f"to {name}", fields={"File": f"{name}_BT.json"})
                 for index, name in enumerate(files)]
        if leaf:
            items.append(node("s" * 32, "TestS", 900.0, fields={}))
        data = tree_file(items, [(root, item["id"]) for item in items], root)
        data["nodes"][0]["composite"] = composite
        return data

    catalog = [*CATALOG, {"type": "TestS", "title": "TestS", "kind": "leaf", "fields": []}]
    documents = {key: TreeDocument(data, catalog, key=key) for key, data in
                 {"R": tree("Sequence", "X", "Y"), "X": tree("Selector", "Y", leaf=True), "Y": tree("Sequence", "X")}.items()}
    bundle = build_bundle("R", documents.get)
    assert [item.get("tree") for item in bundle["trees"]["Y"]["nodes"] if item["type"] == "Sub-Tree"] == ["X"]
    assert [item.get("tree") for item in bundle["trees"]["X"]["nodes"] if item["type"] == "Sub-Tree"] == ["Y"]
    found = {(item["path"], item["text"]) for item in validate_bundle(bundle, [], catalog)}
    assert found == {(f"tree:Y/{'0' * 32}", "the sub-tree 'X' contains itself"),
                     (f"tree:X/{'0' * 32}", "the sub-tree 'Y' contains itself")}
    run = EngineRun("", bundle=bundle).start()
    assert run.result is not None and run.result.outcome == Outcome.SUCCEEDED


def test_review_10_bundle_keys_follow_the_documents_spelling(tmp_path):
    r, sub = ids(2)
    write_tree_file(tmp_path / "robot_BT.json", tree_file([node(sub, "Sub-Tree", fields={"File": "Robot_BT.json"})],
                                                          [(r, sub)], r))
    documents = {"robot": TreeDocument(load_tree(tmp_path, "robot").data, CATALOG, key="robot")}
    bundle = build_bundle("robot", lambda key: documents.get(key.lower()))
    assert list(bundle["trees"]) == ["robot"] and nodes_of(bundle, "robot")[sub]["tree"] == "robot"
    assert [item["text"] for item in validate_bundle(bundle, [], CATALOG)] == ["the sub-tree 'robot' contains itself"]
