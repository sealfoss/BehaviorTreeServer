"""Appendix B.4: run states and commands (design 4.4, 5.5), and the command table of 4.4."""

from __future__ import annotations

import pytest
from engine_helpers import EngineRun, Script, make_registry

from bt_server_core.blackboard import Blackboard
from bt_server_core.compat import LeafNodeWidget
from bt_server_core.engine import BundleError, CommandRefused, Outcome, State, Status, TreeRunner
from bt_server_core.leaves import AsyncLeaf
from bt_server_core.registry import Registry
from bt_server_core.scheduler import FakeScheduler


def check(run: EngineRun, when: float, state: State, statuses: str) -> None:
    run.at(when)
    assert (run.state, run.codes()) == (state, statuses), f"at {when}"


def test_R_01_pause_keeps_a_result_for_the_resume() -> None:
    run = EngineRun("Seq[A, s]", scripts={"A": Script("S", 1)}).start()
    run.at(0.5)
    run.runner.pause()
    check(run, 0.5, State.PAUSED, "R R -")
    check(run, 1, State.PAUSED, "R R -")
    run.at(3.4)
    run.runner.resume()
    # 3.4: RUNNING S S S. The resume enters RUNNING, and its tick ends the run at once.
    check(run, 3.4, State.IDLE, "S S S")
    states = [state for when, state, _ in run.sink.states if when == 3.4]
    assert states[0] == State.RUNNING and states[-1] == State.IDLE
    assert run.result.outcome == Outcome.SUCCEEDED
    assert run.events_of("A") == [(0, "goal")]


def test_R_02_deadline_acts_during_a_pause() -> None:
    run = EngineRun("Seq[A]", scripts={"A": Script(timeout=2)}).start()
    run.at(1)
    run.runner.pause()
    check(run, 2, State.PAUSED, "R C")
    check(run, 2.2, State.PAUSED, "R F")
    run.at(5.39)
    assert run.result is None
    run.at(5.4)
    run.runner.resume()
    run.at(5.4)
    assert run.codes() == "F F"
    assert run.result.outcome == Outcome.FAILED


def test_R_03_stop_waits_for_cancels() -> None:
    run = EngineRun("Seq[A, s]").start()
    run.at(0.5)
    run.runner.stop()
    check(run, 0.5, State.STOPPING, "- C -")
    check(run, 0.7, State.IDLE, "- - -")
    result = run.result
    assert result.outcome == Outcome.STOPPED
    assert result.stopped_by_command  # goal canceled
    assert result.unconfirmed_cancels == 0


def test_R_04_reset_while_running_restarts() -> None:
    run = EngineRun("Seq[A, s]", scripts={"A": Script("S", 1)}).start()
    run.at(0.5)
    run.runner.reset()
    check(run, 0.5, State.RESTARTING, "- C -")
    check(run, 0.7, State.RESTARTING, "- - -")
    check(run, 1, State.RUNNING, "R R -")
    check(run, 2, State.IDLE, "S S S")
    assert run.events_of("A") == [(0, "goal"), (0.5, "cancel"), (1, "goal")]
    assert run.result.outcome == Outcome.SUCCEEDED


def test_R_05_reset_while_paused() -> None:
    run = EngineRun("Seq[A, s]", scripts={"A": Script("S", 1)}).start()
    run.at(0.5)
    run.runner.pause()
    run.at(0.8)
    run.runner.reset()
    check(run, 0.8, State.PAUSED, "- C -")
    check(run, 1, State.PAUSED, "- - -")
    run.at(3.4)
    run.runner.resume()
    check(run, 3.4, State.RUNNING, "R R -")
    check(run, 4.4, State.IDLE, "S S S")
    assert run.result.outcome == Outcome.SUCCEEDED


def test_R_06_reset_after_the_run() -> None:
    run = EngineRun("Seq[s, f]").start()
    assert (run.state, run.codes()) == (State.IDLE, "F S F")
    run.runner.reset()
    assert (run.state, run.codes()) == (State.IDLE, "- - -")


def test_R_07_second_start_is_refused() -> None:
    run = EngineRun("Seq[A]").start()
    run.at(0.5)
    with pytest.raises(CommandRefused):
        run.runner.start()
    assert (run.state, run.codes()) == (State.RUNNING, "R R")
    assert run.events_of("A") == [(0, "goal")]


