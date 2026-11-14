"""Appendix B.3: started leaves and cancelling (design 5.4, 5.6). Timelines are checked at every
listed time."""

from __future__ import annotations

from engine_helpers import EngineRun, Script

from bt_server_core.engine import Outcome, State


def timeline(run: EngineRun, steps: list[tuple[float, str]]) -> None:
    for when, statuses in steps:
        run.at(when)
        assert run.codes() == statuses, f"at {when}"


def test_E_20_started_leaf_then_instant() -> None:
    run = EngineRun("Seq[A, s]", scripts={"A": Script("S", 1.5)}).start()
    timeline(run, [(0, "R R -"), (1.5, "S S S")])
    assert run.result.outcome == Outcome.SUCCEEDED
    lines = run.sink.lines("Node ")
    assert (0, "Node 'A' (TestA) started: Running") in lines
    finished = lines.index((1.5, "Node 'A' (TestA) finished: Succeeded"))
    assert lines[finished + 1] == (1.5, "Node 's' (TestS) executed: Succeeded")


def test_E_21_memory_resumes_at_running_child() -> None:
    run = EngineRun("Seq[n, A, s]", scripts={"A": Script("S", 2.5)}).start()
    timeline(run, [(0, "R S R -"), (2.5, "S S S S")])
    assert run.calls["n"] == [0]


def test_E_22_memory_off_reruns_first_child() -> None:
    run = EngineRun("Seq~[n, A, s]", scripts={"A": Script("S", 2.5)}).start()
    timeline(run, [(0, "R S R -"), (2.5, "S S S S")])
    assert run.calls["n"] == [0, 1, 2, 2.5]


def test_E_23_end_of_run_waits_for_the_cancel() -> None:
    run = EngineRun("Seq~[c, A]", entries=[{"name": "go", "type": "Bool", "value": True}]).start()
    assert run.codes() == "R S R"
    run.at(2.3)
    run.set("go", False)
    assert run.codes() == "F F C"
    assert run.state == State.RUNNING
    run.at(2.49)
    assert run.result is None
    run.at(2.5)
    assert run.codes() == "F F -"
    assert run.result.outcome == Outcome.FAILED
    assert run.state == State.IDLE
    assert run.events_of("A") == [(0, "goal"), (2.3, "cancel")]
    assert (2.3, "Node 'A' (TestA) interrupted") in run.sink.lines()


def _e24(scripts: dict, options: dict | None = None) -> EngineRun:
    run = EngineRun("Sel~[Seq[c, A], B]", scripts=scripts, options=options).start()
    assert run.codes() == "R F F - R"
    run.at(1.4)
    run.set("go", True)
    assert run.codes() == "R R S R C"
    return run


def test_E_24_start_waits_for_the_cancel_before_it() -> None:
    run = _e24({"A": Script("S", 1), "B": Script(cancel_after=0.5)})
    assert "B" in run.details("A")["waiting"]
    timeline(run, [(1.9, "R R S R -"), (2.9, "S S S S -")])
    assert run.events_of("A") == [(1.9, "goal")]
    assert "waiting" not in run.details("A")


def test_E_25_wait_switched_off_for_the_node() -> None:
    run = _e24({"A": Script("S", 1), "B": Script(cancel_after=0.5)}, options={"A": {"wait_for_cancels": False}})
    timeline(run, [(1.9, "R R S R -"), (2.4, "S S S S -")])
    assert run.events_of("A") == [(1.4, "goal")]


def test_E_25_wait_switched_off_for_the_tree() -> None:
    run = EngineRun("Sel~[Seq[c, A], B]", scripts={"A": Script("S", 1), "B": Script(cancel_after=0.5)},
                    config={"wait_for_cancels": False}).start()
    run.at(1.4)
    run.set("go", True)
    run.at(2.4)
    assert run.codes() == "S S S S -"
    assert run.events_of("A") == [(1.4, "goal")]


def test_E_26_parallel_branches_do_not_wait_for_each_other() -> None:
    scripts = {"A": Script("S", 1), "B": Script(cancel_after=3), "D": Script("S", 1.5), "H": Script("S", 1)}
    run = EngineRun("Seq[All[Sel~[Seq[c, A], B], Seq[D, H]]]", scripts=scripts).start()
    assert run.codes() == "R R R F F - R R R -"
    run.at(1.2)
    run.set("go", True)
    assert run.codes() == "R R R R S R C R R -"
    timeline(run, [
        (1.5, "R R R R S R C R S R"),
        (2.5, "R R R R S R C S S S"),
        (4.2, "R R R R S R - S S S"),
        (5.2, "S S S S S S - S S S"),
    ])
    assert run.events_of("H") == [(1.5, "goal")]
    assert run.events_of("A") == [(4.2, "goal")]


