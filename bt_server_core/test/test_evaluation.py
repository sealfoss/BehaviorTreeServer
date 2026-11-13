"""Appendix B.5: the Evaluation node (design 6.3): comparisons with Wait off, then waiting."""

from __future__ import annotations

import math
from typing import Any

import pytest
from engine_helpers import EngineRun, Script

from bt_server_core.engine import Outcome, State
from bt_server_core.values import encode_value


def evaluate(entry: tuple[str, Any] | None, operator: str, literal: Any = None, *, compare_to: str | None = None,
             others: list[dict] | None = None, value_name: str = "e") -> EngineRun:
    entries = list(others or [])
    if entry is not None:
        entries.insert(0, {"name": "e", "type": entry[0], "value": encode_value(entry[1])})
    fields = {"ValueName": value_name, "CompareTo": compare_to, "LiteralValue": encode_value(literal),
              "Operator": operator}
    return EngineRun("Seq[E]", entries=entries, leaves={"E": {"type": "Evaluation", "fields": fields}}).start()


def result_of(run: EngineRun) -> str:
    assert run.state == State.IDLE
    return run.codes().split()[1]


@pytest.mark.parametrize(
    ("case", "entry", "operator", "literal", "expected"),
    [
        ("V-01", ("Integer", 3), "==", 3, "S"),
        ("V-02", ("Integer", 3), "!=", 3, "F"),
        ("V-03", ("Double", 17.3), ">=", 20.0, "F"),
        ("V-04", ("Double", 20.0), ">=", 20.0, "S"),
        ("V-05", ("Integer", 5), "<", 7, "S"),
        ("V-07", ("Bool", True), "==", True, "S"),
        ("V-08", ("List", [1, 2]), "==", [1.0, 2.0], "S"),
        ("V-09", ("List", [1, 0]), "==", [True, False], "F"),
        ("V-10", ("Double", math.nan), "==", math.nan, "F"),
        ("V-11", ("Double", math.nan), "!=", math.nan, "S"),
    ],
)
def test_V_01_to_V_11_comparisons(case: str, entry: tuple[str, Any], operator: str, literal: Any,
                                  expected: str) -> None:
    assert result_of(evaluate(entry, operator, literal)) == expected, case


def test_V_06_strings_cannot_be_ordered() -> None:
    run = evaluate(("String", "a"), "<", "b")
    assert result_of(run) == "F"
    assert "cannot be ordered" in run.details("E")["error"]


def test_V_12_compare_with_another_entry() -> None:
    run = evaluate(("Integer", 4), "==", compare_to="b", others=[{"name": "b", "type": "Integer", "value": 4}])
    assert result_of(run) == "S"


def test_V_13_compare_entry_missing() -> None:
    run = evaluate(("Integer", 4), "==", compare_to="b")
    assert result_of(run) == "F"
    assert "'b'" in run.details("E")["error"]


def test_V_14_value_entry_missing() -> None:
    run = evaluate(None, "==", 1)
    assert result_of(run) == "F"
    assert "'e'" in run.details("E")["error"]


def test_V_15_no_entry_chosen() -> None:
    run = evaluate(("Integer", 1), "==", 1, value_name="")
    assert result_of(run) == "F"
    assert "Value Name" in run.details("E")["error"]


@pytest.mark.parametrize("operator", ["==", "!=", "<", "<=", ">", ">="])
@pytest.mark.parametrize(("type_name", "value"), [("Integer", 3), ("Double", 2.5), ("String", "a"), ("Bool", True),
                                                  ("List", [1]), ("Dictionary", {"a": 1}), ("Set", {1})])
def test_every_operator_against_every_type(operator: str, type_name: str, value: Any) -> None:
    run = evaluate((type_name, value), operator, value)
    orderable = type_name in ("Integer", "Double")
    if operator in ("==", "<=", ">="):
        expected = "S" if operator == "==" or orderable else "F"
    elif operator in ("!=", "<", ">"):
        expected = "F"
    assert result_of(run) == expected
    if not orderable and operator not in ("==", "!="):
        assert "cannot be ordered" in run.details("E")["error"]


def test_integer_against_double_entry() -> None:
    run = evaluate(("Integer", 2), "<", compare_to="d", others=[{"name": "d", "type": "Double", "value": 2.5}])
    assert result_of(run) == "S"


def test_object_entry_with_a_literal_fails_even_when_waiting() -> None:
    entries: list[dict] = []
    run = EngineRun("Seq[E]", entries=entries, leaves={"E": {"type": "Evaluation", "fields": {
        "ValueName": "obj", "CompareTo": None, "LiteralValue": 1, "Wait": True}}})
    run.blackboard.put_object("obj", object())
    run.start()
    assert run.codes() == "F F"
    assert "Literal Value" in run.details("E")["error"]


# ------------------------------------------------------------------------- waiting


def test_V_20_wait_until_the_entry_changes() -> None:
    run = EngineRun("Seq[W, s]").start()
    assert run.codes() == "R R -"
    assert run.details("W")["waiting"] == "waiting for battery >= 20.0 (now 17.3)"
    run.at(3.3)
    run.set("battery", 21.0)
    assert run.codes() == "S S S"
    assert run.result.outcome == Outcome.SUCCEEDED
    assert "waiting" not in run.details("W")


