"""Appendix B.6 (the engine and blackboard part) and the blackboard's own rules (design 7)."""

from __future__ import annotations

import threading

import pytest
from engine_helpers import EngineRun, Script

from bt_server_core.blackboard import Blackboard, OpError
from bt_server_core.engine import Outcome, State


def set_node(name: str, value: object) -> dict:
    return {"type": "Set", "fields": {"ValueName": name, "CompareTo": None, "LiteralValue": value}}


def eval_node(name: str, value: object) -> dict:
    return {"type": "Evaluation", "fields": {"ValueName": name, "CompareTo": None, "LiteralValue": value}}


def entry_value(entries: list[dict], name: str) -> object:
    return next(item["value"] for item in entries if item["name"] == name)


# ------------------------------------------------------------------------- B.6


def test_K_01_resets_at_the_end_of_the_loop() -> None:
    run = EngineRun("Seq[S5, A]", scripts={"A": Script("S", 1)}, leaves={"S5": set_node("n", 5)},
                    entries=[{"name": "n", "type": "Integer", "value": 0, "reset": True}]).start()
    run.at(0.5)
    assert run.blackboard.get("n") == 5
    run.at(1)
    assert run.result.outcome == Outcome.SUCCEEDED
    assert run.blackboard.get("n") == 0
    assert entry_value(run.result.blackboard, "n") == 0
    assert "id" in run.result.blackboard[0]


@pytest.mark.parametrize(("reset", "second_loop"), [(True, "S S S"), (False, "F F -")])
def test_K_02_resets_with_loop_execution(reset: bool, second_loop: str) -> None:
    run = EngineRun("Seq[E0, S5]", leaves={"E0": eval_node("n", 0), "S5": set_node("n", 5)}, config={"repeat": True},
                    entries=[{"name": "n", "type": "Integer", "value": 0, "reset": reset}]).start()
    assert run.codes() == "S S S"
    for loop in (1, 2):
        run.at(loop)
        assert run.codes() == ("S S S" if reset else second_loop)
    assert run.runner.loop_count == 3


@pytest.mark.parametrize(("restore", "expected"), [(True, 1), (False, 9)])
def test_K_03_restore_on_stop(restore: bool, expected: int) -> None:
    run = EngineRun("Seq[S9, A]", leaves={"S9": set_node("m", 9)}, config={"restore_blackboard": restore},
                    entries=[{"name": "m", "type": "Integer", "value": 1}]).start()
    assert run.blackboard.get("m") == 9
    run.at(0.5)
    run.runner.stop()
    run.at(0.7)
    assert run.state == State.IDLE
    assert run.blackboard.get("m") == expected
    assert entry_value(run.result.blackboard, "m") == expected


def test_K_04_restore_renames_back_and_keeps_the_id() -> None:
    run = EngineRun("Seq[S9, A, Em]", leaves={"S9": set_node("m", 9), "Em": eval_node("m", 1)},
                    config={"restore_blackboard": True},
                    entries=[{"name": "m", "type": "Integer", "value": 1}]).start()
    entry_id = run.blackboard.id_of("m")
    evaluation = run.runner.node(run.index("Em"))
    run.at(0.3)
    run.runner.apply([{"op": "bb.rename", "id": entry_id, "name": "k"}])
    assert run.blackboard.names() == ["go", "battery", "k"] or "k" in run.blackboard.names()
    assert evaluation.fields["ValueName"] == "k"  # running nodes follow the rename, as in the widget
    run.at(0.5)
    run.runner.stop()
    run.at(0.7)
    assert run.blackboard.id_of("m") == entry_id
    assert not run.blackboard.has("k")
    assert run.blackboard.get("m") == 1
    assert evaluation.fields["ValueName"] == "m"
    renamed = [entry for change in run.sink.blackboard_changes for entry in change["entries"] if entry["id"] == entry_id]
    assert [entry["name"] for entry in renamed][-2:] == ["k", "m"]


def test_K_07_sourced_entry_cannot_be_written() -> None:
    source = {"topic": "/door", "type": "std_msgs/msg/Bool"}
    run = EngineRun("Seq[Sd]", leaves={"Sd": {"type": "Set", "fields": {
        "ValueName": "door", "CompareTo": None, "LiteralValue": True}}},
        entries=[{"name": "door", "type": "Bool", "value": False, "source": source}]).start()
    assert run.codes() == "F F"
    assert "/door" in run.details("Sd")["error"]
    assert run.blackboard.get("door") is False
    with pytest.raises(OpError, match="/door"):
        run.blackboard.apply_op({"op": "bb.set_value", "id": run.blackboard.id_of("door"), "value": True})
    assert run.blackboard.get("door") is False
    run.blackboard.feed("door", True)
    assert run.blackboard.get("door") is True


