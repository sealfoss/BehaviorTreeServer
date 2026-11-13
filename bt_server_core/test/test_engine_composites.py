"""Appendix B.2: composites with instant leaves (design 5.2, 5.3). One tick at 0 decides each."""

from __future__ import annotations

import pytest
from engine_helpers import EngineRun

from bt_server_core.engine import Outcome, State


def run_once(tree: str) -> EngineRun:
    run = EngineRun(tree).start()
    assert run.state == State.IDLE
    return run


@pytest.mark.parametrize(
    ("case", "tree", "statuses", "outcome"),
    [
        ("E-01", "Seq[s, s, s]", "S S S S", Outcome.SUCCEEDED),
        ("E-02", "Seq[s, f, s]", "F S F -", Outcome.FAILED),
        ("E-03", "Sel[f, s, s]", "S F S -", Outcome.SUCCEEDED),
        ("E-04", "Sel[f, f]", "F F F", Outcome.FAILED),
        ("E-05", "Seq[]", "S", Outcome.SUCCEEDED),
        ("E-06", "Sel[]", "F", Outcome.FAILED),
        ("E-07", "Seq[Neg[f], s]", "S S F S", Outcome.SUCCEEDED),
        ("E-08", "Seq[Neg[s], s]", "F F S -", Outcome.FAILED),
        ("E-10", "Seq[All[s, s], s]", "S S S S S", Outcome.SUCCEEDED),
        ("E-11", "Seq[All[s, f, s], s]", "F F S F S -", Outcome.FAILED),
        ("E-12", "Seq[Any[f, s, s], s]", "F F F S S -", Outcome.FAILED),
        ("E-13", "Seq[Any[f, f], s]", "F F F F -", Outcome.FAILED),
        ("E-14", "Seq[All[], s]", "S S S", Outcome.SUCCEEDED),
        ("E-15", "Seq[Any[], s]", "S S S", Outcome.SUCCEEDED),
    ],
)
def test_E_01_to_E_15_composites(case: str, tree: str, statuses: str, outcome: Outcome) -> None:
    run = run_once(tree)
    assert run.codes() == statuses, case
    assert run.result is not None and run.result.outcome == outcome, case


def test_E_01_seq_all_succeed() -> None:
    assert run_once("Seq[s, s, s]").codes() == "S S S S"


def test_E_02_seq_stops_at_failure() -> None:
    assert run_once("Seq[s, f, s]").codes() == "F S F -"


def test_E_03_sel_stops_at_success() -> None:
    assert run_once("Sel[f, s, s]").codes() == "S F S -"


def test_E_04_sel_all_fail() -> None:
    assert run_once("Sel[f, f]").codes() == "F F F"


def test_E_05_empty_sequence_succeeds() -> None:
    assert run_once("Seq[]").codes() == "S"


def test_E_06_empty_selector_fails() -> None:
    assert run_once("Sel[]").codes() == "F"


def test_E_07_negation_of_failure() -> None:
    assert run_once("Seq[Neg[f], s]").codes() == "S S F S"


def test_E_08_negation_of_success() -> None:
    assert run_once("Seq[Neg[s], s]").codes() == "F F S -"


def test_E_09_negation_without_child_fails_with_reason() -> None:
    run = run_once("Seq[Neg[], s]")
    assert run.codes() == "F F -"
    assert run.result.outcome == Outcome.FAILED
    assert "needs a child" in run.details("Neg")["error"]


def test_E_10_parallel_all_succeeds() -> None:
    assert run_once("Seq[All[s, s], s]").codes() == "S S S S S"


def test_E_11_parallel_all_ticks_every_child() -> None:
    assert run_once("Seq[All[s, f, s], s]").codes() == "F F S F S -"


def test_E_12_failure_wins_a_tie() -> None:
    assert run_once("Seq[Any[f, s, s], s]").codes() == "F F F S S -"


def test_E_13_parallel_any_all_fail() -> None:
    assert run_once("Seq[Any[f, f], s]").codes() == "F F F F -"


def test_E_14_empty_parallel_all_succeeds() -> None:
    assert run_once("Seq[All[], s]").codes() == "S S S"


def test_E_15_empty_parallel_any_succeeds() -> None:
    assert run_once("Seq[Any[], s]").codes() == "S S S"


def test_E_16_exception_fails_the_node_only() -> None:
    run = run_once("Seq[x, s]")
    assert run.codes() == "F F -"
    assert run.result.outcome == Outcome.FAILED
    assert "boom" in run.details("x")["error"]
    errors = [text for _, level, text, _ in run.sink.logs if level == 1]
    assert any("boom" in text for text in errors)
    assert any("'x' (TestX) executed: Failed" in text and "boom" in text for _, text in run.sink.lines())


def test_memory_off_restarts_at_first_child_each_tick() -> None:
    run = EngineRun("Seq~[n, W]").start()
    run.at(3)
    assert run.calls["n"] == [0, 1, 2, 3]


def test_unknown_type_fails_when_executed() -> None:
    run = EngineRun("Seq[q]", leaves={"q": {"type": "NoSuchType"}}).start()
    assert run.codes() == "F F"
    assert "unknown node type" in run.details("q")["error"]


def test_ros_type_without_ros_fails_with_reason() -> None:
    run = EngineRun("Seq[q]", leaves={"q": {"type": "CallAction"}}).start()
    assert run.codes() == "F F"
    assert "needs ROS" in run.details("q")["error"]