def test_R_08_stop_node_stops_the_tree() -> None:
    run = EngineRun("Seq[s, Stop, n]").start()
    assert (run.state, run.codes()) == (State.IDLE, "- - - -")
    assert "n" not in run.calls
    assert run.result.outcome == Outcome.STOPPED
    assert not run.result.stopped_by_command  # goal aborted: the tree stopped itself


def test_R_09_pause_node() -> None:
    run = EngineRun("Seq[s, Pause, n]").start()
    assert (run.state, run.codes()) == (State.PAUSED, "R S R -")
    run.at(2.4)
    run.runner.resume()
    run.at(2.4)
    assert run.codes() == "S S S S"
    assert run.calls["n"] == [2.4]


def test_R_10_restart_node_runs_once_per_interval() -> None:
    run = EngineRun("Seq[n, Restart]").start()
    run.at(2.5)
    run.runner.stop()
    assert run.calls["n"] == [0, 1, 2]
    assert run.state == State.IDLE


def test_R_11_loop_execution_switched_off() -> None:
    run = EngineRun("Seq[A]", scripts={"A": Script("S", 0.5)}, config={"repeat": True}).start()
    assert run.codes() == "R R"
    check(run, 0.5, State.RUNNING, "S S")
    check(run, 1, State.RUNNING, "R R")
    run.at(1.2)
    run.runner.apply([{"op": "doc.set_config", "config": {"repeat": False}}])
    run.at(1.49)
    assert run.result is None
    check(run, 1.5, State.IDLE, "S S")
    assert run.result.outcome == Outcome.SUCCEEDED
    assert run.result.loops_completed == 2


def test_R_12_tick_ends_at_the_stop_node() -> None:
    run = EngineRun("Seq[All[Stop, n]]").start()
    assert (run.state, run.codes()) == (State.IDLE, "- - - -")
    assert "n" not in run.calls


def test_R_13_bundle_that_is_not_json() -> None:
    with pytest.raises(BundleError):
        TreeRunner("{not json", Blackboard(), Registry(), FakeScheduler(), None, lambda result: None)  # type: ignore[arg-type]


@pytest.mark.parametrize("bundle", [42, {"format": "other"}, {"root": "x", "trees": {}},
                                    {"root": "x", "trees": {"x": {"nodes": []}}}])
def test_R_13_unreadable_bundles(bundle: object) -> None:
    with pytest.raises(BundleError):
        TreeRunner(bundle, Blackboard(), Registry(), FakeScheduler(), None, lambda result: None)  # type: ignore[arg-type]


# ------------------------------------------------------------------------- the table of 4.4


def test_commands_refused_while_idle() -> None:
    run = EngineRun("Seq[s]")
    with pytest.raises(CommandRefused):
        run.runner.pause()
    with pytest.raises(CommandRefused):
        run.runner.resume()
    run.runner.stop()  # no effect
    assert run.state == State.IDLE and run.result is None


def test_commands_while_stopping() -> None:
    run = EngineRun("Seq[A]").start()
    run.runner.stop()
    assert run.state == State.STOPPING
    with pytest.raises(CommandRefused):
        run.runner.pause()
    with pytest.raises(CommandRefused):
        run.runner.resume()
    run.runner.stop()
    run.runner.reset()
    assert run.state == State.STOPPING
    run.at(0.2)
    assert run.state == State.IDLE


def test_pause_while_restarting_pauses_once_the_new_loop_started() -> None:
    run = EngineRun("Seq[A]").start()
    run.at(0.5)
    run.runner.reset()
    run.runner.pause()
    run.runner.resume()  # no effect while restarting, but drops the pending pause
    run.runner.pause()
    assert run.state == State.RESTARTING
    run.at(1)
    assert run.state == State.PAUSED
    assert run.codes() == "- -"
    run.runner.resume()
    run.at(1)
    assert (run.state, run.codes()) == (State.RUNNING, "R R")


def test_pause_and_resume_have_no_effect_when_already_there() -> None:
    run = EngineRun("Seq[A]").start()
    run.runner.resume()
    assert run.state == State.RUNNING
    run.runner.pause()
    run.runner.pause()
    assert run.state == State.PAUSED


