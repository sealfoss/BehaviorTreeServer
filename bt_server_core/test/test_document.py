"""TreeDocument: loading checks, edit operations and their refusals (design 9.3; contract 5.3;
Appendix B.11, G-01 to G-04 and G-17 at document level)."""

from __future__ import annotations

import copy
import json
import uuid

import pytest

from bt_server_core import node_types, values
from bt_server_core.document import SCENE_LIMIT, OpRefused, TreeDocument, node_size
from bt_server_core.files import TreeFileError

MOVER = {
    "type": "Mover", "title": "Mover", "kind": "leaf", "category": "Custom", "builtin": False,
    "fields": [
        {"name": "x", "kind": "float", "default": 1.5},
        {"name": "speed", "kind": "int", "default": 5},
        {"name": "mode", "kind": "choice", "default": ["walk", "run"]},
        {"name": "avoid", "kind": "bool", "default": True},
        {"name": "label", "kind": "str", "default": "target"},
        {"name": "goal", "kind": "other", "default": {"pose": {"x": 0.0}, "points": [1, 2]}},
    ],
}
CATALOG = [*node_types.builtin_catalog(), MOVER]

ROOT, SEQ, SEL, PAR, NEG = "0" * 32, "1" * 32, "2" * 32, "3" * 32, "4" * 32
EVAL, SET, PRINT, MOVE, CALL, REMOTE, NOTE, GROUP, STOP = (f"{n:x}" * 32 for n in range(5, 14))
ENTRY_TYPES = {"counter": "Integer", "speed": "Double", "go": "Bool"}


def new_id() -> str:
    return uuid.uuid4().hex


def tree() -> dict:
    def item(node_id, type_name, x, y, **extra):
        return {"id": node_id, "type": type_name, "title": type_name, "x": x, "y": y, **extra}

    return {
        "format": "behavior_tree_widget",
        "version": 2,
        "config": {"tick_interval_ms": 100, "repeat": False, "restore_blackboard": False, "default_memory": True},
        "nodes": [
            item(ROOT, "Root", 0.0, 0.0, composite="Sequence", memory=True),
            item(SEQ, "Sequence", -600.0, 200.0, memory=True),
            item(SEL, "Selector", 0.0, 200.0, memory=False),
            item(PAR, "Parallel", 600.0, 200.0, policy="any"),
            item(NEG, "Negation", 900.0, 200.0),
            item(EVAL, "Evaluation", -900.0, 400.0, fields={"ValueName": "counter", "CompareTo": None, "LiteralValue": 4}),
            item(SET, "Set", -500.0, 400.0, fields={"ValueName": "speed", "CompareTo": None, "LiteralValue": 2.5}),
            item(PRINT, "Print Message", 0.0, 400.0, fields={"Level": 3, "Source": "counter", "Message": "hi"}),
            item(MOVE, "Mover", 400.0, 400.0, fields={"x": 2, "mode": ["walk", "run"]}, field_selections={"mode": 1}),
            item(CALL, "CallAction", 800.0, 400.0, fields={"Action": "/a", "Type": "p/action/A", "Timeout": 5.0},
                 bindings={"Goal.pose": "speed"}, outputs={"Result.code": "counter"}),
            item(REMOTE, "RemoteSubTree", 1200.0, 400.0, fields={"Executor": "/r2/bt_executor", "File": "dock_BT.json"},
                 bindings={"dock_id": "counter"}),
            item(NOTE, "Notes", 2000.0, 0.0, text="note", width=220, height=120),
            item(GROUP, "Group", -1200.0, 300.0, width=900, height=400, color="#3d7cc9", title="group_004"),
            item(STOP, "Stop", 1600.0, 400.0, fields={}),
        ],
        "connections": [{"parent": p, "child": c} for p, c in (
            (ROOT, SEQ), (ROOT, SEL), (ROOT, PAR), (SEQ, EVAL), (SEQ, SET), (SEL, PRINT), (PAR, MOVE), (PAR, CALL),
            (ROOT, NEG), (NEG, STOP), (ROOT, REMOTE))],
    }


@pytest.fixture
def doc() -> TreeDocument:
    document = TreeDocument(tree(), CATALOG, key="robot")
    assert document.problems == []
    return document


def state(document: TreeDocument):
    return json.dumps(document.to_file(), sort_keys=True), document.rev


def refused(document: TreeDocument, op: dict, match: str = "", **options) -> OpRefused:
    before = state(document)
    with pytest.raises(OpRefused, match=match) as caught:
        document.apply(op, **options)
    assert state(document) == before  # nothing changed, revision included (G-01)
    return caught.value


def reloaded(document: TreeDocument) -> TreeDocument:
    """The document saved and loaded again: what an operation did must survive that."""
    again = TreeDocument(document.to_file(), CATALOG, key=document.key)
    assert again.problems == []
    assert again.to_file() == document.to_file()
    return again


