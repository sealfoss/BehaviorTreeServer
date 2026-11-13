"""Leaf classes (design 6.6): thread leaves, leaves written for the widget, async leaves, bindings
and outputs, details from leaf code, the built-in Print Message and UDP Message, and the registry."""

from __future__ import annotations

import socket
import sys
import threading
import time
import types
from typing import Any

import pytest
from engine_helpers import EngineRun, make_registry

from bt_server_core.compat import ExecutionCancelled, LeafNodeWidget, NodeStatus, Status
from bt_server_core.engine import Outcome, State
from bt_server_core.leaves import AsyncLeaf, InstantLeaf, LeafContext, LeafFailure, ThreadLeaf, split_path
from bt_server_core.registry import Registry, RegistryError

# ------------------------------------------------------------------------- leaf classes used below


class Sleeper(ThreadLeaf):
    """Sleeps, checking cancel_requested; writes "done"."""

    fields = {"seconds": 0.05}

    def run(self, ctx: LeafContext) -> Any:
        deadline = time.monotonic() + self.get("seconds")
        while time.monotonic() < deadline:
            if ctx.cancel_requested():
                return False
            time.sleep(0.002)
        ctx.set("done", True)
        return True


GATE = threading.Event()
EVENTS: list[tuple[str, Any]] = []


class Stubborn(ThreadLeaf):
    """Blocks until GATE is set, whatever happens."""

    def run(self, ctx: LeafContext) -> Any:
        GATE.wait(10)
        return True


class WidgetLeaf(LeafNodeWidget):
    """A widget leaf on a worker thread: waits for GATE, then writes its field x."""

    _title = "Widget Leaf"
    _fields = {"x": 1.5, "mode": ["walk", "run"]}

    def OnStart(self, tree: Any) -> None:  # noqa: N802
        EVENTS.append(("start", self.GetFieldSelection("mode")))

    def OnRun(self, tree: Any) -> Any:  # noqa: N802
        GATE.wait(10)
        try:
            tree.SetEntry(self.GetField("x"), "out")
        except ExecutionCancelled:
            EVENTS.append(("cancelled", self.CancelRequested()))
            raise
        return True

    def OnTerminate(self, tree: Any, status: NodeStatus) -> None:  # noqa: N802
        EVENTS.append(("terminate", status))


class Polling(LeafNodeWidget):
    """A widget leaf on the engine thread that returns RUNNING twice."""

    RUN_IN_THREAD = False
    _fields = {"ticks": 2}

    def OnStart(self, tree: Any) -> None:  # noqa: N802
        self._left = self.GetField("ticks")

    def OnRun(self, tree: Any) -> Any:  # noqa: N802
        EVENTS.append(("run", None))
        if self._left > 0:
            self._left -= 1
            return Status.RUNNING
        return True


class NoArg(LeafNodeWidget):
    RUN_IN_THREAD = False

    def OnRun(self) -> Any:  # noqa: N802 - the widget allows leaving out the tree
        return self.GetTree().HasEntry("go")


class Echo(AsyncLeaf):
    """Writes outputs, details, a field and the title, then succeeds."""

    fields = {"Goal": {"pose": {"x": 0.0, "y": 0.0}}, "Timeout": 0.0}

    def on_start(self, ctx: LeafContext) -> None:
        ctx.detail("feedback", {"goal": self.get("Goal")})
        ctx.set_field("Timeout", 9.0)
        ctx.set_title("Echoed")
        problems = ctx.write_outputs({"Result": {"echo": self.get("Goal")["pose"]["x"], "list": [4, 5]}})
        EVENTS.append(("problems", problems))
        self.succeed()


class Refuses(AsyncLeaf):
    def on_start(self, ctx: LeafContext) -> None:
        self.fail("refused by the server")


class Raises(InstantLeaf):
    def tick(self, ctx: LeafContext) -> Any:
        return "maybe"


class Fails(InstantLeaf):
    def tick(self, ctx: LeafContext) -> Any:
        raise LeafFailure("not today")


def registry(*classes: type) -> Registry:
    result = make_registry()
    for cls in classes:
        result.register(cls)
    return result


@pytest.fixture(autouse=True)
def _reset_gate() -> Any:
    GATE.clear()
    EVENTS.clear()
    yield
    GATE.set()


def wait(run: EngineRun, predicate: Any) -> None:
    assert run.scheduler.wait_for(predicate, 5.0)