def test_K_08_value_of_the_wrong_type_is_refused() -> None:
    board = Blackboard([{"name": "count", "type": "Integer", "value": 3}])
    with pytest.raises(OpError, match="Integer"):
        board.apply_op({"op": "bb.set_value", "id": board.id_of("count"), "value": "abc"})
    assert board.get("count") == 3


def test_K_08_during_a_run_apply_raises() -> None:
    run = EngineRun("Seq[A]", entries=[{"name": "count", "type": "Integer", "value": 3}]).start()
    with pytest.raises(OpError):
        run.runner.apply([{"op": "bb.set_value", "id": run.blackboard.id_of("count"), "value": "abc"}])
    assert run.blackboard.get("count") == 3


# ------------------------------------------------------------------------- Resets (the widget's rules)


def board_with(*entries: dict) -> Blackboard:
    return Blackboard(list(entries))


def test_resets_follow_a_rename_and_restore_the_type() -> None:
    board = board_with({"name": "a", "type": "Integer", "value": 1, "reset": True})
    board.start_loop()
    board.set("a", 5)
    board.rename("a", "b")
    board.retype("b", "String")
    assert board.get("b") == "5"
    assert board.end_loop() == ["b"]
    assert board.type_of("b") == "Integer" and board.get("b") == 1


def test_resets_leave_created_and_removed_entries_alone() -> None:
    board = board_with({"name": "a", "type": "Integer", "value": 1, "reset": True})
    board.start_loop()
    board.add("new", "Integer", 3, reset=True)
    board.set("new", 4)
    board.set("a", 2)
    board.remove("a")
    board.end_loop()
    assert board.names() == ["new"] and board.get("new") == 4


def test_reset_flag_checked_during_the_loop_counts() -> None:
    board = board_with({"name": "a", "type": "Integer", "value": 1})
    board.start_loop()
    board.set("a", 2)
    board.set_reset("a", True)
    board.end_loop()
    assert board.get("a") == 1


def test_sourced_entries_are_not_reset() -> None:
    board = board_with({"name": "a", "type": "Integer", "value": 1, "reset": True,
                        "source": {"topic": "/a", "type": "std_msgs/msg/Int32"}})
    board.start_loop()
    board.feed("a", 7)
    board.end_loop()
    assert board.get("a") == 7


# ------------------------------------------------------------------------- restore


def test_restore_swapped_names_keeps_ids() -> None:
    board = board_with({"name": "a", "type": "Integer", "value": 1}, {"name": "b", "type": "Integer", "value": 2})
    ids = board.ids()
    snapshot = board.snapshot()
    board.rename("a", "t")
    board.rename("b", "a")
    board.rename("t", "b")
    board.add("c", "String", "x")
    board.remove("a")
    board.restore(snapshot)
    assert board.names() == ["a", "b"] and board.ids() == ids
    assert (board.get("a"), board.get("b")) == (1, 2)


def test_restore_leaves_a_sourced_value_alone() -> None:
    board = board_with({"name": "a", "type": "Integer", "value": 1,
                        "source": {"topic": "/a", "type": "std_msgs/msg/Int32"}})
    snapshot = board.snapshot()
    board.feed("a", 9)
    board.restore(snapshot)
    assert board.get("a") == 9


# ------------------------------------------------------------------------- operations


def test_operations() -> None:
    board = Blackboard()
    board.apply_op({"op": "bb.add", "entry": {"id": "1" * 32, "name": "x", "type": "Double", "value": 1.0,
                                              "reset": False}})
    board.apply_op({"op": "bb.add", "entry": {"name": "y", "type": "List", "value": [1]}})
    board.apply_op({"op": "bb.set_value", "id": "1" * 32, "value": 2})
    assert board.get("x") == 2.0
    board.apply_op({"op": "bb.retype", "id": "1" * 32, "type": "Integer"})
    assert board.get("x") == 2 and board.type_of("x") == "Integer"
    board.apply_op({"op": "bb.set_reset", "id": "1" * 32, "reset": True})
    assert board.is_reset("x")
    board.apply_op({"op": "bb.move", "order": [board.id_of("y"), "1" * 32]})
    assert board.names() == ["y", "x"]
    board.apply_op({"op": "bb.set_source", "id": "1" * 32, "source": {"topic": "/x", "type": "std_msgs/msg/Int32"}})
    assert board.source_of("x")["max_rate_hz"] == 2.0 and board.freshness("x") == "nodata"
    board.apply_op({"op": "bb.rename", "id": "1" * 32, "name": "z"})
    board.apply_op({"op": "bb.remove", "ids": ["1" * 32]})
    assert board.names() == ["y"]