# ============================================================================== loading
def test_loading_follows_the_widget_rules():
    data = tree()
    nodes = data["nodes"]
    nodes[8]["fields"].update({"speed": "fast", "unknown_field": 3, "label": 7})  # wrong kinds, undeclared
    nodes[5]["fields"]["CompareTo"] = ""
    nodes.append({"id": "x1", "type": "Stop", "title": "", "x": float("nan"), "y": 0.0})
    nodes.append({"id": "x2", "type": "Stop", "title": "far", "x": 1e9, "y": -1e9})
    nodes.append({"id": "x1", "type": "Stop"})  # duplicate id
    nodes.append({"type": "Stop"})
    nodes.append({"id": "x3"})
    nodes.append("junk")
    nodes.append({"id": "x4", "type": "Root"})
    nodes.append({"id": "x5", "type": "Sequence", "x": 0.0, "y": 0.0})
    nodes.append({"id": "x6", "type": "Sequence", "x": 0.0, "y": 0.0})
    data["connections"] += [
        {"parent": NEG, "child": "x1"},  # a second child of a Negation
        {"parent": SEQ, "child": ROOT},  # the Root takes no parent
        {"parent": EVAL, "child": "x2"},  # a leaf takes no children
        {"parent": SEQ, "child": NOTE},  # Notes are not connected
        {"parent": SEL, "child": EVAL},  # a second parent
        {"parent": EVAL, "child": EVAL},
        {"parent": "nobody", "child": "x2"},
        {"parent": SEQ, "child": "x2"},
        {"parent": "x5", "child": "x6"},
        {"parent": "x6", "child": "x5"},
        7,
    ]
    document = TreeDocument(data, CATALOG)
    fields = document.fields(MOVE)
    assert fields["x"] == 2.0 and isinstance(fields["x"], float)  # an int loads into a float field
    assert (fields["speed"], fields["label"]) == (5, "target") and "unknown_field" not in fields
    assert document.fields(EVAL)["CompareTo"] is None
    assert document.title("x1") == "Stop" and document.position("x1") == (0.0, 0.0)
    assert document.position("x2") == (SCENE_LIMIT, -SCENE_LIMIT)
    assert document.children(NEG) == [STOP] and document.parent(ROOT) is None
    assert document.children(SEQ) == [EVAL, SET, "x2"]
    joined = "\n".join(document.problems)
    for text in ("ignoring saved field 'speed'", "no longer declares", "invalid position", "moved inside",
                 "duplicate id", "has no type", "node 19 is not an object", "extra root node",
                 "accepts only 1 child", "already has a parent", "is invalid", "refers to a missing node", "creates a cycle",
                 "connection 21 is not an object"):
        assert text in joined, text


def test_a_file_without_a_root_gets_one():
    data = tree()
    data["nodes"] = [node for node in data["nodes"] if node["type"] != "Root"]
    document = TreeDocument(data, CATALOG)
    assert document.node_type(document.root_id) == "Root" and document.node_ids()[0] == document.root_id
    assert "the file has no root node; a new root was created" in document.problems


def test_not_a_tree_file_is_refused():
    with pytest.raises(TreeFileError):
        TreeDocument({"format": "x"}, CATALOG)
    with pytest.raises(TreeFileError, match="newer version"):
        TreeDocument({"format": "behavior_tree_widget", "version": 3}, CATALOG)


def test_config_loading_and_saving():
    data = tree()
    data["config"] = {"tick_interval_ms": 0, "repeat": "yes", "log_level": 9, "cancel_timeout_ms": -1,
                      "on_unconfirmed_cancel": "maybe", "extra": 1}
    document = TreeDocument(data, CATALOG)
    assert document.to_file()["config"] == {"tick_interval_ms": 1, "repeat": False, "restore_blackboard": False,
                                             "default_memory": True}
    document.apply({"op": "doc.set_config", "config": {"log_level": 2, "wait_for_cancels": False}})
    assert list(document.to_file()["config"]) == ["tick_interval_ms", "repeat", "restore_blackboard", "default_memory",
                                                  "log_level", "wait_for_cancels"]


def test_to_wire(doc):
    wire = doc.to_wire(ENTRY_TYPES)
    json.dumps(wire, allow_nan=False)
    assert (wire["key"], wire["path"], wire["rev"], wire["version"], wire["unsaved"]) == ("robot", "robot_BT.json", 0, 2, False)
    assert wire["config"]["cancel_timeout_ms"] == 5000
    nodes = {node["id"]: node for node in wire["nodes"]}
    assert nodes[EVAL]["fields"] == {"ValueName": "counter", "CompareTo": None, "LiteralValue": {"type": "Integer", "value": 4},
                                     "Operator": "==", "Wait": False, "WaitTimeout": "0.0"}
    assert nodes[SET]["fields"]["LiteralValue"] == {"type": "Double", "value": "2.5"}
    assert nodes[PRINT]["fields"]["Source"] == "counter"  # an "entry" field: the name as it is
    assert nodes[MOVE]["fields"]["goal"] == "{'pose': {'x': 0.0}, 'points': [1, 2]}"
    assert nodes[MOVE]["field_selections"] == {"mode": 1}
    assert nodes[CALL]["bindings"] == {"Goal.pose": "speed"} and "wait_for_cancels" not in nodes[CALL]
    assert "unknown" not in nodes[CALL]
    assert [c["child"] for c in wire["connections"] if c["parent"] == ROOT] == [SEQ, SEL, PAR, NEG, REMOTE]


def test_node_size():
    assert node_size("Sequence") == (130, 102)
    assert node_size("Notes", {"width": 300, "height": 50}) == (300.0, 50.0)
    assert node_size("SomethingElse") == (246, 74)


# ============================================================================== G-01 to G-04
def test_G_01_a_cycle_is_refused_and_nothing_changes(doc):
    error = refused(doc, {"op": "conn.add", "parent": EVAL, "child": SEQ}, "takes no children")
    assert error.reason
    refused(doc, {"op": "conn.add", "parent": SEL, "child": ROOT}, "cannot be connected")
    doc.apply({"op": "conn.add", "parent": SEL, "child": NEG})
    refused(doc, {"op": "conn.add", "parent": NEG, "child": SEL}, "cycle")
    refused(doc, {"op": "conn.add", "parent": SEL, "child": SEL}, "cycle")


def test_G_02_a_second_child_of_a_negation_replaces_the_first(doc):
    applied = doc.apply({"op": "conn.add", "parent": NEG, "child": EVAL})
    assert doc.children(NEG) == [EVAL] and doc.parent(STOP) is None and doc.parent(EVAL) == NEG
    assert EVAL not in doc.children(SEQ)
    assert applied.op == {"op": "conn.add", "parent": NEG, "child": EVAL, "order": [EVAL]}
    assert applied.rev == 1 and doc.unsaved
    reloaded(doc)