# ------------------------------------------------------------------------- thread leaves


def test_thread_leaf_runs_on_a_worker_thread() -> None:
    run = EngineRun("Seq[T]", leaves={"T": {"type": "Sleeper"}}, registry=registry(Sleeper)).start()
    assert run.codes() == "R R"
    wait(run, lambda: run.result is not None)
    assert run.codes() == "S S"
    assert run.blackboard.get("done") is True


def test_halted_thread_leaf_shows_cancelling_until_it_returns() -> None:
    run = EngineRun("Seq~[c, T]", leaves={"T": {"type": "Sleeper", "fields": {"seconds": 5.0}}},
                    entries=[{"name": "go", "type": "Bool", "value": True}], registry=registry(Sleeper)).start()
    assert run.codes() == "R S R"
    run.set("go", False)
    assert run.codes() == "F F C"
    wait(run, lambda: run.result is not None)
    assert run.codes() == "F F -"
    assert run.result.outcome == Outcome.FAILED
    assert not run.blackboard.has("done")


def test_thread_leaf_ignoring_the_cancel_becomes_unsettled() -> None:
    run = EngineRun("Seq~[c, T]", leaves={"T": {"type": "Stubborn"}}, config={"on_unconfirmed_cancel": "continue"},
                    entries=[{"name": "go", "type": "Bool", "value": True}], registry=registry(Stubborn)).start()
    run.set("go", False)
    assert run.codes() == "F F C"
    run.at(5)
    assert run.codes() == "F F -" and run.result.unconfirmed_cancels == 1
    assert run.runner.unsettled == 1
    GATE.set()
    wait(run, lambda: run.runner.unsettled == 0)


def test_thread_leaf_not_restarted_before_its_call_returned() -> None:
    run = EngineRun("Seq~[c, T]", leaves={"T": {"type": "Stubborn"}}, config={"on_unconfirmed_cancel": "continue",
                                                                            "cancel_timeout_ms": 1000},
                    entries=[{"name": "go", "type": "Bool", "value": True}], registry=registry(Stubborn))
    run.runner.config["repeat"] = True
    run.start()
    run.set("go", False)
    run.at(1)  # the cancel times out: unsettled
    run.set("go", True)
    run.at(2)
    assert run.codes() == "R S C"  # ticked again while its call runs: it waits, showing Cancelling
    GATE.set()
    wait(run, lambda: run.codes() == "R S R" or run.codes() == "S S S")


# ------------------------------------------------------------------------- leaves written for the widget


def test_widget_leaf_on_a_thread() -> None:
    run = EngineRun("Seq[L]", leaves={"L": {"type": "WidgetLeaf", "fields": {"x": 2.5},
                                            "field_selections": {"mode": 1}}},
                    registry=registry(WidgetLeaf)).start()
    assert run.codes() == "R R"
    GATE.set()
    wait(run, lambda: run.result is not None)
    assert run.codes() == "S S"
    assert run.blackboard.get("out") == 2.5
    assert EVENTS == [("start", "run"), ("terminate", NodeStatus.SUCCEEDED)]


def test_halted_widget_call_is_abandoned_at_once() -> None:
    run = EngineRun("Seq~[c, L]", leaves={"L": {"type": "WidgetLeaf"}}, registry=registry(WidgetLeaf),
                    entries=[{"name": "go", "type": "Bool", "value": True}]).start()
    run.set("go", False)
    assert run.codes() == "F F -"  # never Cancelling, never holds up the tree
    assert run.result.outcome == Outcome.FAILED
    assert ("terminate", NodeStatus.READY) in EVENTS
    GATE.set()
    wait(run, lambda: ("cancelled", True) in EVENTS)
    assert not run.blackboard.has("out")


def test_widget_leaf_waits_for_its_abandoned_call() -> None:
    run = EngineRun("Seq~[c, L]", leaves={"L": {"type": "WidgetLeaf"}}, registry=registry(WidgetLeaf),
                    entries=[{"name": "go", "type": "Bool", "value": True}], config={"repeat": True}).start()
    run.set("go", False)
    assert run.codes() == "F F -"
    run.at(0.5)
    run.set("go", True)
    run.at(1)  # the next loop: L is ticked while its abandoned call still runs
    assert run.codes() == "R S R"
    assert "previous call" in run.details("L")["waiting"]
    assert [event for event in EVENTS if event[0] == "start"] == [("start", "walk")]
    GATE.set()
    wait(run, lambda: len([event for event in EVENTS if event[0] == "start"]) == 2)