def test_V_21_wait_times_out() -> None:
    run = EngineRun("Seq[W, s]", options={"W": {"fields": {"WaitTimeout": 5.0}}}).start()
    assert run.codes() == "R R -"
    run.at(4.99)
    assert run.codes() == "R R -"
    run.at(5)
    assert run.codes() == "F F -"
    assert run.details("W")["error"] == "timed out"


def test_V_21_timeout_follows_the_ros_clock() -> None:
    run = EngineRun("Seq[W, s]", options={"W": {"fields": {"WaitTimeout": 5.0}}})
    run.scheduler.ros_offset = 1000.0
    run.start()
    run.at(5)
    assert run.codes() == "F F -"


def test_V_22_entry_created_while_waiting() -> None:
    run = EngineRun("Seq[W, s]", entries=[]).start()
    assert run.codes() == "R R -"
    run.at(2.2)
    run.blackboard.add("battery", "Double", 25.0)
    run.scheduler.run_pending()
    assert run.codes() == "S S S"


def test_V_23_halted_waiting_evaluation_never_cancels() -> None:
    run = EngineRun("Seq~[c, W]", entries=[{"name": "go", "type": "Bool", "value": True},
                                           {"name": "battery", "type": "Double", "value": 17.3}]).start()
    assert run.codes() == "R S R"
    run.at(1.5)
    run.set("go", False)
    assert run.codes() == "F F -"
    assert run.result.outcome == Outcome.FAILED
    assert all(codes.split()[2] != "C" for _, codes in run.sink.snapshots)


def test_V_24_waiting_evaluation_ends_an_action() -> None:
    run = EngineRun("Seq[Any[A, W]]").start()
    assert run.codes() == "R R R R"
    run.at(2.4)
    run.set("battery", 21.0)
    assert run.codes() == "S S C S"
    assert run.result is None
    run.at(2.6)
    assert run.codes() == "S S - S"
    assert run.result.outcome == Outcome.SUCCEEDED


def test_V_25_change_inside_a_tick_waits_for_the_periodic_tick() -> None:
    set_battery = {"type": "Set", "title": "Set",
                   "fields": {"ValueName": "battery", "CompareTo": None, "LiteralValue": 21.0}}
    run = EngineRun("Seq[All[W, Seq[A, S1]]]", scripts={"A": Script("S", 0.5)}, leaves={"S1": set_battery}).start()
    assert run.codes() == "R R R R R -"
    run.at(0.5)
    assert run.codes() == "R R R S S S"
    run.at(0.99)
    assert run.codes() == "R R R S S S"
    run.at(1)
    assert run.codes() == "S S S S S S"


def test_V_26_change_seen_later_in_the_same_tick() -> None:
    set_battery = {"type": "Set", "title": "Set",
                   "fields": {"ValueName": "battery", "CompareTo": None, "LiteralValue": 21.0}}
    run = EngineRun("Seq[All[Seq[A, S1], W]]", scripts={"A": Script("S", 0.5)}, leaves={"S1": set_battery}).start()
    assert run.codes() == "R R R R - R"
    run.at(0.5)
    assert run.codes() == "S S S S S S"


def test_wait_on_stale_source_keeps_waiting_and_fails_without_wait() -> None:
    source = {"topic": "/battery", "type": "std_msgs/msg/Float64", "max_age_s": 2.0}
    entries = [{"name": "battery", "type": "Double", "value": 30.0, "source": source}]
    run = EngineRun("Seq[W]", entries=entries).start()
    assert run.codes() == "R R"
    assert "no recent data" in run.details("W")["waiting"]
    run.at(0.5)
    run.blackboard.feed("battery", 25.0)
    run.scheduler.run_pending()
    assert run.codes() == "S S"
    plain = EngineRun("Seq[E]", entries=entries, leaves={"E": {"type": "Evaluation", "fields": {
        "ValueName": "battery", "CompareTo": None, "LiteralValue": 20.0, "Operator": ">="}}}).start()
    assert plain.codes() == "F F"
    assert plain.details("E")["error"] == "no recent data"


def test_entry_going_stale_requests_a_tick() -> None:
    source = {"topic": "/battery", "type": "std_msgs/msg/Float64", "max_age_s": 0.5}
    entries = [{"name": "battery", "type": "Double", "value": 30.0, "source": source}]
    run = EngineRun("Seq~[n, W]", entries=entries, config={"tick_interval_ms": 10000})
    run.blackboard.feed("battery", 10.0)
    run.start()
    assert run.calls["n"] == [0]
    run.at(0.5)
    assert run.calls["n"] == [0, 0.5]
    assert "no recent data" in run.details("W")["waiting"]


def test_evaluation_timeout_acts_on_time_with_a_ros_clock_offset() -> None:
    late = []
    for offset in (0.1, 1e9 + 0.7, 1.7e9 + 0.123):
        for timeout in (0.1, 0.3, 0.7, 1.1, 2.3, 5.0):
            run = EngineRun("Seq[W, s]", options={"W": {"fields": {"WaitTimeout": timeout}}})
            run.scheduler.ros_offset = offset
            run.scheduler.advance_to(0.25)
            run.start()
            run.at(0.25 + timeout + 1e-12)
            if run.codes() != "F F -":
                late.append((offset, timeout, run.codes()))
    assert late == []