def test_G_03_the_root_cannot_be_removed(doc):
    refused(doc, {"op": "node.remove", "ids": [STOP, ROOT]}, "Root cannot be removed")
    assert doc.has_node(STOP)


def test_G_04_structure_is_locked_while_running_but_moves_and_edits_are_not(doc):
    stop = {"id": new_id(), "type": "Stop", "title": "Stop", "x": 0.0, "y": 0.0}
    for op in ({"op": "node.add", "node": stop}, {"op": "node.remove", "ids": [STOP]},
               {"op": "conn.add", "parent": SEQ, "child": STOP}, {"op": "conn.remove", "parent": NEG, "child": STOP},
               {"op": "paste", "nodes": [stop], "connections": []}, {"op": "group.promote", "id": GROUP, "name": "x"},
               {"op": "composite.set_type", "id": SEQ, "composite": "Selector"},
               {"op": "composite.set_memory", "id": SEQ, "memory": False},
               {"op": "composite.set_policy", "id": PAR, "policy": "all"}):
        refused(doc, op, "executing", running=True)
    for op in ({"op": "node.move", "positions": {STOP: [5.0, 6.0]}}, {"op": "node.set_title", "id": STOP, "title": "Halt"},
               {"op": "node.set_field", "id": MOVE, "field": "speed", "value": 9},
               {"op": "node.set_text", "id": NOTE, "text": "running"},
               {"op": "node.set_color", "id": GROUP, "color": "#ff0000"},
               {"op": "node.resize", "id": GROUP, "width": 500, "height": 300},
               {"op": "doc.set_config", "config": {"repeat": True}}):
        doc.apply(op, running=True)
    assert doc.position(STOP) == (5.0, 6.0) and doc.rev == 7


def test_G_17_literals_through_parse_literal(doc):
    assert values.parse_literal("[1, 2.5, 'a']") == [1, 2.5, "a"]
    for text in ("__import__('os')", "x" * 2_000_001, "[" * 200 + "]" * 200):
        with pytest.raises(ValueError):
            values.parse_literal(text)
    # the same route for a field of kind "other"
    doc.apply({"op": "node.set_field", "id": MOVE, "field": "goal", "value": "{'p': [1, 2.5, 'a']}"})
    assert doc.fields(MOVE)["goal"] == {"p": [1, 2.5, "a"]}
    refused(doc, {"op": "node.set_field", "id": MOVE, "field": "goal", "value": "__import__('os')"}, "literal")
    refused(doc, {"op": "node.set_field", "id": MOVE, "field": "goal", "value": "x" * 2_000_001}, "longer")
    refused(doc, {"op": "node.set_field", "id": MOVE, "field": "goal", "value": "[" * 200 + "]" * 200}, "nested")


# ============================================================================== every operation
def test_node_add(doc):
    node_id = new_id()
    applied = doc.apply({"op": "node.add", "node": {"id": node_id, "type": "Mover", "x": 10, "y": 20,
                                                     "fields": {"x": "3.0", "mode": ["a", "b", "c"]},
                                                     "field_selections": {"mode": 2}, "bindings": {"speed": "counter"}}},
                        entry_types=ENTRY_TYPES)
    node = applied.op["node"]
    assert node["title"] == "Mover" and node["fields"]["x"] == "3.0" and node["field_selections"] == {"mode": 2}
    assert (node["x"], node["y"]) == (10.0, 20.0) and doc.parent(node_id) is None
    composite = new_id()
    applied = doc.apply({"op": "node.add", "node": {"id": composite, "type": "Sequence"}})
    assert applied.op["node"]["memory"] is True
    group = new_id()
    applied = doc.apply({"op": "node.add", "node": {"id": group, "type": "Group", "title": "group_001"}})
    assert applied.op["node"]["title"] == "group_005"  # counts on from the file's group_004
    assert applied.op["node"]["width"] == 360
    again = reloaded(doc)
    assert again.fields(node_id)["x"] == 3.0 and again.node_to_wire(node_id)["bindings"] == {"speed": "counter"}


@pytest.mark.parametrize(
    ("node", "match"),
    [({"id": "short", "type": "Stop"}, "32 hex"), ({"id": SEQ, "type": "Stop"}, "in use"),
     ({"id": "A" * 32, "type": "Stop"}, "32 hex"), ({"id": "e" * 32, "type": "Root"}, "one Root"),
     ({"id": "e" * 32, "type": "Nope"}, "unknown node type"), ({"id": "e" * 32, "type": "Mover", "fields": {"zz": 1}}, "no field"),
     ({"id": "e" * 32, "type": "Mover", "fields": {"speed": "fast"}}, "whole number"),
     ({"id": "e" * 32, "type": "Stop", "x": "1"}, "finite"), ({"id": "e" * 32, "type": "Stop", "title": 3}, "title"),
     ({"id": "e" * 32, "type": "Sequence", "memory": 1}, "memory"), ({"id": "e" * 32, "type": "Parallel", "policy": "most"}, "policy"),
     ({"id": "e" * 32, "type": "Group", "color": "blue"}, "colour"), ({"id": "e" * 32, "type": "Notes", "text": 3}, "text"),
     ({"id": "e" * 32, "type": "Stop", "bindings": {"x": "a.b"}}, "cannot contain"),
     ({"id": "e" * 32, "type": "Stop", "cancel_timeout_ms": -1}, "cancel_timeout_ms"),
     ({"id": "e" * 32, "type": "Mover", "field_selections": {"mode": 5}}, "out of range"), ("node", "object")],
)
def test_node_add_refusals(doc, node, match):
    refused(doc, {"op": "node.add", "node": node}, match)


def test_node_add_with_an_order(doc):
    node_id = new_id()
    applied = doc.apply({"op": "node.add", "node": {"id": node_id, "type": "Stop"}, "order": {SEQ: [SET, EVAL]}})
    assert applied.op["order"] == {SEQ: [SET, EVAL]} and doc.children(SEQ) == [SET, EVAL]
    refused(doc, {"op": "node.add", "node": {"id": new_id(), "type": "Stop"}, "order": {SEQ: [SET]}}, "exactly")