@pytest.mark.parametrize(("op", "reason"), [
    ({"op": "bb.add", "entry": {"name": "", "type": "Integer"}}, "empty"),
    ({"op": "bb.add", "entry": {"name": "a.b", "type": "Integer"}}, "'.'"),
    ({"op": "bb.add", "entry": {"name": " a", "type": "Integer"}}, "spaces"),
    ({"op": "bb.add", "entry": {"name": "taken", "type": "Integer"}}, "already exists"),
    ({"op": "bb.add", "entry": {"name": "o", "type": "Object"}}, "Object"),
    ({"op": "bb.add", "entry": {"name": "q", "type": "Quaternion"}}, "unknown"),
    ({"op": "bb.rename", "id": "nope", "name": "x"}, "no blackboard entry"),
    ({"op": "bb.retype", "id": "TAKEN", "type": "Object"}, "Object"),
    ({"op": "bb.move", "order": ["nope"]}, "no blackboard entry"),
    ({"op": "bb.set_source", "id": "TAKEN", "source": {"type": "x"}}, "topic"),
    ({"op": "bb.nothing"}, "unknown"),
])
def test_refused_operations(op: dict, reason: str) -> None:
    board = Blackboard([{"id": "TAKEN", "name": "taken", "type": "Integer", "value": 1}])
    with pytest.raises(OpError, match=reason):
        board.apply_op(op)
    assert board.names() == ["taken"] and board.get("taken") == 1


def test_object_entries_are_read_only_for_operations() -> None:
    board = Blackboard()
    entry_id = board.put_object("device", object())
    with pytest.raises(OpError, match="read-only"):
        board.apply_op({"op": "bb.set_value", "id": entry_id, "value": 1})
    assert board.to_list() == []
    assert board.entry("device")["type"] == "Object"


# ------------------------------------------------------------------------- freshness and changes


def test_freshness_and_age() -> None:
    now = [0.0]
    board = Blackboard([{"name": "a", "type": "Double", "value": 0.0,
                         "source": {"topic": "/a", "type": "t", "max_age_s": 2.0}}], clock=lambda: now[0])
    assert board.freshness("a") == "nodata" and board.age("a") is None
    assert board.feed("a", 1.0)
    assert board.freshness("a") == "ok" and board.next_stale_time() == 2.0
    now[0] = 1.0
    assert not board.feed("a", 1.0)  # a repeated value only renews the age
    now[0] = 3.0
    assert board.freshness("a") == "stale" and board.age("a") == 2.0
    assert board.feed("a", 1.0)  # stale -> ok is a change the tree sees


def test_drain_changes_shape() -> None:
    board = Blackboard([{"name": "a", "type": "Integer", "value": 1}])
    assert board.drain_changes() is None
    board.set("a", 2)
    board.add("b", "Set", {1, 2})
    changes = board.drain_changes()
    assert [entry["name"] for entry in changes["entries"]] == ["a", "b"]
    assert changes["entries"][1]["value"] == {"__set__": [1, 2]}
    assert changes["order"] == board.ids() and changes["removed"] == []
    board.remove("a")
    changes = board.drain_changes()
    assert changes["entries"] == [] and len(changes["removed"]) == 1


def test_listeners_from_worker_threads() -> None:
    board = Blackboard()
    seen = []
    board.add_listener(lambda: seen.append(threading.get_ident()))
    worker = threading.Thread(target=lambda: board.set("x", 1))
    worker.start()
    worker.join()
    assert seen == [worker.ident]
    assert board.type_of("x") == "Integer"


def test_load_reports_invalid_entries() -> None:
    board = Blackboard({"format": "behavior_tree_widget_blackboard", "blackboard": [
        {"name": "ok", "type": "Integer", "value": 1},
        {"name": "ok", "type": "Integer", "value": 2},
        {"name": "bad", "type": "Integer", "value": "x"},
        {"name": "flag", "type": "Bool", "value": True, "reset": "yes"},
        "junk",
    ]})
    assert board.names() == ["ok"]
    assert len(board.problems) == 4