def test_stop_while_restarting() -> None:
    run = EngineRun("Seq[A]").start()
    run.runner.reset()
    run.runner.stop()
    assert run.state == State.STOPPING
    run.at(0.2)
    assert run.state == State.IDLE and run.result.outcome == Outcome.STOPPED


def test_states_are_reported() -> None:
    run = EngineRun("Seq[A]").start()
    run.at(0.5)
    run.runner.pause()
    run.runner.resume()
    run.runner.stop()
    run.at(0.7)
    assert [state for _, state, _ in run.sink.states] == [
        State.RUNNING, State.PAUSED, State.RUNNING, State.STOPPING, State.IDLE]


def test_statuses_reported_at_most_once_per_tick() -> None:
    run = EngineRun("Seq[n, n, n]").start()
    assert len(run.sink.status_reports) == 1
    assert run.sink.status_reports[0][1] == [0, 1, 2, 3]


def test_change_inside_a_tick_requests_no_tick() -> None:
    run = EngineRun("Seq~[n, S1, W]", leaves={"S1": {"type": "Set", "fields": {
        "ValueName": "battery", "CompareTo": None, "LiteralValue": 10.0}}}).start()
    run.at(0.999)
    assert run.calls["n"] == [0]
    run.at(1)
    assert run.calls["n"] == [0, 1]


def test_tick_failure_ends_the_run_with_error(monkeypatch: pytest.MonkeyPatch) -> None:
    run = EngineRun("Seq[A]")
    root = run.runner.node(0)

    def broken() -> None:
        raise RuntimeError("engine bug")

    monkeypatch.setattr(root, "tick", broken)
    run.start()
    assert run.state == State.IDLE
    assert run.result.outcome == Outcome.ERROR
    assert run.codes()[0] == "F"
    assert "engine bug" in run.details("Root")["error"]


def test_event_ticks_are_capped() -> None:
    run = EngineRun("Seq~[n, W]").start()
    for step in range(10):
        run.set("battery", 1.0 + step)
    assert run.calls["n"] == [0, 0]  # the first request ticks at once, the rest are merged
    run.at(0.004)
    run.set("battery", 30.0)
    assert run.calls["n"] == [0, 0]
    run.at(0.005)
    assert run.calls["n"] == [0, 0, 0.005]


def test_apply_set_field_and_tick_interval_during_a_run() -> None:
    run = EngineRun("Seq~[n, E]", leaves={"E": {"type": "Evaluation", "fields": {
        "ValueName": "battery", "CompareTo": None, "LiteralValue": 50.0, "Operator": ">=", "Wait": True}}}).start()
    node = run.runner.node(run.index("E"))
    run.runner.apply([{"op": "node.set_field", "tree": "main", "id": node.node_id, "field": "LiteralValue",
                       "value": 10.0}])
    run.at(1)
    assert run.codes() == "S S S"
    run2 = EngineRun("Seq~[n, W]").start()
    run2.runner.apply([{"op": "doc.set_config", "config": {"tick_interval_ms": 250}}])
    run2.at(0.5)
    assert run2.calls["n"] == [0, 0.25, 0.5]
    with pytest.raises(Exception, match="cannot be applied"):
        run2.runner.apply([{"op": "node.add"}])


# ------------------------------------------------------------------------- regressions (review)


class Quick(AsyncLeaf):
    """A started leaf whose cancel is confirmed at once (AsyncLeaf's default on_cancel)."""

    TYPE_NAME = "Quick"


def quick_registry() -> Registry:
    registry = make_registry()
    registry.register(Quick)
    return registry


def test_pause_node_keeps_memory_position() -> None:
    run = EngineRun("Seq[n, Pause, s]").start()
    run.at(2.4)
    run.runner.resume()
    run.at(2.4)
    assert run.calls["n"] == [0]
    assert run.result.outcome == Outcome.SUCCEEDED
    run = EngineRun("Seq[A, Pause, s]", scripts={"A": Script("S", 0.5)}).start()
    run.at(1)
    assert (run.state, run.codes()) == (State.PAUSED, "R S R -")
    run.runner.resume()
    run.at(1.6)
    assert run.codes() == "S S S S"
    assert run.events_of("A") == [(0, "goal")]