def test_node_remove(doc):
    applied = doc.apply({"op": "node.remove", "ids": [SEQ, NOTE, SEQ]})
    assert applied.op == {"op": "node.remove", "ids": [SEQ, NOTE]}
    assert not doc.has_node(SEQ) and doc.parent(EVAL) is None and SEQ not in doc.children(ROOT)
    reloaded(doc)
    refused(doc, {"op": "node.remove", "ids": ["nope"]}, "no node")
    refused(doc, {"op": "node.remove", "ids": []}, "ids")


def test_node_move(doc):
    applied = doc.apply({"op": "node.move", "positions": {EVAL: [-100, 1e9]}, "orders": {SEQ: [SET, EVAL]}}, rev=0)
    assert applied.op == {"op": "node.move", "positions": {EVAL: [-100.0, SCENE_LIMIT]}, "orders": {SEQ: [SET, EVAL]}}
    assert applied.reorder == [] and doc.children(SEQ) == [SET, EVAL]
    reloaded(doc)
    stale = doc.apply({"op": "node.move", "positions": {SET: [1, 2]}, "orders": {SEQ: [EVAL, SET]}}, rev=0)
    assert stale.reorder == [SEQ] and "orders" not in stale.op and doc.position(SET) == (1.0, 2.0)
    assert doc.children(SEQ) == [SET, EVAL]
    refused(doc, {"op": "node.move", "positions": {"nope": [1, 2]}}, "no node")
    refused(doc, {"op": "node.move", "positions": {SET: [1]}}, r"\[x, y\]")
    refused(doc, {"op": "node.move", "positions": {SET: [float("nan"), 1]}}, "finite")
    refused(doc, {"op": "node.move", "positions": {}, "orders": {SEQ: [EVAL, EVAL]}}, "exactly")
    refused(doc, {"op": "node.move", "positions": {}, "orders": {SEQ: [EVAL, SET, STOP]}}, "exactly")


def test_node_set_title(doc):
    assert doc.apply({"op": "node.set_title", "id": SEQ, "title": "Main"}).op["title"] == "Main"
    assert doc.apply({"op": "node.set_title", "id": SEQ, "title": ""}).op["title"] == "Sequence"
    refused(doc, {"op": "node.set_title", "id": SEQ, "title": None}, "text")
    refused(doc, {"op": "node.set_title", "id": "x", "title": "a"}, "no node")


def test_node_set_field_by_kind(doc):
    cases = [("x", "2.25", "2.25", 2.25), ("x", 3, "3.0", 3.0), ("speed", "12", 12, 12), ("avoid", False, False, False),
             ("label", "here", "here", "here"), ("mode", ["a", "b"], ["a", "b"], ["a", "b"]),
             ("goal", "{1: (2, 3)}", "{1: (2, 3)}", {1: (2, 3)}), ("goal", None, None, None)]
    for name, wire, normalised, value in cases:
        applied = doc.apply({"op": "node.set_field", "id": MOVE, "field": name, "value": wire})
        assert applied.op["value"] == normalised, name
        assert doc.fields(MOVE)[name] == value, name
    assert doc.node_to_wire(MOVE)["field_selections"] == {"mode": 1}  # still within the new choices
    doc.apply({"op": "node.set_field", "id": MOVE, "field": "mode", "value": ["only"]})
    again = reloaded(doc)
    assert again.fields(MOVE)["mode"] == ["only"] and again.file_item(MOVE)["field_selections"] == {"mode": 0}


def test_node_set_field_special_fields(doc):
    applied = doc.apply({"op": "node.set_field", "id": EVAL, "field": "LiteralValue",
                         "value": {"type": "Integer", "value": "7"}}, entry_types=ENTRY_TYPES)
    assert applied.op["value"] == {"type": "Integer", "value": 7}
    doc.apply({"op": "node.set_field", "id": EVAL, "field": "CompareTo", "value": "speed"})
    assert doc.fields(EVAL)["CompareTo"] == "speed"
    doc.apply({"op": "node.set_field", "id": EVAL, "field": "CompareTo", "value": ""})
    assert doc.fields(EVAL)["CompareTo"] is None
    doc.apply({"op": "node.set_field", "id": EVAL, "field": "Operator", "value": ">="})
    doc.apply({"op": "node.set_field", "id": EVAL, "field": "Wait", "value": True})
    doc.apply({"op": "node.set_field", "id": PRINT, "field": "Level", "value": 1})
    assert reloaded(doc).file_version() == 2
    refused(doc, {"op": "node.set_field", "id": EVAL, "field": "Operator", "value": "=~"}, "operator")
    refused(doc, {"op": "node.set_field", "id": EVAL, "field": "CompareTo", "value": 3}, "name of an entry")
    refused(doc, {"op": "node.set_field", "id": PRINT, "field": "Level", "value": 7}, "level")
    refused(doc, {"op": "node.set_field", "id": EVAL, "field": "LiteralValue", "value": {"type": "Integer", "value": "x"}},
            "whole number")
    refused(doc, {"op": "node.set_field", "id": EVAL, "field": "LiteralValue", "value": 4}, "typed")


