"""Promote to Sub-Tree with the widget's rules and refusals (F-10)."""

from __future__ import annotations

import pytest

from bt_server_core import node_types
from bt_server_core.document import OpRefused, TreeDocument
from bt_server_core.files import load_tree, subtree_key, write_tree_file
from bt_server_core.promote import promote

CATALOG = node_types.builtin_catalog()
ROOT, SEL, A, B, C, GROUP, NOTE = (f"{n:x}" * 32 for n in range(7))


def item(node_id, type_name, x, y, **extra) -> dict:
    return {"id": node_id, "type": type_name, "title": type_name, "x": x, "y": y, **extra}


def source(parent_type: str = "Selector", memory: bool = False, order=(A, B, C), key: str = "robot") -> TreeDocument:
    """Root -> a Selector with Memory off (or ``parent_type``) -> A, B, C; a group around B, C
    and a note; A lies outside the group."""
    parent = item(SEL, parent_type, 0.0, 200.0, **({"policy": "all"} if parent_type == "Parallel" else {"memory": memory}))
    data = {
        "format": "behavior_tree_widget",
        "version": 1,
        "config": {"tick_interval_ms": 40, "repeat": True, "restore_blackboard": True, "default_memory": True},
        "view": {"zoom": 0.5, "center": [0.0, 0.0]},
        "nodes": [
            item(ROOT, "Root", 0.0, 0.0, composite="Sequence", memory=True),
            parent,
            item(A, "Evaluation", -500.0, 400.0, fields={"ValueName": "go", "CompareTo": None, "LiteralValue": True}),
            item(B, "Stop", 0.0, 400.0, fields={}),
            item(C, "Pause", 300.0, 400.0, fields={}, wait_for_cancels=False),
            item(GROUP, "Group", -50.0, 350.0, width=600, height=300, color="#3d7cc9", title="patrol"),
            item(NOTE, "Notes", 100.0, 500.0, text="inside", width=220, height=120),
        ],
        "connections": [{"parent": ROOT, "child": SEL}] + [{"parent": SEL, "child": child} for child in order],
    }
    document = TreeDocument(data, CATALOG, key=key)
    assert document.problems == []
    return document


def test_F_10_a_group_below_a_selector_without_memory_keeps_running_as_before(tmp_path):
    document = source()
    data, ops = promote(document, GROUP, "patrol", tmp_path)
    # the new tree: a Root that is a Selector with Memory off over B and C, in their order
    write_tree_file(tmp_path / "patrol_BT.json", data)
    new = TreeDocument(load_tree(tmp_path, "patrol").data, CATALOG, key="patrol")
    root = new.root_id
    assert new.file_item(root)["composite"] == "Selector" and new.file_item(root)["memory"] is False
    assert new.children(root) == [B, C]
    assert new.has_node(NOTE) and not new.has_node(GROUP) and not new.has_node(A)
    assert new.file_item(C)["wait_for_cancels"] is False and data["version"] == 2  # a node's data goes along
    assert data["config"] == {"tick_interval_ms": 40, "repeat": False, "restore_blackboard": False, "default_memory": True}
    assert data["view"]["zoom"] == 0.5
    # the tree: the Sub-Tree node takes the group's place among the Selector's children
    applied = document.apply_all(ops)
    assert [item.op["op"] for item in applied] == ["node.remove", "node.add", "conn.add"]
    sub = applied[1].op["node"]["id"]
    assert document.children(SEL) == [A, sub]
    assert document.node_type(sub) == "Sub-Tree" and document.title(sub) == "patrol"
    assert document.fields(sub) == {"File": "patrol_BT.json"}
    assert subtree_key(document.key, document.fields(sub)["File"]) == "patrol"
    assert not any(document.has_node(node_id) for node_id in (GROUP, B, C, NOTE))
    assert document.file_item(SEL)["memory"] is False


def test_the_sub_tree_node_keeps_the_place_between_siblings(tmp_path):
    other = "e" * 32
    document = source(order=(B, C, A))
    document.apply({"op": "node.add", "node": {"id": other, "type": "Stop", "x": 900.0, "y": 400.0}})
    document.apply({"op": "conn.add", "parent": SEL, "child": other})
    data, ops = promote(document, GROUP, "patrol", tmp_path)
    document.apply_all(ops)
    sub = ops[1]["node"]["id"]
    assert document.children(SEL) == [sub, A, other]


