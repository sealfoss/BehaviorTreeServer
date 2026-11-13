"""Names, paths confined to the trees folder (G-16), atomic writes, reading files (design 9.4, 12, 13)."""

from __future__ import annotations

import json
import os

import pytest

from bt_server_core.files import (
    PathError,
    TreeFileError,
    atomic_write_bytes,
    atomic_write_json,
    blackboard_file_path,
    canonical_key,
    document_key,
    find_tree_file,
    key_of_file,
    load_tree,
    parse_blackboard,
    parse_source,
    read_blackboard_file,
    read_tree_file,
    resolve_in_root,
    subtree_key,
    tree_file_path,
    tree_name,
)


@pytest.mark.parametrize("name", ["robot", "robot.json", "robot_BT.json", "robot_BB.json", "ROBOT_bt.JSON"])
def test_the_four_spellings_name_one_tree(name):
    assert document_key(name).lower() == "robot"


@pytest.mark.parametrize(
    ("name", "key"),
    [("cells/patrol_BT.json", "cells/patrol"), ("cells\\patrol", "cells/patrol"), ("./a/../b_BB.json", "b"),
     ("_BT.json", "_BT"), ("x_BT", "x")],
)
def test_document_keys(name, key):
    assert document_key(name) == key


def test_tree_name_keeps_a_name_that_is_only_a_suffix():
    assert tree_name("_BT.json") == "_BT"


@pytest.mark.parametrize(
    "name",
    ["../../etc/passwd", "..", "a/../../b", "/etc/passwd", "\\\\server\\share\\x", "C:\\trees\\x", "C:x", "", "   ",
     "a\x00b", "."],
)
def test_G_16_names_outside_the_trees_folder_are_refused(name, tmp_path):
    with pytest.raises(PathError):
        document_key(name)
    with pytest.raises(PathError):
        resolve_in_root(tmp_path, name)