def test_node_set_field_inside_a_dictionary(doc):
    applied = doc.apply({"op": "node.set_field", "id": MOVE, "field": "goal", "path": "pose.x", "kind": "float", "value": "1"})
    assert applied.op == {"op": "node.set_field", "id": MOVE, "field": "goal", "value": "1.0", "path": "pose.x", "kind": "float"}
    doc.apply({"op": "node.set_field", "id": MOVE, "field": "goal", "path": "points[1]", "kind": "int", "value": 7})
    doc.apply({"op": "node.set_field", "id": MOVE, "field": "goal", "path": "header.frame_id", "kind": "str", "value": "map"})
    assert doc.fields(MOVE)["goal"] == {"pose": {"x": 1.0}, "points": [1, 7], "header": {"frame_id": "map"}}
    reloaded(doc)
    refused(doc, {"op": "node.set_field", "id": MOVE, "field": "goal", "path": "points[5]", "kind": "int", "value": 1}, "list item")
    refused(doc, {"op": "node.set_field", "id": MOVE, "field": "goal", "path": "pose..x", "kind": "int", "value": 1}, "malformed")
    refused(doc, {"op": "node.set_field", "id": MOVE, "field": "speed", "path": "a", "kind": "int", "value": 1}, "dictionary")
    refused(doc, {"op": "node.set_field", "id": MOVE, "field": "goal", "path": "pose.x", "kind": "huge", "value": 1}, "kind")
    doc.apply({"op": "node.set_field", "id": MOVE, "field": "goal", "value": None})
    refused(doc, {"op": "node.set_field", "id": MOVE, "field": "goal", "path": "pose.x", "kind": "int", "value": 1},
            "dictionary")


def test_node_set_field_refusals(doc):
    refused(doc, {"op": "node.set_field", "id": SEQ, "field": "x", "value": 1}, "not a leaf")
    refused(doc, {"op": "node.set_field", "id": MOVE, "field": "nope", "value": 1}, "no field")
    refused(doc, {"op": "node.set_field", "id": MOVE, "field": "speed", "value": 2**40}, "32-bit")
    refused(doc, {"op": "node.set_field", "id": MOVE, "field": "avoid", "value": "yes"}, "true or false")
    refused(doc, {"op": "node.set_field", "id": MOVE, "field": "mode", "value": "a"}, "list of texts")
    data = tree()
    data["nodes"].append({"id": "p" * 32, "type": "Laser", "title": "L", "x": 0, "y": 0, "fields": {"a": 1}})
    document = TreeDocument(data, CATALOG)
    refused(document, {"op": "node.set_field", "id": "p" * 32, "field": "a", "value": 2}, "unknown here")


def test_node_set_selection(doc):
    assert doc.apply({"op": "node.set_selection", "id": MOVE, "field": "mode", "index": 0}).op["index"] == 0
    assert reloaded(doc).file_item(MOVE)["field_selections"] == {"mode": 0}
    refused(doc, {"op": "node.set_selection", "id": MOVE, "field": "mode", "index": 2}, "out of range")
    refused(doc, {"op": "node.set_selection", "id": MOVE, "field": "speed", "index": 0}, "not a choice")
    refused(doc, {"op": "node.set_selection", "id": MOVE, "field": "mode", "index": True}, "out of range")


def test_node_set_binding_and_output(doc):
    doc.apply({"op": "node.set_binding", "id": MOVE, "key": "goal.pose.x", "entry": "speed"})
    doc.apply({"op": "node.set_output", "id": CALL, "key": "Feedback.distance", "entry": "distance"})
    applied = doc.apply({"op": "node.set_binding", "id": CALL, "key": "Goal.pose", "entry": None})
    assert applied.op["entry"] is None
    assert "bindings" not in doc.file_item(CALL) and doc.file_item(CALL)["outputs"] == {
        "Result.code": "counter", "Feedback.distance": "distance"}
    doc.apply({"op": "node.set_binding", "id": REMOTE, "key": "speed_limit", "entry": "speed"})
    assert reloaded(doc).file_item(MOVE)["bindings"] == {"goal.pose.x": "speed"}
    refused(doc, {"op": "node.set_binding", "id": MOVE, "key": "nope", "entry": "speed"}, "names no field")
    refused(doc, {"op": "node.set_binding", "id": MOVE, "key": "speed", "entry": "a/b"}, "cannot contain")
    refused(doc, {"op": "node.set_output", "id": CALL, "key": "Goal.x", "entry": "a"}, "starts with")
    refused(doc, {"op": "node.set_binding", "id": REMOTE, "key": "a.b", "entry": "a"}, "remote entry")
    refused(doc, {"op": "node.set_binding", "id": SEQ, "key": "a", "entry": "a"}, "not a leaf")


def test_node_set_option(doc):
    doc.apply({"op": "node.set_option", "id": STOP, "name": "wait_for_cancels", "value": False})
    doc.apply({"op": "node.set_option", "id": STOP, "name": "cancel_timeout_ms", "value": 3000})
    item = reloaded(doc).file_item(STOP)
    assert (item["wait_for_cancels"], item["cancel_timeout_ms"]) == (False, 3000)
    doc.apply({"op": "node.set_option", "id": STOP, "name": "wait_for_cancels", "value": None})
    assert "wait_for_cancels" not in doc.file_item(STOP)
    refused(doc, {"op": "node.set_option", "id": STOP, "name": "cancel_timeout_ms", "value": 1.5}, "whole number")
    refused(doc, {"op": "node.set_option", "id": STOP, "name": "speed", "value": 1}, "unknown option")
    refused(doc, {"op": "node.set_option", "id": STOP, "name": "speed", "value": None}, "unknown option")


def test_notes_and_groups(doc):
    applied = doc.apply({"op": "node.resize", "id": NOTE, "width": 10, "height": 5000.4, "x": 3, "y": 4})
    assert applied.op == {"op": "node.resize", "id": NOTE, "width": 60, "height": 2000, "x": 3.0, "y": 4.0}
    assert doc.apply({"op": "node.resize", "id": GROUP, "width": 10, "height": 10}).op["width"] == 120
    assert doc.apply({"op": "node.set_text", "id": NOTE, "text": "é ✓\nline"}).op["text"] == "é ✓\nline"
    assert doc.apply({"op": "node.set_color", "id": GROUP, "color": "#ABC"}).op["color"] == "#aabbcc"
    item = reloaded(doc).file_item(GROUP)
    assert (item["width"], item["height"], item["color"]) == (120, 60, "#aabbcc")
    refused(doc, {"op": "node.resize", "id": SEQ, "width": 100, "height": 100}, "Notes or Group")
    refused(doc, {"op": "node.resize", "id": NOTE, "width": "1", "height": 100}, "finite")
    refused(doc, {"op": "node.set_text", "id": GROUP, "text": "x"}, "Notes")
    refused(doc, {"op": "node.set_text", "id": NOTE, "text": 1}, "text")
    refused(doc, {"op": "node.set_color", "id": GROUP, "color": "#12345"}, "colour")
    refused(doc, {"op": "node.set_color", "id": NOTE, "color": "#123456"}, "Group")