def test_stop_waits_for_every_cancel_after_a_synchronous_settle() -> None:
    run = EngineRun("Seq[All[Q, B]]", registry=quick_registry(), leaves={"Q": {"type": "Quick"}},
                    scripts={"B": Script(cancel_after=None)}).start()
    run.at(0.5)
    run.runner.stop()
    assert (run.state, run.codes()) == (State.STOPPING, "- - - C")
    assert run.result is None
    run.at(5.5)
    assert run.state == State.IDLE
    assert run.result.outcome == Outcome.STOPPED and run.result.unconfirmed_cancels == 1


@pytest.mark.parametrize("paused", [False, True])
def test_reset_waits_for_every_cancel_after_a_synchronous_settle(paused: bool) -> None:
    run = EngineRun("Seq[S9, All[Q, B]]", registry=quick_registry(), config={"restore_blackboard": True},
                    entries=[{"name": "m", "type": "Integer", "value": 1}], scripts={"B": Script(cancel_after=2.0)},
                    leaves={"Q": {"type": "Quick"}, "S9": {"type": "Set", "fields": {
                        "ValueName": "m", "CompareTo": None, "LiteralValue": 9}}}).start()
    run.at(0.5)
    if paused:
        run.runner.pause()
    run.runner.reset()
    assert run.codes() == "- - - - C"
    assert run.blackboard.get("m") == 9  # restored only once every cancel is confirmed
    run.at(2.5)
    assert run.blackboard.get("m") == 1
    assert run.state == (State.PAUSED if paused else State.RESTARTING)


def test_tick_failure_sends_the_queued_cancels() -> None:
    run = EngineRun("Seq[Sel~[c, A], s]").start()
    run.at(0.5)
    failing = run.runner.node(run.index("s"))

    def broken() -> None:
        raise RuntimeError("engine bug")

    failing.tick = broken  # type: ignore[method-assign]
    run.set("go", True)
    assert run.events_of("A") == [(0, "goal"), (0.5, "cancel")]
    assert run.state == State.STOPPING
    run.at(0.7)
    assert run.state == State.IDLE and run.result.outcome == Outcome.ERROR


def test_restore_switched_on_during_the_run() -> None:
    run = EngineRun("Seq[S9, A]", entries=[{"name": "m", "type": "Integer", "value": 1}],
                    leaves={"S9": {"type": "Set", "fields": {"ValueName": "m", "CompareTo": None,
                                                             "LiteralValue": 9}}}).start()
    run.runner.apply([{"op": "doc.set_config", "config": {"restore_blackboard": True}}])
    run.at(0.5)
    run.runner.stop()
    run.at(1)
    assert run.blackboard.get("m") == 1


def test_run_does_not_end_while_paused() -> None:
    run = EngineRun("Seq~[c, A]", entries=[{"name": "go", "type": "Bool", "value": True}]).start()
    run.at(2.3)
    run.set("go", False)
    run.at(2.4)
    run.runner.pause()
    run.at(2.5)
    assert (run.state, run.codes()) == (State.PAUSED, "F F -")
    assert run.result is None
    run.at(3)
    run.runner.resume()
    assert run.state == State.IDLE and run.result.outcome == Outcome.FAILED


def test_loop_execution_switched_off_while_paused_between_loops() -> None:
    run = EngineRun("Seq[s]", config={"repeat": True}).start()
    run.runner.pause()
    run.runner.apply([{"op": "doc.set_config", "config": {"repeat": False}}])
    assert run.result is None
    run.runner.resume()
    assert run.state == State.IDLE and run.result.outcome == Outcome.SUCCEEDED


def test_periodic_tick_skips_missed_periods() -> None:
    times: list[float] = []

    class Slow(LeafNodeWidget):
        TYPE_NAME = "Slow"
        RUN_IN_THREAD = False

        def OnRun(self, tree: object) -> object:  # noqa: N802
            times.append(round(self.ctx.now(), 3))
            if len(times) == 2:
                self.ctx.scheduler._now += 1.0  # this call takes 1 s  # type: ignore[attr-defined]
            return Status.RUNNING

    registry = make_registry()
    registry.register(Slow)
    run = EngineRun("Seq[I]", registry=registry, leaves={"I": {"type": "Slow"}}, config={"tick_interval_ms": 100})
    run.start()
    run.at(1.5)
    assert times == [0.0, 0.1, 1.2, 1.3, 1.4, 1.5]