def test_inline_widget_leaf_called_once_per_interval() -> None:
    run = EngineRun("Seq~[n, P]", leaves={"P": {"type": "Polling"}}, registry=registry(Polling)).start()
    run.at(0.5)
    run.set("battery", 30.0)  # an event tick: n runs, P is not called
    assert run.calls["n"] == [0, 0.5]
    assert len(EVENTS) == 1
    run.at(2)
    assert run.codes() == "S S S"
    assert len(EVENTS) == 3


def test_widget_onrun_without_tree_argument() -> None:
    run = EngineRun("Seq[Q]", leaves={"Q": {"type": "NoArg"}}, registry=registry(NoArg)).start()
    assert run.codes() == "S S"


# ------------------------------------------------------------------------- async leaves, bindings, outputs


def test_async_leaf_bindings_outputs_and_details() -> None:
    entries = [{"name": "target", "type": "Double", "value": 3.5}, {"name": "go", "type": "Bool", "value": True}]
    run = EngineRun("Seq[E]", entries=entries, registry=registry(Echo), leaves={"E": {
        "type": "Echo", "bindings": {"Goal.pose.x": "target"},
        "outputs": {"Result.echo": "echoed", "Result.list[1]": "second", "Result.nothing": "x"}}}).start()
    assert run.codes() == "S S"
    assert run.blackboard.get("echoed") == 3.5
    assert run.blackboard.get("second") == 5
    assert EVENTS == [("problems", ["Result.nothing is not in the data"])]
    index = run.index("Echoed")
    kinds = {kind for _, at, kind, _ in run.sink.detail_history if at == index}
    assert {"feedback", "field", "title"} <= kinds
    field = next(value for _, at, kind, value in run.sink.detail_history if at == index and kind == "field")
    assert field == {"tree": "main", "id": run.runner.node(index).node_id, "field": "Timeout", "value": 9.0}


def test_binding_to_a_missing_entry_fails_when_the_leaf_starts() -> None:
    run = EngineRun("Seq[E]", registry=registry(Echo), leaves={"E": {"type": "Echo",
                                                                     "bindings": {"Goal.pose.x": "nowhere"}}}).start()
    assert run.codes() == "F F"
    assert "nowhere" in run.details("E")["error"]


def test_binding_to_a_sourced_entry_without_data_fails() -> None:
    entries = [{"name": "pose_x", "type": "Double", "value": 0.0, "source": {"topic": "/x", "type": "t"}}]
    run = EngineRun("Seq[E]", entries=entries, registry=registry(Echo),
                    leaves={"E": {"type": "Echo", "bindings": {"Goal.pose.x": "pose_x"}}}).start()
    assert run.codes() == "F F"
    assert "no recent data" in run.details("E")["error"]


def test_failures_with_reasons() -> None:
    run = EngineRun("Seq~[Sel[R1, R2, R3]]", registry=registry(Refuses, Raises, Fails),
                    leaves={"R1": {"type": "Refuses"}, "R2": {"type": "Raises"}, "R3": {"type": "Fails"}}).start()
    assert run.codes() == "F F F F F"
    assert run.details("R1")["error"] == "refused by the server"
    assert "must return True, False or a Status" in run.details("R2")["error"]
    assert run.details("R3")["error"] == "not today"
    errors = [text for _, level, text, _ in run.sink.logs if level == 1]
    assert len(errors) == 1 and "R2" in errors[0]


def test_split_path() -> None:
    assert split_path("Goal.waypoints[0].x") == ["Goal", "waypoints", 0, "x"]
    for bad in ("", ".a", "a..b", "[0]", "a[x]", "a]"):
        with pytest.raises(ValueError):
            split_path(bad)


# ------------------------------------------------------------------------- built-in leaves