def test_composite_operations(doc):
    applied = doc.apply({"op": "composite.set_type", "id": SEQ, "composite": "Parallel"})
    assert applied.op == {"op": "composite.set_type", "id": SEQ, "composite": "Parallel", "title": "Parallel"}
    assert doc.file_item(SEQ)["policy"] == "all"
    doc.apply({"op": "composite.set_policy", "id": SEQ, "policy": "any"})
    doc.apply({"op": "composite.set_type", "id": SEQ, "composite": "Selector"})
    doc.apply({"op": "composite.set_memory", "id": SEQ, "memory": False})
    doc.apply({"op": "composite.set_type", "id": ROOT, "composite": "Selector"})
    doc.apply({"op": "composite.set_memory", "id": ROOT, "memory": False})
    again = reloaded(doc)
    assert again.file_item(SEQ) == {**again.file_item(SEQ), "type": "Selector", "memory": False}
    assert again.file_item(ROOT)["composite"] == "Selector" and not again.file_item(ROOT)["memory"]
    doc.apply({"op": "node.set_title", "id": SEL, "title": "Fallback"})
    assert doc.apply({"op": "composite.set_type", "id": SEL, "composite": "Sequence"}).op["title"] == "Fallback"
    refused(doc, {"op": "composite.set_type", "id": ROOT, "composite": "Parallel"}, "can be Sequence, Selector")
    refused(doc, {"op": "composite.set_type", "id": NEG, "composite": "Sequence"}, "is not a")
    refused(doc, {"op": "composite.set_memory", "id": PAR, "memory": False}, "is not a")
    refused(doc, {"op": "composite.set_memory", "id": SEL, "memory": "no"}, "true or false")
    refused(doc, {"op": "composite.set_policy", "id": SEL, "policy": "all"}, "is not a Parallel")
    refused(doc, {"op": "composite.set_policy", "id": PAR, "policy": "most"}, "policy")


def test_conn_add_and_remove(doc):
    applied = doc.apply({"op": "conn.add", "parent": SEQ, "child": PRINT, "order": [PRINT, EVAL, SET]})
    assert applied.op["order"] == [PRINT, EVAL, SET] and doc.children(SEL) == []  # one parent: the old one is replaced
    again = doc.apply({"op": "conn.add", "parent": SEQ, "child": PRINT})  # connected again: nothing moves
    assert again.op["order"] == [PRINT, EVAL, SET]
    doc.apply({"op": "conn.add", "parent": SEL, "child": STOP})
    assert doc.children(NEG) == [] and doc.children(SEL) == [STOP]
    reloaded(doc)
    assert doc.apply({"op": "conn.remove", "parent": SEQ, "child": EVAL}).op == {
        "op": "conn.remove", "parent": SEQ, "child": EVAL}
    assert doc.parent(EVAL) is None
    refused(doc, {"op": "conn.remove", "parent": SEQ, "child": EVAL}, "no such connection")
    refused(doc, {"op": "conn.add", "parent": SEQ, "child": NOTE}, "cannot be connected")
    refused(doc, {"op": "conn.add", "parent": SEQ, "child": "nope"}, "no node")
    refused(doc, {"op": "conn.add", "parent": SEQ, "child": EVAL, "order": [EVAL]}, "exactly")


def test_paste(doc):
    a, b, g1, g2, unknown = (new_id() for _ in range(5))
    op = {"op": "paste", "nodes": [
        {"id": a, "type": "Sequence", "title": "Copy", "x": 0, "y": 0, "memory": False},
        {"id": b, "type": "Mover", "x": 0, "y": 100, "fields": {"x": "4.5"}},
        {"id": g1, "type": "Group", "title": "group_002", "x": 0, "y": 0},
        {"id": g2, "type": "Group", "title": "kitchen", "x": 0, "y": 0},
        {"id": unknown, "type": "Laser", "title": "L", "x": 0, "y": 0, "unknown": True, "fields": {"r": 1}, "cal": 3},
    ], "connections": [{"parent": a, "child": b}, {"parent": a, "child": unknown}]}
    applied = doc.apply(op)
    titles = {node["id"]: node["title"] for node in applied.op["nodes"]}
    assert (titles[g1], titles[g2]) == ("group_005", "kitchen")
    assert applied.op["connections"] == [{"parent": a, "child": b}, {"parent": a, "child": unknown}]
    assert doc.is_placeholder(unknown) and doc.file_item(unknown)["cal"] == 3
    again = doc.apply({**op, "nodes": [{**node, "id": new_id()} for node in op["nodes"][2:3]], "connections": []})
    assert again.op["nodes"][0]["title"] == "group_006"
    assert TreeDocument(doc.to_file(), CATALOG).to_file() == doc.to_file()
    refused(doc, {**op}, "in use")
    refused(doc, {"op": "paste", "nodes": [{"id": new_id(), "type": "Root"}], "connections": []}, "nothing to paste")
    refused(doc, {"op": "paste", "nodes": [], "connections": []}, "needs nodes")
    twice = new_id()
    refused(doc, {"op": "paste", "nodes": [{"id": twice, "type": "Stop"}, {"id": twice, "type": "Stop"}], "connections": []},
            "in use")