def test_tops_without_a_parent_all_become_children_of_a_sequence(tmp_path):
    document = source()
    document.apply({"op": "conn.remove", "parent": SEL, "child": B})
    document.apply({"op": "conn.remove", "parent": SEL, "child": C})
    data, ops = promote(document, GROUP, "patrol", tmp_path)
    new = TreeDocument(data, CATALOG)
    assert new.file_item(new.root_id)["composite"] == "Sequence" and new.file_item(new.root_id)["memory"] is True
    assert new.children(new.root_id) == [B, C]  # left to right
    assert [op["op"] for op in ops] == ["node.remove", "node.add"]


def test_a_root_parent_gives_its_type_and_memory(tmp_path):
    document = source()
    document.apply({"op": "composite.set_type", "id": ROOT, "composite": "Selector"})
    document.apply({"op": "conn.add", "parent": ROOT, "child": B})
    document.apply({"op": "conn.add", "parent": ROOT, "child": C})
    data, _ = promote(document, GROUP, "patrol", tmp_path)
    root = next(node for node in data["nodes"] if node["type"] == "Root")
    assert (root["composite"], root["memory"]) == ("Selector", True)


def test_files_in_other_folders_are_named_relative_to_the_tree(tmp_path):
    document = source(key="cells/robot")
    _, ops = promote(document, GROUP, "cells/patrol", tmp_path)
    assert ops[1]["node"]["fields"]["File"] == "patrol_BT.json"
    _, ops = promote(document, GROUP, "common/patrol", tmp_path)
    assert ops[1]["node"]["fields"]["File"] == "../common/patrol_BT.json"
    assert subtree_key("cells/robot", ops[1]["node"]["fields"]["File"]) == "common/patrol"


def test_an_existing_file_is_replaced_only_when_asked(tmp_path):
    (tmp_path / "patrol_BT.json").write_text("{}", encoding="utf-8")
    with pytest.raises(FileExistsError):
        promote(source(), GROUP, "patrol", tmp_path)
    data, _ = promote(source(), GROUP, "patrol", tmp_path, overwrite=True)
    assert data["format"] == "behavior_tree_widget"


def refusal(document, match, *, key="patrol", **options):
    before = document.to_file()
    with pytest.raises(OpRefused, match=match):
        promote(document, options.pop("group", GROUP), key, options.pop("root"), **options)
    assert document.to_file() == before


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"op": "node.move", "positions": {GROUP: [-2000.0, -2000.0]}}, "no node lies completely inside"),
        ({"op": "node.move", "positions": {ROOT: [0.0, 400.0]}}, "the Root lies inside the group"),
        ({"op": "node.move", "positions": {C: [2000.0, 400.0]}}, None),  # C outside: B alone is promoted
    ],
)
def test_group_contents_decide(tmp_path, change, match):
    document = source()
    document.apply(change)
    if match is None:
        data, _ = promote(document, GROUP, "patrol", tmp_path)
        assert {node["id"] for node in data["nodes"]} >= {B} and C not in {node["id"] for node in data["nodes"]}
    else:
        refusal(document, match, root=tmp_path)


def test_the_widget_refusals(tmp_path):
    document = source()
    refusal(document, "executing", root=tmp_path, running=True)
    refusal(document, "only a group", root=tmp_path, group=B)
    refusal(document, "only a group", root=tmp_path, group="nope")
    refusal(document, "cannot be used as a file name", root=tmp_path, key="pa:trol")
    refusal(document, "cannot be used as a file name", root=tmp_path, key="patrol.")
    refusal(document, "names the file of this tree", root=tmp_path, key="robot")
    refusal(document, "names the file of this tree", root=tmp_path, key="main", open_keys=["main"])
    refusal(document, "cannot name a tree", root=tmp_path, key="patrol_BT.json")
    refusal(document, "outside", root=tmp_path, key="../patrol")
    # a node inside with a child outside
    document.apply({"op": "node.add", "node": {"id": "f" * 32, "type": "Sequence", "x": 100.0, "y": 360.0}})
    document.apply({"op": "conn.add", "parent": "f" * 32, "child": A})
    refusal(document, "has children outside the group", root=tmp_path)


def test_tops_with_two_parents_or_a_node_between_them_are_refused(tmp_path):
    document = source()
    document.apply({"op": "conn.add", "parent": ROOT, "child": C})
    refusal(document, "more than one node outside", root=tmp_path)
    refusal(source(order=(B, A, C)), "runs between the group's nodes", root=tmp_path)


def test_two_nodes_below_a_parallel_are_refused(tmp_path):
    refusal(source(parent_type="Parallel"), "run them in parallel", root=tmp_path)


def test_sizes_from_the_browser_decide_what_lies_inside(tmp_path):
    document = source()
    data, _ = promote(document, GROUP, "patrol", tmp_path, sizes={C: (2000.0, 50.0)})
    assert C not in {node["id"] for node in data["nodes"]}