def test_print_message() -> None:
    leaves = {"P1": {"type": "Print Message", "fields": {"Level": 2, "Source": None, "Message": "hello"}},
              "P2": {"type": "Print Message", "fields": {"Level": 3, "Source": "battery", "Message": ""}},
              "P3": {"type": "Print Message", "fields": {"Level": 0, "Source": "missing", "Message": ""}},
              "P4": {"type": "Print Message", "fields": {"Level": 3, "Source": "missing", "Message": ""}}}
    run = EngineRun("Seq[P1, P2, P3, P4]", leaves=leaves).start()
    assert run.codes() == "F S S S F"
    logged = [(level, text, index) for _, level, text, index in run.sink.logs if not text.startswith(("Node", "Exec"))]
    assert logged == [(2, "hello", 1), (3, "battery = 17.3", 2)]
    assert "missing" in run.details("P4")["error"]


def test_udp_message_sends_a_datagram() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(5)
        port = receiver.getsockname()[1]
        run = EngineRun("Seq[U]", leaves={"U": {"type": "UDP Message", "fields": {
            "Destination IP": "127.0.0.1", "Destination Port": port, "Message": "patrol started"}}}).start()
        wait(run, lambda: run.result is not None)
        assert run.codes() == "S S"
        assert receiver.recvfrom(100)[0] == b"patrol started"


def test_udp_message_bad_port_fails() -> None:
    run = EngineRun("Seq[U]", leaves={"U": {"type": "UDP Message", "fields": {"Destination Port": 70000}}}).start()
    wait(run, lambda: run.result is not None)
    assert "between 1 and 65535" in run.details("U")["error"]


# ------------------------------------------------------------------------- the registry


def test_registry_imports_leaf_modules() -> None:
    reg = Registry(["bt_server_core.demo_leaves"])
    assert set(reg.custom_types()) == {"Wait", "LogMessage", "IncrementCounter", "CounterAtLeast", "RandomOutcome",
                                       "MoveTo", "Fail", "RunningTicks"}
    catalog = {item["type"]: item for item in reg.catalog()}
    assert catalog["MoveTo"]["builtin"] is False and catalog["MoveTo"]["category"] == "Custom"
    assert catalog["MoveTo"]["title"] == "Move To"
    assert [field["kind"] for field in catalog["MoveTo"]["fields"]] == ["float", "float", "int", "choice", "bool", "str"]
    assert catalog["Wait"]["leaf_kind"] == "thread" and catalog["Fail"]["leaf_kind"] == "instant"
    assert catalog["Evaluation"]["builtin"] is True
    assert "CallAction" not in catalog


def test_demo_leaves_run() -> None:
    reg = Registry(["bt_server_core.demo_leaves"])
    leaves = {"I": {"type": "IncrementCounter"}, "C": {"type": "CounterAtLeast", "fields": {"threshold": 2}},
              "L": {"type": "LogMessage"}, "R": {"type": "RunningTicks", "fields": {"ticks": 1}}}
    run = EngineRun("Seq[I, I, C, L, R]", leaves=leaves, registry=reg, entries=[]).start()
    run.at(1)
    assert run.codes() == "S S S S S S"
    assert run.blackboard.get("counter") == 2
    assert run.blackboard.get("last_message") == "Hello from the behavior tree!"


def test_reserved_and_duplicate_names_are_refused() -> None:
    reg = Registry()

    class Set(InstantLeaf):  # noqa: D401 - a clash with a built-in name
        def tick(self, ctx: LeafContext) -> Any:
            return True

    class UdpClash(InstantLeaf):
        TYPE_NAME = "UDP Message"

    with pytest.raises(RegistryError, match="built-in"):
        reg.register(Set)
    with pytest.raises(RegistryError, match="built-in"):
        reg.register(UdpClash)
    reg.register(Sleeper)

    class Other(ThreadLeaf):
        TYPE_NAME = "Sleeper"

    with pytest.raises(RegistryError, match="already registered"):
        reg.register(Other)


def test_setup_hook_adds_object_entries_to_runs() -> None:
    module = types.ModuleType("bt_test_leaf_module")
    device = object()

    class Probe(InstantLeaf):
        def tick(self, ctx: LeafContext) -> Any:
            return ctx.get("device") is device

    Probe.__module__ = module.__name__
    module.Probe = Probe  # type: ignore[attr-defined]
    module.setup = lambda board: board.put_object("device", device)  # type: ignore[attr-defined]
    sys.modules[module.__name__] = module
    try:
        reg = Registry([module.__name__])
        for cls in (make_registry().custom_types().values()):
            reg.register(cls)
        assert reg.run_setup() == []
        run = EngineRun("Seq[P]", leaves={"P": {"type": "Probe"}}, registry=reg,
                        entries=[{"name": "device", "type": "Integer", "value": 1}]).start()
        assert run.codes() == "S S"
        assert run.blackboard.type_of("device") == "Object"
        assert all(item["name"] != "device" for item in run.result.blackboard)
    finally:
        del sys.modules[module.__name__]