def test_doc_set_config(doc):
    applied = doc.apply({"op": "doc.set_config", "config": {"tick_interval_ms": 50, "on_unconfirmed_cancel": "continue"}})
    assert applied.op == {"op": "doc.set_config", "config": {"tick_interval_ms": 50, "on_unconfirmed_cancel": "continue"}}
    assert reloaded(doc).config["on_unconfirmed_cancel"] == "continue"
    for config, match in (({"tick_interval_ms": 0}, "valid"), ({"repeat": 1}, "valid"), ({"log_level": 5}, "valid"),
                          ({"cancel_timeout_ms": -5}, "valid"), ({"on_unconfirmed_cancel": "x"}, "valid"),
                          ({"colour": 1}, "unknown option"), ([], "object")):
        refused(doc, {"op": "doc.set_config", "config": config}, match)


def test_unknown_and_malformed_operations(doc):
    refused(doc, {"op": "node.explode"}, "unknown operation")
    refused(doc, {"op": "bb.add"}, "unknown operation")
    refused(doc, {"no": "op"}, "'op'")
    refused(doc, {"op": "group.promote", "id": GROUP, "name": "x"}, "promote")


def test_apply_all_is_all_or_nothing(doc):
    before = state(doc)
    with pytest.raises(OpRefused):
        doc.apply_all([{"op": "node.set_title", "id": SEQ, "title": "A"}, {"op": "node.remove", "ids": [ROOT]}])
    assert state(doc) == before and doc.title(SEQ) == "Sequence"
    applied = doc.apply_all([{"op": "node.set_title", "id": SEQ, "title": "A"}, {"op": "node.remove", "ids": [STOP]}])
    assert [item.rev for item in applied] == [1, 2]


def test_rename_entry_updates_fields_bindings_and_outputs(doc):
    data = tree()
    document = TreeDocument(data, CATALOG)
    applied = document.rename_entry("counter", "count")
    ops = [item.op for item in applied]
    assert {"op": "node.set_field", "id": EVAL, "field": "ValueName", "value": "count"} in ops
    assert {"op": "node.set_field", "id": PRINT, "field": "Source", "value": "count"} in ops
    assert {"op": "node.set_output", "id": CALL, "key": "Result.code", "entry": "count"} in ops
    assert {"op": "node.set_binding", "id": REMOTE, "key": "dock_id", "entry": "count"} in ops
    assert len(ops) == 4 and document.rev == 4
    assert document.fields(PRINT)["Source"] == "count" and document.file_item(CALL)["outputs"]["Result.code"] == "count"
    assert document.rename_entry("nothing", "else") == []


def test_set_field_file_takes_a_value_in_file_encoding(doc):
    applied = doc.set_field_file(MOVE, "goal", {"__tuple__": [1, 2]})
    assert doc.fields(MOVE)["goal"] == (1, 2) and applied.op["value"] == "(1, 2)" and not doc.unsaved
    applied = doc.set_field_file(EVAL, "LiteralValue", 5, ENTRY_TYPES)
    assert applied.op["value"] == {"type": "Integer", "value": 5}
    with pytest.raises(OpRefused):
        doc.set_field_file(MOVE, "nope", 1)


def test_the_document_is_not_shared_with_the_data_it_was_loaded_from():
    data = tree()
    original = copy.deepcopy(data)
    document = TreeDocument(data, CATALOG)
    document.apply({"op": "node.set_field", "id": MOVE, "field": "goal", "path": "pose.x", "kind": "float", "value": "9"})
    assert data == original


# ============================================================================== review findings
def test_review_2_a_written_order_against_the_positions_is_warned():
    data = tree()
    document = TreeDocument({**data, "nodes": [node for node in data["nodes"] if node["type"] in ("Root", "Stop")],
                             "connections": [], "version": 1}, CATALOG)
    left = new_id()
    document.apply({"op": "node.add", "node": {"id": left, "type": "Pause", "title": "left", "x": 0.0, "y": 400.0}})
    document.apply({"op": "node.set_title", "id": STOP, "title": "right"})
    document.apply({"op": "conn.add", "parent": ROOT, "child": STOP})
    document.apply({"op": "conn.add", "parent": ROOT, "child": left})  # no order: appended, but drawn left
    (warning,) = document.save_warnings()
    assert "'Root'" in warning and "'right', 'left'" in warning and "left to right: 'left', 'right'" in warning
    document.apply({"op": "node.move", "positions": {}, "orders": {ROOT: [left, STOP]}})
    assert document.save_warnings() == []


def test_review_3_widths_follow_the_title_and_the_fields():
    """Widths measured with the widget 0.10.0 (Segoe UI 9 pt; Qt's offscreen fallback font)."""
    nodes = [
        {"id": ROOT, "type": "Root", "title": "Root", "x": 0.0, "y": 0.0, "composite": "Sequence", "memory": True},
        {"id": SEQ, "type": "Sequence", "title": "Drive to the charging dock and wait", "x": 0.0, "y": 200.0, "memory": True},
        {"id": SEL, "type": "Sequence", "title": "A", "x": 60.0, "y": 200.0, "memory": True},
        {"id": MOVE, "type": "Mover", "title": "Mover", "x": 400.0, "y": 200.0, "fields": {}},
        {"id": STOP, "type": "Stop", "title": "Stop", "x": 470.0, "y": 200.0, "fields": {}},
        {"id": EVAL, "type": "Sub-Tree", "title": "Sub-Tree", "x": 2000.0, "y": 200.0, "fields": {"File": ""}},
    ]
    data = {"format": "behavior_tree_widget", "version": 1, "nodes": nodes,
            "connections": [{"parent": ROOT, "child": child} for child in (SEQ, SEL, MOVE, STOP)]}
    document = TreeDocument(data, CATALOG)
    assert document.widget_widths(ROOT) == (119, 210)
    assert document.widget_widths(SEQ) == (282, 582)
    assert document.widget_widths(SEL) == (102, 174)
    assert document.widget_widths(MOVE) == (288, 482)  # its float field's row
    assert document.widget_widths(STOP) == (85, 130)
    assert document.widget_widths(EVAL) == (221, 352)
    (warning,) = document.save_warnings()
    assert "left to right: 'A', 'Drive to the charging dock and wait', 'Stop', 'Mover'" in warning
    document.apply({"op": "node.set_field", "id": MOVE, "field": "mode", "value": ["abcdefghijklmnopqrstuvwxyzabcdefghijkl"]})
    assert document.widget_widths(MOVE) == (381, 716)