def test_E_27_what_follows_a_parallel_waits_for_its_halted_children() -> None:
    run = EngineRun("Seq[Any[A, B], D]", scripts={"A": Script("S", 1), "B": Script(cancel_after=0.5),
                                                  "D": Script("S", 1)}).start()
    timeline(run, [(0, "R R R R -"), (1, "R S S C R"), (1.5, "R S S - R"), (2.5, "S S S - S")])
    assert run.events_of("D") == [(1.5, "goal")]


def test_E_28_first_child_to_finish_decides_any() -> None:
    run = EngineRun("Seq[Any[A, B]]", scripts={"A": Script("F", 1), "B": Script("S", 2.5)}).start()
    timeline(run, [(0, "R R R R"), (1, "F F F C")])
    assert run.result is None
    timeline(run, [(1.2, "F F F -")])
    assert run.result.outcome == Outcome.FAILED
    assert run.events_of("B") == [(0, "goal"), (1, "cancel")]


def test_E_29_all_fails_when_a_child_fails() -> None:
    run = EngineRun("Seq[All[A, B]]", scripts={"A": Script("F", 1)}).start()
    timeline(run, [(0, "R R R R"), (1, "F F F C")])
    assert run.result is None
    timeline(run, [(1.2, "F F F -")])
    assert run.result.outcome == Outcome.FAILED


def test_E_30_timeout_cancels_then_fails() -> None:
    run = EngineRun("Seq[A]", scripts={"A": Script(timeout=2)}).start()
    timeline(run, [(0, "R R"), (2, "R C")])
    assert run.details("A")["error"] == "timed out"
    assert run.result is None
    timeline(run, [(2.2, "F F")])
    assert run.result.outcome == Outcome.FAILED
    assert (2.2, "Node 'A' (TestA) finished: Failed (timed out)") in run.sink.lines()


def test_E_31_timeout_counts_from_the_real_start() -> None:
    run = _e24({"A": Script(timeout=2), "B": Script(cancel_after=0.5)})
    run.at(3.89)
    assert run.codes()[-3] == "R"
    run.at(3.9)
    assert run.codes() == "R R S C -"
    run.at(4.1)
    assert run.codes().split()[3] == "F"
    assert run.events_of("A") == [(1.9, "goal"), (3.9, "cancel")]


def _e32(config: dict | None = None) -> EngineRun:
    run = EngineRun("Seq~[c, A]", scripts={"A": Script(cancel_after=None)},
                    entries=[{"name": "go", "type": "Bool", "value": True}], config=config).start()
    assert run.codes() == "R S R"
    run.at(1.5)
    run.set("go", False)
    assert run.codes() == "F F C"
    run.at(6.49)
    assert run.result is None
    return run


def test_E_32_unconfirmed_cancel_stops_the_tree() -> None:
    run = _e32()
    run.at(6.5)
    assert run.codes() == "- - -"
    assert run.state == State.IDLE
    result = run.result
    assert result.outcome == Outcome.STOPPED
    assert not result.stopped_by_command  # goal aborted, not canceled
    assert result.unconfirmed_cancels == 1
    assert run.runner.unsettled == 1
    assert any(level == 1 and "not confirmed" in text for when, level, text, _ in run.sink.logs if when == 6.5)


def test_E_33_unconfirmed_cancel_continue() -> None:
    run = _e32({"on_unconfirmed_cancel": "continue"})
    run.at(6.5)
    assert run.codes() == "F F -"
    assert run.result.outcome == Outcome.FAILED
    assert run.result.unconfirmed_cancels == 1
    assert run.runner.unsettled == 1


def test_E_34_a_node_never_runs_twice_at_once() -> None:
    run = EngineRun("Sel~[Seq~[c, A], B]", scripts={"A": Script(cancel_after=3)},
                    entries=[{"name": "go", "type": "Bool", "value": True}]).start()
    assert run.codes() == "R R S R -"
    run.at(1.2)
    run.set("go", False)
    assert run.codes() == "R F F C R"
    run.at(2.4)
    run.set("go", True)
    assert run.codes() == "R R S C -"
    run.at(4.19)
    assert run.codes() == "R R S C -"
    run.at(4.2)
    assert run.codes() == "R R S R -"
    assert run.events_of("A") == [(0, "goal"), (1.2, "cancel"), (4.2, "goal")]
    assert run.events_of("B") == []

