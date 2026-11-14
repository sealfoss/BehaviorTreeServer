"""Helpers for the engine tests: trees written as in Appendix B.1, scripted leaves, a recording
sink and a fake clock.

    run = EngineRun("Seq[A, s]", scripts={"A": Script(result="S", after=1.5)})
    run.start()
    assert run.codes() == "R R -"
    run.at(1.5)
    assert run.codes() == "S S S"

Trees: ``Seq[...]``, ``Sel[...]`` (Memory on), ``Seq~[...]``, ``Sel~[...]`` (Memory off),
``Neg[...]``, ``All[...]``, ``Any[...]``. Leaves: ``s``, ``f`` (instant, succeed / fail), ``n``
(succeeds and counts its calls), ``x`` (raises), ``c`` (Evaluation ``go == true``), ``W``
(Evaluation ``battery >= 20.0`` with Wait), ``Stop``, ``Pause``, ``Restart``, upper-case letters
(scripted started leaves, :class:`Script`), and any name given in ``leaves`` (a node dict).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Any

from bt_server_core.blackboard import Blackboard
from bt_server_core.engine import RunResult, State, Status, TreeRunner
from bt_server_core.leaves import AsyncLeaf, InstantLeaf, LeafContext
from bt_server_core.registry import Registry
from bt_server_core.scheduler import FakeScheduler

LETTERS = {Status.READY: "-", Status.RUNNING: "R", Status.SUCCEEDED: "S", Status.FAILED: "F", Status.CANCELLING: "C"}
COMPOSITES = {
    "Seq": ("Sequence", True), "Sel": ("Selector", True), "Seq~": ("Sequence", False), "Sel~": ("Selector", False),
}
DEFAULT_ENTRIES = [
    {"name": "go", "type": "Bool", "value": False},
    {"name": "battery", "type": "Double", "value": 17.3},
]


@dataclass
class Script:
    """A scripted started leaf: ``result`` ``"S"`` or ``"F"`` ``after`` seconds after it starts
    (None: never), a cancel confirmed ``cancel_after`` seconds after it is sent (None: never),
    and a ``timeout`` (its Timeout field)."""

    result: str | None = None
    after: float = 0.0
    cancel_after: float | None = 0.2
    timeout: float = 0.0


class TestSucceed(InstantLeaf):
    TYPE_NAME = "TestS"

    def tick(self, ctx: LeafContext) -> Any:
        return True


class TestFail(InstantLeaf):
    TYPE_NAME = "TestF"

    def tick(self, ctx: LeafContext) -> Any:
        return False


class TestCount(InstantLeaf):
    TYPE_NAME = "TestN"

    def tick(self, ctx: LeafContext) -> Any:
        ctx.run.count(ctx.title)
        return True


class TestRaise(InstantLeaf):
    TYPE_NAME = "TestX"

    def tick(self, ctx: LeafContext) -> Any:
        raise RuntimeError("boom")


class TestScripted(AsyncLeaf):
    """A started leaf that follows the :class:`Script` of its name and records what its server saw."""

    TYPE_NAME = "TestA"
    fields = {"Name": "", "Timeout": 0.0}

    def on_start(self, ctx: LeafContext) -> None:
        run: EngineRun = ctx.run
        name = ctx.field("Name")
        run.event(name, "goal")
        script = run.scripts.get(name, Script())
        self._timers = []
        if script.result is not None:
            finish = self.succeed if script.result == "S" else self.fail
            self._timers.append(ctx.scheduler.call_at(ctx.now() + script.after, finish))

    def on_cancel(self, ctx: LeafContext) -> None:
        run: EngineRun = ctx.run
        name = ctx.field("Name")
        run.event(name, "cancel")
        for timer in self._timers:
            timer.cancel()
        script = run.scripts.get(name, Script())
        if script.cancel_after is not None:
            self._timers.append(ctx.scheduler.call_at(ctx.now() + script.cancel_after, self.settled))

    def on_end(self, ctx: LeafContext) -> None:
        for timer in self._timers:
            timer.cancel()


def make_registry() -> Registry:
    registry = Registry()
    for cls in (TestSucceed, TestFail, TestCount, TestRaise, TestScripted):
        registry.register(cls)
    return registry





def _tokens(text: str) -> list[str]:
    tokens, word = [], ""
    for char in text:
        if char in "[],":
            if word.strip():
                tokens.append(word.strip())
            word = ""
            tokens.append(char)
        else:
            word += char
    if word.strip():
        tokens.append(word.strip())
    return tokens


class TreeBuilder:
    """Builds a bundle from a tree written as in Appendix B.1."""

    def __init__(self, leaves: dict[str, dict] | None = None, options: dict[str, dict] | None = None,
                 scripts: dict[str, Script] | None = None) -> None:
        self.leaves = leaves or {}
        self.options = options or {}
        self.scripts = scripts or {}
        self.nodes: list[dict] = []
        self.connections: list[dict] = []
        self._ids = itertools.count(1)

    def _new(self, data: dict) -> dict:
        data["id"] = f"{next(self._ids):032x}"
        self.nodes.append(data)
        return data

    def bundle(self, text: str, config: dict | None = None) -> dict:
        tokens = _tokens(text)
        position = self._parse(tokens, 0, None, root=True)
        assert position == len(tokens), f"trailing text in {text!r}"
        return {"format": "behavior_tree_bundle", "version": 1, "root": "main",
                "trees": {"main": {"config": config or {}, "nodes": self.nodes, "connections": self.connections}}}

    def _parse(self, tokens: list[str], position: int, parent: dict | None, root: bool = False) -> int:
        name = tokens[position]
        position += 1
        node = self._node(name, root)
        if parent is not None:
            self.connections.append({"parent": parent["id"], "child": node["id"]})
        if position < len(tokens) and tokens[position] == "[":
            position += 1
            while tokens[position] != "]":
                position = self._parse(tokens, position, node)
                if tokens[position] == ",":
                    position += 1
            position += 1
        return position

    def _node(self, name: str, root: bool) -> dict:
        options = dict(self.options.get(name, {}))
        if name in COMPOSITES:
            composite, memory = COMPOSITES[name]
            if root:
                return self._new({"type": "Root", "title": "Root", "composite": composite, "memory": memory, **options})
            return self._new({"type": composite, "title": name, "memory": memory, **options})
        if name in ("All", "Any"):
            return self._new({"type": "Parallel", "title": name, "policy": name.lower(), **options})
        if name == "Neg":
            return self._new({"type": "Negation", "title": "Neg", **options})
        if name in self.leaves:
            data = dict(self.leaves[name])
            data.setdefault("title", name)
            return self._new({**data, **options})
        if name in ("s", "f", "n", "x"):
            return self._new({"type": f"Test{name.upper()}", "title": name, **options})
        if name == "c":
            return self._new({"type": "Evaluation", "title": "c", "fields": {
                "ValueName": "go", "CompareTo": None, "LiteralValue": True}, **options})
        if name == "W":
            fields = {"ValueName": "battery", "CompareTo": None, "LiteralValue": 20.0, "Operator": ">=", "Wait": True}
            fields.update(options.pop("fields", {}))
            return self._new({"type": "Evaluation", "title": "W", "fields": fields, **options})
        if name in ("Stop", "Pause", "Restart"):
            return self._new({"type": name, "title": name, **options})
        if name.isupper():
            script = self.scripts.get(name, Script())
            return self._new({"type": "TestA", "title": name,
                              "fields": {"Name": name, "Timeout": script.timeout}, **options})
        raise ValueError(f"unknown leaf {name!r}")


class Recorder:
    """A Sink that records everything, with the time of the fake clock."""

    def __init__(self, scheduler: FakeScheduler) -> None:
        self.scheduler = scheduler
        self.states: list[tuple[float, State, int]] = []
        self.status_reports: list[tuple[float, list[int]]] = []
        self.details: dict[int, dict[str, object]] = {}
        self.detail_history: list[tuple[float, int, str, object]] = []
        self.logs: list[tuple[float, int, str, int]] = []
        self.blackboard_changes: list[dict] = []
        self.snapshots: list[tuple[float, str]] = []  # the statuses after each report
        self.codes: Any = None

    def state(self, state: State, loop: int) -> None:
        self.states.append((self.scheduler.now(), state, loop))

    def statuses(self, changed: Any) -> None:
        self.status_reports.append((self.scheduler.now(), list(changed)))
        if self.codes is not None:
            self.snapshots.append((self.scheduler.now(), self.codes()))

    def detail(self, index: int, kind: str, value: object) -> None:
        self.detail_history.append((self.scheduler.now(), index, kind, value))
        kinds = self.details.setdefault(index, {})
        if value is None:
            kinds.pop(kind, None)
        else:
            kinds[kind] = value

    def blackboard(self, changes: dict) -> None:
        self.blackboard_changes.append(changes)

    def log(self, level: int, text: str, index: int = -1) -> None:
        self.logs.append((self.scheduler.now(), int(level), text, index))

    def lines(self, contains: str = "") -> list[tuple[float, str]]:
        return [(time, text) for time, _, text, _ in self.logs if contains in text]


class EngineRun:
    """One run of a test tree on a fake clock (see the module docs)."""

    def __init__(
        self,
        tree: str,
        *,
        scripts: dict[str, Script] | None = None,
        entries: list[dict] | None = None,
        config: dict | None = None,
        leaves: dict[str, dict] | None = None,
        options: dict[str, dict] | None = None,
        registry: Registry | None = None,
        bundle: dict | None = None,
    ) -> None:
        self.scheduler = FakeScheduler()
        self.sink = Recorder(self.scheduler)
        self.scripts = scripts or {}
        self.blackboard = Blackboard(DEFAULT_ENTRIES if entries is None else entries)
        self.registry = registry if registry is not None else make_registry()
        if bundle is None:
            bundle = TreeBuilder(leaves, options, self.scripts).bundle(tree, {"tick_interval_ms": 1000, **(config or {})})
        self.bundle = bundle
        self.calls: dict[str, list[float]] = {}
        self.events: list[tuple[float, str, str]] = []
        self.results: list[RunResult] = []
        self.sink.codes = self.codes
        self.runner = TreeRunner(bundle, self.blackboard, self.registry, self.scheduler, self.sink,
                                 self.results.append, run_context=self)

    # ----------------------------------------------------------------- what the leaves report
    def count(self, name: str) -> None:
        self.calls.setdefault(name, []).append(self.scheduler.now())

    def event(self, name: str, what: str) -> None:
        self.events.append((self.scheduler.now(), name, what))

    def events_of(self, name: str) -> list[tuple[float, str]]:
        return [(round(time, 6), what) for time, leaf, what in self.events if leaf == name]

    # ----------------------------------------------------------------- driving
    def start(self) -> EngineRun:
        self.runner.start()
        self.scheduler.run_pending()
        return self

    def at(self, when: float) -> EngineRun:
        self.scheduler.advance_to(when)
        return self

    def set(self, name: str, value: Any) -> None:
        """Change an entry from outside the tree, as a browser would."""
        self.blackboard.set(name, value)
        self.scheduler.run_pending()

    # ----------------------------------------------------------------- reading
    def codes(self) -> str:
        return " ".join(LETTERS[Status(value)] for value in self.runner.statuses)

    @property
    def state(self) -> State:
        return self.runner.state

    @property
    def result(self) -> RunResult | None:
        return self.results[-1] if self.results else None

    def index(self, title: str) -> int:
        for position in range(len(self.runner.node_paths)):
            if self.runner.node(position).title == title:
                return position
        raise KeyError(title)

    def details(self, title: str) -> dict[str, object]:
        return self.runner.details().get(self.index(title), {})