def test_review_3_siblings_close_under_either_font_are_named():
    data = tree()
    stop = next(node for node in data["nodes"] if node["id"] == STOP)
    document = TreeDocument({**data, "version": 1, "nodes": [data["nodes"][0], stop,
                                                            {**stop, "id": "f" * 32, "type": "Pause", "title": "P", "x": 1630.0}],
                             "connections": [{"parent": ROOT, "child": STOP}, {"parent": ROOT, "child": "f" * 32}]}, CATALOG)
    (warning,) = document.save_warnings()
    assert "'Stop' and 'P' below 'Root' are less than 40 pixels apart" in warning


def test_review_4_a_rename_is_not_refused_for_a_stale_binding():
    data = tree()
    data["nodes"][8]["bindings"] = {"velocity": "counter"}  # names no field of Mover any more
    document = TreeDocument(data, CATALOG)
    ops = [item.op for item in document.rename_entry("counter", "count")]
    assert {"op": "node.set_binding", "id": MOVE, "key": "velocity", "entry": "count"} in ops
    assert document.fields(EVAL)["ValueName"] == "count" and document.fields(PRINT)["Source"] == "count"
    assert [item.rev for item in document.rename_entry("count", "n")] == [6, 7, 8, 9, 10]


@pytest.mark.parametrize("wire", ["5", "[1, 2]", "'text'", "True"])
def test_review_5_values_the_file_could_not_keep_are_refused(doc, wire):
    refused(doc, {"op": "node.set_field", "id": MOVE, "field": "goal", "value": wire}, "could not keep")
    doc.apply({"op": "node.set_field", "id": MOVE, "field": "goal", "value": "(1, 2)"})  # the widget keeps it too
    assert reloaded(doc).fields(MOVE)["goal"] == (1, 2)


def test_review_5_a_call_action_goal_stays_a_dictionary(doc):
    refused(doc, {"op": "node.set_field", "id": CALL, "field": "Goal", "value": "[1]"}, "could not keep")
    doc.apply({"op": "node.set_field", "id": CALL, "field": "Goal", "value": "{'pose': {'x': 1.0}}"})
    assert reloaded(doc).fields(CALL)["Goal"] == {"pose": {"x": 1.0}}


def test_review_6_frozensets_and_tuple_literals_survive_a_paste_in_wire_form():
    data = tree()
    data["nodes"][8]["fields"]["goal"] = {"a": {"__frozenset__": [1, 2]}, "b": {"__tuple__": [3]}}
    data["nodes"][6]["fields"]["LiteralValue"] = {"__tuple__": [1, 2]}
    document = TreeDocument(data, CATALOG)
    assert document.problems == []
    for source in (MOVE, SET):
        copy_id = new_id()
        wire = json.loads(json.dumps({**document.node_to_wire(source, {"speed": "List"}), "id": copy_id}))
        document.apply({"op": "paste", "encoding": "wire", "nodes": [wire], "connections": []})
        assert document.file_item(copy_id)["fields"] == document.file_item(source)["fields"]
    assert values.parse_literal("frozenset({frozenset({1}), 2})") == frozenset({frozenset({1}), 2})
    assert values.parse_literal("frozenset()") == frozenset()
    for unsafe in ("frozenset('ab')", "frozenset(x)", "frozenset(range(3))", "frozenset({1}, k=2)", "set.mro()"):
        with pytest.raises(ValueError):
            values.parse_literal(unsafe)


def test_review_8_a_placeholder_pasted_in_wire_form_keeps_non_finite_numbers():
    data = tree()
    odd = {"id": "f" * 32, "type": "Odd", "title": "Odd", "x": 0.0, "y": 0.0,
           "fields": {"v": float("nan"), "w": [float("inf"), float("-inf")]}, "__float__": "nan",
           "k": {"__float__": "inf"}}
    data["nodes"].append(odd)
    document = TreeDocument(data, CATALOG)
    wire = document.node_to_wire(odd["id"])
    text = json.dumps(wire, allow_nan=False)  # a browser can parse it
    copy_id = new_id()
    document.apply({"op": "paste", "encoding": "wire", "nodes": [{**json.loads(text), "id": copy_id}], "connections": []})
    saved = document.file_item(copy_id)
    assert json.dumps({**saved, "id": odd["id"]}) == json.dumps(odd)


def test_review_9_removing_a_connection_that_does_not_exist_is_refused(doc):
    refused(doc, {"op": "conn.remove", "child": GROUP}, "no such connection")
    refused(doc, {"op": "conn.remove", "parent": None, "child": "zz"}, "no such connection")
    refused(doc, {"op": "conn.remove", "parent": SEQ, "child": PRINT}, "no such connection")


def test_review_10_a_paste_leaves_out_a_root_and_invalid_connections(doc):
    stop, pause = new_id(), new_id()
    applied = doc.apply({"op": "paste", "encoding": "wire", "nodes": [
        {**doc.node_to_wire(ROOT), "id": new_id()}, {"id": stop, "type": "Stop", "x": 0, "y": 0},
        {"id": pause, "type": "Pause", "x": 0, "y": 0}],
        "connections": [{"parent": stop, "child": pause}, {"parent": SEQ, "child": stop}]})
    assert [node["id"] for node in applied.op["nodes"]] == [stop, pause] and applied.op["connections"] == []
    assert len(applied.problems) == 3 and "Root" in applied.problems[0]
    assert doc.parent(stop) is None and doc.node_type(doc.root_id) == "Root"