def test_ros_leaf_hook() -> None:
    class FakeAction(AsyncLeaf):
        def on_start(self, ctx: LeafContext) -> None:
            self.succeed()

    reg = make_registry()
    reg.register_ros_leaf("CallAction", FakeAction)
    assert any(item["type"] == "CallAction" for item in reg.catalog())
    run = EngineRun("Seq[q]", leaves={"q": {"type": "CallAction"}}, registry=reg).start()
    assert run.codes() == "S S"
    with pytest.raises(RegistryError):
        reg.register_ros_leaf("Evaluation", FakeAction)


def test_commands_from_a_worker_thread() -> None:
    class Stopper(ThreadLeaf):
        def run(self, ctx: LeafContext) -> Any:
            ctx.stop()
            return True

    run = EngineRun("Seq[T]", leaves={"T": {"type": "Stopper"}}, registry=registry(Stopper)).start()
    wait(run, lambda: run.result is not None)
    assert run.result.outcome == Outcome.STOPPED
    assert run.state == State.IDLE


# ------------------------------------------------------------------------- regressions (review)


def test_widget_set_field_reads_back_at_once() -> None:
    seen = []

    class Counter(LeafNodeWidget):
        _fields = {"count": 0}

        def OnRun(self, tree: Any) -> Any:  # noqa: N802
            self.SetField("count", self.GetField("count") + 1)
            self.SetField("count", self.GetField("count") + 1)
            self.SetFieldSelectionIndex("mode", 1)
            seen.append((self.GetField("count"), self.GetFieldSelection("mode")))
            return True

    Counter._fields = {"count": 0, "mode": ["a", "b"]}
    run = EngineRun("Seq[K]", leaves={"K": {"type": "Counter"}}, registry=registry(Counter)).start()
    wait(run, lambda: run.result is not None)
    assert seen == [(2, "b")]
    node = run.runner.node(run.index("K"))
    assert node.fields["count"] == 2
    reported = [value["value"] for _, _, kind, value in run.sink.detail_history if kind == "field"]
    assert reported == [1, 2]


def test_thread_call_from_an_earlier_run_ticks_the_new_run() -> None:
    calls: list[float] = []

    class Slowpoke(ThreadLeaf):
        def run(self, ctx: LeafContext) -> Any:
            calls.append(ctx.now())
            GATE.wait(10)
            return True

    reg = registry(Slowpoke)
    first = EngineRun("Seq[T]", registry=reg, leaves={"T": {"type": "Slowpoke"}}, config={"cancel_timeout_ms": 500})
    first.start()
    wait(first, lambda: len(calls) == 1)
    first.at(0.5)
    first.runner.stop()
    first.at(1.5)
    assert first.result.unconfirmed_cancels == 1
    second = EngineRun("Seq[T]", registry=reg, leaves={"T": {"type": "Slowpoke"}})
    second.scheduler.advance_to(1.5)
    second.start()
    assert second.codes() == "R R"
    assert "previous call" in second.details("T")["waiting"]
    GATE.set()
    wait(second, lambda: len(calls) == 2)  # started when the old call returned, not at the next periodic tick
    assert calls[1] == 1.5


# ------------------------------------------------------------------------- Remote Sub-Tree bindings, cancel_and_fail


class FakeRemoteSubTree(AsyncLeaf):
    """Stands in for the executor's Remote Sub-Tree: records what it sees."""

    fields = {"Executor": "", "File": "dock_BT.json", "Timeout": 0.0, "Watchdog": 5.0}

    def on_start(self, ctx: LeafContext) -> None:
        EVENTS.append(("seen", dict(ctx.bindings), ctx.bound_values(), ctx.field("File"), ctx.field("Timeout")))
        self.succeed()


def remote_registry() -> Registry:
    reg = make_registry()
    reg.register_ros_leaf("RemoteSubTree", FakeRemoteSubTree)
    return reg