def test_G_16_a_symbolic_link_leading_out_is_refused(tmp_path):
    root, outside = tmp_path / "trees", tmp_path / "secret"
    root.mkdir()
    outside.mkdir()
    (outside / "x_BT.json").write_text("{}", encoding="utf-8")
    try:
        os.symlink(outside, root / "link", target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links cannot be made here")
    with pytest.raises(PathError):
        tree_file_path(root, "link/x")
    with pytest.raises(PathError):
        load_tree(root, "link/x")
    inside = root / "cells"
    inside.mkdir()
    os.symlink(inside, root / "alias", target_is_directory=True)
    assert tree_file_path(root, "alias/p") == (inside / "p_BT.json").resolve()


def test_paths_inside_the_root(tmp_path):
    assert tree_file_path(tmp_path, "cells/patrol") == (tmp_path / "cells" / "patrol_BT.json").resolve()
    assert blackboard_file_path(tmp_path, "robot") == (tmp_path / "robot_BB.json").resolve()
    (tmp_path / "cells").mkdir()
    (tmp_path / "cells" / "patrol_BT.json").write_text("{}", encoding="utf-8")
    assert key_of_file(tmp_path / "cells" / "patrol_BT.json", tmp_path) == "cells/patrol"
    with pytest.raises(PathError):
        key_of_file(tmp_path.parent / "x_BT.json", tmp_path)


def test_subtree_files_are_relative_to_the_holding_tree():
    assert subtree_key("cells/robot", "patrol_BT.json") == "cells/patrol"
    assert subtree_key("cells/robot", "..\\common\\dock.json") == "common/dock"
    assert subtree_key("robot", "sub/patrol") == "sub/patrol"
    for bad in ("", "C:/x_BT.json", "/x_BT.json", "../x_BT.json"):
        with pytest.raises(PathError):
            subtree_key("robot", bad)


def test_find_tree_file_prefers_the_tree_file_over_an_early_single_file(tmp_path):
    assert find_tree_file(tmp_path, "robot") is None
    (tmp_path / "robot.json").write_text("{}", encoding="utf-8")
    assert find_tree_file(tmp_path, "robot").name == "robot.json"
    (tmp_path / "robot_BT.json").write_text("{}", encoding="utf-8")
    assert find_tree_file(tmp_path, "robot").name == "robot_BT.json"


# ============================================================================== atomic writes
def test_atomic_write_replaces_the_file_and_leaves_no_temporary_file(tmp_path):
    path = tmp_path / "a_BT.json"
    path.write_text("old", encoding="utf-8")
    atomic_write_json(path, {"x": float("nan"), "text": "é"})
    assert path.read_text(encoding="utf-8") == '{\n  "x": NaN,\n  "text": "é"\n}\n'
    assert [item.name for item in tmp_path.iterdir()] == ["a_BT.json"]


def test_a_failed_write_keeps_the_old_file(tmp_path, monkeypatch):
    path = tmp_path / "a_BT.json"
    path.write_bytes(b"old")

    def broken(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(OSError, match="disk full"):
        atomic_write_bytes(path, b"new")
    assert path.read_bytes() == b"old"
    assert [item.name for item in tmp_path.iterdir()] == ["a_BT.json"]


def test_writing_into_a_missing_folder_fails(tmp_path):
    with pytest.raises(FileNotFoundError):
        atomic_write_bytes(tmp_path / "missing" / "a.json", b"x")


def test_lone_surrogates_are_written_as_escapes(tmp_path):
    path = tmp_path / "a.json"
    atomic_write_json(path, {"text": "\udc80"})
    assert json.loads(path.read_text(encoding="ascii")) == {"text": "\udc80"}


# ============================================================================== reading
def write(path, data) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize("version", [1, 2])
def test_versions_1_and_2_are_read(tmp_path, version):
    write(tmp_path / "t_BT.json", {"format": "behavior_tree_widget", "version": version, "nodes": []})
    write(tmp_path / "t_BB.json", {"format": "behavior_tree_widget_blackboard", "version": version, "blackboard": []})
    assert read_tree_file(tmp_path / "t_BT.json")["version"] == version
    assert read_blackboard_file(tmp_path / "t_BB.json") == []


def test_a_newer_version_is_refused_with_the_widget_kind_of_message(tmp_path):
    write(tmp_path / "t_BT.json", {"format": "behavior_tree_widget", "version": 3})
    with pytest.raises(TreeFileError, match=r"was written by a newer version .*\(format 3; this version reads up to 2\)"):
        read_tree_file(tmp_path / "t_BT.json")
    write(tmp_path / "t_BB.json", {"format": "behavior_tree_widget_blackboard", "version": 3, "blackboard": []})
    with pytest.raises(TreeFileError, match="newer version"):
        read_blackboard_file(tmp_path / "t_BB.json")


@pytest.mark.parametrize(
    ("data", "message"),
    [({"format": "other", "version": 1}, "is not a behavior tree file"),
     ({"format": "behavior_tree_widget", "version": True}, "invalid format version"),
     ({"format": "behavior_tree_widget", "version": 0}, "invalid format version"),
     ({"format": "behavior_tree_widget", "version": 1, "nodes": {}}, "'nodes' must be a list"),
     ({"format": "behavior_tree_widget", "version": 1, "config": []}, "'config' must be a dict"),
     ([1, 2], "is not a behavior tree file")],
)
def test_malformed_tree_files_are_refused(tmp_path, data, message):
    write(tmp_path / "t_BT.json", data)
    with pytest.raises(TreeFileError, match=message):
        read_tree_file(tmp_path / "t_BT.json")


def test_unreadable_files(tmp_path):
    (tmp_path / "t_BT.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(TreeFileError, match="not a valid JSON file"):
        read_tree_file(tmp_path / "t_BT.json")
    with pytest.raises(TreeFileError, match="Could not read"):
        read_tree_file(tmp_path / "missing_BT.json")
    write(tmp_path / "b_BB.json", {"format": "behavior_tree_widget", "version": 1})
    with pytest.raises(TreeFileError, match="is not a blackboard file"):
        read_blackboard_file(tmp_path / "b_BB.json")
    write(tmp_path / "c_BB.json", {"format": "behavior_tree_widget_blackboard", "version": 1, "blackboard": {}})
    with pytest.raises(TreeFileError, match="'blackboard' must be a list"):
        read_blackboard_file(tmp_path / "c_BB.json")


def test_a_bom_is_accepted(tmp_path):
    (tmp_path / "t_BT.json").write_bytes(b'\xef\xbb\xbf{"format": "behavior_tree_widget", "version": 1}')
    assert read_tree_file(tmp_path / "t_BT.json")["version"] == 1


def test_load_tree_checks_both_files_and_takes_the_blackboard_file_first(tmp_path):
    write(tmp_path / "r_BT.json", {"format": "behavior_tree_widget", "version": 1, "blackboard": [{"name": "old"}]})
    write(tmp_path / "r_BB.json", {"format": "behavior_tree_widget_blackboard", "version": 1,
                                   "blackboard": [{"name": "a", "type": "Bool", "value": True}]})
    loaded = load_tree(tmp_path, "r.json")
    assert (loaded.key, loaded.path, loaded.has_blackboard_file) == ("r", "r_BT.json", True)
    assert loaded.blackboard == [{"name": "a", "type": "Bool", "value": True}]
    write(tmp_path / "r_BB.json", {"format": "nope"})
    with pytest.raises(TreeFileError):
        load_tree(tmp_path, "r")
    with pytest.raises(TreeFileError, match="does not exist"):
        load_tree(tmp_path, "missing")


def test_parse_blackboard_drops_invalid_entries_with_problems():
    entries, problems = parse_blackboard([
        {"name": "a", "type": "Integer", "value": 3, "reset": True},
        {"name": "b", "type": "double", "value": 1},
        {"name": "a", "type": "Bool", "value": True},
        {"name": "c.d", "type": "Bool", "value": True},
        {"name": "o", "type": "Object", "value": None},
        {"name": "s", "type": "Set", "value": {"__set__": [1, 2]}, "reset": "yes"},
        {"name": "t", "type": "Integer", "value": "x"},
        {"name": "src", "type": "Double", "value": 0.0, "source": {"topic": "/b", "type": "std_msgs/msg/Float64"}},
        {"name": "bad", "type": "Double", "value": 0.0, "source": {"topic": "", "type": "x"}},
        7,
    ])
    assert [(entry["name"], entry["type"], entry["value"], entry["reset"]) for entry in entries] == [
        ("a", "Integer", 3, True), ("b", "Double", 1.0, False), ("s", "Set", {1, 2}, False), ("src", "Double", 0.0, False),
    ]
    assert entries[3]["source"] == {"topic": "/b", "type": "std_msgs/msg/Float64", "field": "", "max_rate_hz": 2.0,
                                    "max_age_s": 0.0}
    assert len(problems) == 7
    assert parse_blackboard({}) == ([], ["the blackboard section is not a list"])


@pytest.mark.parametrize(
    "source",
    [None, {"type": "x"}, {"topic": "/t"}, {"topic": "/t", "type": "x", "field": 3},
     {"topic": "/t", "type": "x", "max_rate_hz": 0}, {"topic": "/t", "type": "x", "max_age_s": -1},
     {"topic": "/t", "type": "x", "max_rate_hz": True}, {"topic": "/t", "type": "x", "max_age_s": float("nan")}],
)
def test_malformed_sources(source):
    with pytest.raises(ValueError):
        parse_source(source)


def test_review_10_keys_take_the_case_of_the_files_on_disk(tmp_path):
    (tmp_path / "Cells").mkdir()
    write(tmp_path / "Cells" / "Robot_BT.json", {"format": "behavior_tree_widget", "version": 1})
    if not (tmp_path / "cells" / "ROBOT_BT.json").exists():
        assert load_tree(tmp_path, "Cells/Robot").key == "Cells/Robot"
        pytest.skip("this file system tells case apart: 'cells/robot' is another tree")
    assert load_tree(tmp_path, "cells/ROBOT").key == "Cells/Robot"
    assert canonical_key(tmp_path, "CELLS/robot") == "Cells/Robot"
    assert canonical_key(tmp_path, "missing") == "missing"
