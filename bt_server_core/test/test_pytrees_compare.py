"""The engine against py_trees (design 17): random trees of Sequence, Selector (Memory on and
off), Negation and Parallel (both policies) with instant leaves, ticked in both; after every tick
every node's status must match, py_trees's INVALID read as Ready.

Each leaf has a random result for each tick, so the trees run several loops (Loop Execution,
one tick per loop). As the widget did, py_trees's Root is stopped before each new loop: its
Selectors do not reset their children on a new round.
"""

from __future__ import annotations

import random
from typing import Any

import py_trees
from engine_helpers import EngineRun
from py_trees.common import Status as PtStatus

from bt_server_core.engine import Status
from bt_server_core.leaves import InstantLeaf, LeafContext
from bt_server_core.registry import Registry

SEED = 20261113
TREES = 300
TICKS = 4

PT_LETTERS = {PtStatus.INVALID: "-", PtStatus.RUNNING: "R", PtStatus.SUCCESS: "S", PtStatus.FAILURE: "F"}
SCRIPTS: dict[str, list[bool]] = {}
CLOCK = {"tick": 0}


class Scripted(InstantLeaf):
    """Returns the result its script gives for the current tick."""

    TYPE_NAME = "Scripted"

    def tick(self, ctx: LeafContext) -> Any:
        return SCRIPTS[ctx.id][CLOCK["tick"]]


class PtScripted(py_trees.behaviour.Behaviour):
    def __init__(self, node_id: str) -> None:
        super().__init__(name=node_id)
        self.node_id = node_id

    def update(self) -> PtStatus:
        return PtStatus.SUCCESS if SCRIPTS[self.node_id][CLOCK["tick"]] else PtStatus.FAILURE


class RandomTree:
    """A random tree, as a bundle for the engine and as a py_trees tree, nodes in pre-order."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.nodes: list[dict] = []
        self.connections: list[dict] = []
        self.behaviours: list[py_trees.behaviour.Behaviour] = []
        root_kind = rng.choice(["Sequence", "Selector"])
        self.root = self._build(root_kind, depth=0, root=True)

    def _new_id(self) -> str:
        return f"{len(self.nodes) + 1:032x}"

    def _build(self, kind: str, depth: int, root: bool = False) -> py_trees.behaviour.Behaviour:
        rng = self.rng
        node_id = self._new_id()
        data: dict = {"id": node_id, "title": node_id}
        self.nodes.append(data)
        position = len(self.behaviours)
        self.behaviours.append(None)  # type: ignore[arg-type]  # filled below, keeps pre-order
        behaviour: py_trees.behaviour.Behaviour
        if kind == "Leaf":
            data["type"] = "Scripted"
            SCRIPTS[node_id] = [rng.random() < 0.6 for _ in range(TICKS)]
            behaviour = PtScripted(node_id)
        else:
            children_kinds = self._children(kind, depth)
            child_behaviours = []
            for child_kind in children_kinds:
                child_id = self._new_id()
                self.connections.append({"parent": node_id, "child": child_id})
                child_behaviours.append(self._build(child_kind, depth + 1))
            if kind in ("Sequence", "Selector"):
                memory = rng.random() < 0.5
                data.update({"type": "Root", "composite": kind} if root else {"type": kind})
                data["memory"] = memory
                cls = py_trees.composites.Sequence if kind == "Sequence" else py_trees.composites.Selector
                behaviour = cls(name=node_id, memory=memory, children=child_behaviours)
            elif kind in ("All", "Any"):
                data.update({"type": "Parallel", "policy": kind.lower()})
                policy = (py_trees.common.ParallelPolicy.SuccessOnAll() if kind == "All"
                          else py_trees.common.ParallelPolicy.SuccessOnOne())
                behaviour = py_trees.composites.Parallel(name=node_id, policy=policy, children=child_behaviours)
            else:
                data["type"] = "Negation"
                behaviour = py_trees.decorators.Inverter(name=node_id, child=child_behaviours[0])
        self.behaviours[position] = behaviour
        return behaviour

    def _children(self, kind: str, depth: int) -> list[str]:
        rng = self.rng
        if kind == "Negation":
            count = 1
        else:
            count = rng.randint(0, 4) if depth < 3 else rng.randint(0, 2)
        kinds = []
        for _ in range(count):
            if depth >= 3 or rng.random() < 0.45:
                kinds.append("Leaf")
            else:
                kinds.append(rng.choice(["Sequence", "Selector", "All", "Any", "Negation"]))
        return kinds

    def bundle(self) -> dict:
        return {"format": "behavior_tree_bundle", "version": 1, "root": "main", "trees": {"main": {
            "config": {"tick_interval_ms": 1000, "repeat": True}, "nodes": self.nodes,
            "connections": self.connections}}}

    def pt_codes(self) -> str:
        return " ".join(PT_LETTERS[behaviour.status] for behaviour in self.behaviours)


def test_random_trees_match_py_trees() -> None:
    rng = random.Random(SEED)
    registry = Registry()
    registry.register(Scripted)
    compared = 0
    for number in range(TREES):
        tree = RandomTree(rng)
        run = EngineRun("", bundle=tree.bundle(), registry=registry, entries=[])
        for tick in range(TICKS):
            CLOCK["tick"] = tick
            if tick == 0:
                run.start()
            else:
                tree.root.stop(PtStatus.INVALID)
                run.at(tick)
            for _ in tree.root.tick():
                pass
            assert run.codes() == tree.pt_codes(), f"tree {number}, tick {tick}: {tree.nodes}"
            assert Status(run.runner.statuses[0]) in (Status.SUCCEEDED, Status.FAILED)
            compared += 1
    assert compared == TREES * TICKS