def test_remote_sub_tree_bindings_are_entry_to_entry() -> None:
    entries = [{"name": "dock", "type": "Integer", "value": 3}, {"name": "pose", "type": "List", "value": [1, 2]}]
    bindings = {"File": "dock", "waypoints[0]": "pose"}
    run = EngineRun("Seq[R]", entries=entries, registry=remote_registry(),
                    leaves={"R": {"type": "RemoteSubTree", "bindings": bindings}}).start()
    assert run.codes() == "S S"
    assert EVENTS == [("seen", bindings, {"File": 3, "waypoints[0]": [1, 2]}, "dock_BT.json", 0.0)]
    node = run.runner.node(run.index("R"))
    with pytest.raises(TypeError):
        node.ctx.bindings["File"] = "other"  # type: ignore[index]


def test_remote_sub_tree_binding_to_a_missing_entry_fails_at_start() -> None:
    run = EngineRun("Seq[R]", entries=[], registry=remote_registry(),
                    leaves={"R": {"type": "RemoteSubTree", "bindings": {"dock_id": "nowhere"}}}).start()
    assert run.codes() == "F F"
    assert "nowhere" in run.details("R")["error"]
    assert EVENTS == []


def test_bound_values_of_field_bindings() -> None:
    class Reader(AsyncLeaf):
        fields = {"Goal": {"x": 0.0}}

        def on_start(self, ctx: LeafContext) -> None:
            EVENTS.append(("values", ctx.bound_values(), ctx.field("Goal")))
            self.succeed()

    run = EngineRun("Seq[G]", entries=[{"name": "x", "type": "Double", "value": 2.5}], registry=registry(Reader),
                    leaves={"G": {"type": "Reader", "bindings": {"Goal.x": "x"}}}).start()
    assert run.codes() == "S S"
    assert EVENTS == [("values", {"Goal.x": 2.5}, {"x": 2.5})]


class GivesUp(AsyncLeaf):
    """Gives up at 1 s with "no feedback"; its cancel is confirmed after ``confirm`` s (None: never)."""

    fields = {"confirm": 0.3}

    def on_start(self, ctx: LeafContext) -> None:
        self._timer = ctx.scheduler.call_at(ctx.now() + 1.0, lambda: self.cancel_and_fail("no feedback"))

    def on_cancel(self, ctx: LeafContext) -> None:
        EVENTS.append(("cancel", ctx.now()))
        confirm = self.get("confirm")
        if confirm is not None:
            ctx.scheduler.call_at(ctx.now() + confirm, self.settled)

    def on_end(self, ctx: LeafContext) -> None:
        self._timer.cancel()


def test_async_leaf_cancel_and_fail() -> None:
    run = EngineRun("Seq[G, s]", registry=registry(GivesUp), leaves={"G": {"type": "GivesUp"}}).start()
    run.at(1)
    assert run.codes() == "R C -"
    assert run.details("G")["error"] == "no feedback"
    assert EVENTS == [("cancel", 1.0)]
    run.at(1.29)
    assert run.codes() == "R C -"
    run.at(1.3)
    assert run.codes() == "F F -"
    assert run.result.outcome == Outcome.FAILED
    assert (1.3, "Node 'G' (GivesUp) finished: Failed (no feedback)") in run.sink.lines()


def test_cancel_and_fail_with_an_unconfirmed_cancel() -> None:
    run = EngineRun("Seq[G]", registry=registry(GivesUp), config={"on_unconfirmed_cancel": "continue"},
                    leaves={"G": {"type": "GivesUp", "fields": {"confirm": None}}}).start()
    run.at(6)
    assert run.codes() == "F F"
    assert run.result.outcome == Outcome.FAILED
    assert run.result.unconfirmed_cancels == 1 and run.runner.unsettled == 1


def test_cancel_and_fail_from_another_thread_and_once_ended() -> None:
    class Idle(AsyncLeaf):
        pass

    run = EngineRun("Seq~[c, I]", registry=registry(Idle), leaves={"I": {"type": "Idle"}},
                    entries=[{"name": "go", "type": "Bool", "value": True}]).start()
    leaf = run.runner.node(run.index("I")).leaf
    worker = threading.Thread(target=leaf.cancel_and_fail, args=("server lost",))
    worker.start()
    worker.join()
    wait(run, lambda: run.result is not None)
    assert run.codes() == "F S F"
    assert run.details("I")["error"] == "server lost"
    leaf.cancel_and_fail("too late")  # no effect once the leaf is no longer active
    assert run.details("I")["error"] == "server lost"
