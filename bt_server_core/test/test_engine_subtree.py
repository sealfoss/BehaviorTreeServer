"""Sub-Tree nodes expanded into the run (design 6.7): node indices and instance paths
(contract 2.2), labels in the log, and the nodes that fail when executed."""

from __future__ import annotations

import asyncio

from engine_helpers import EngineRun

from bt_server_core.engine import Outcome, State
from bt_server_core.scheduler import AsyncioScheduler


def node(node_id: str, type_name: str, title: str | None = None, **extra: object) -> dict:
    return {"id": node_id, "type": type_name, "title": title or type_name, **extra}


def tree(nodes: list[dict], connections: list[tuple[str, str]], config: dict | None = None) -> dict:
    return {"config": config or {"tick_interval_ms": 1000}, "nodes": nodes,
            "connections": [{"parent": parent, "child": child} for parent, child in connections]}


def bundle(trees: dict[str, dict], root: str = "robot") -> dict:
    return {"format": "behavior_tree_bundle", "version": 1, "root": root, "trees": trees}


def patrol() -> dict:
    return tree([node("r2", "Root", composite="Sequence", memory=True), node("s2", "TestS", "Check"),
                 node("n2", "TestN", "Count")], [("r2", "s2"), ("r2", "n2")])


def test_sub_trees_are_expanded_in_pre_order() -> None:
    robot = tree([
        node("r1", "Root", composite="Sequence", memory=True),
        node("p1", "Sub-Tree", "patrol", fields={"File": "cells/patrol_BT.json"}, tree="cells/patrol"),
        node("p2", "Sub-Tree", "again", fields={"File": "cells/patrol_BT.json"}, tree="cells/patrol"),
        node("s1", "TestS", "Last"),
        node("note", "Notes", text="ignored"),
    ], [("r1", "p1"), ("r1", "p2"), ("r1", "s1")])
    run = EngineRun("", bundle=bundle({"robot": robot, "cells/patrol": patrol()})).start()
    assert run.runner.node_paths == ["r1", "p1", "p1/r2", "p1/s2", "p1/n2", "p2", "p2/r2", "p2/s2", "p2/n2", "s1"]
    assert run.codes() == "S S S S S S S S S S"
    assert run.result.outcome == Outcome.SUCCEEDED
    assert (0, "Node 'patrol' > 'Check' (TestS) executed: Succeeded") in run.sink.lines()
    assert run.calls["Count"] == [0, 0]


def test_sub_tree_failures() -> None:
    robot = tree([
        node("r1", "Root", composite="Selector", memory=True),
        node("a", "Sub-Tree", "no file", fields={"File": ""}),
        node("b", "Sub-Tree", "missing", fields={"File": "gone_BT.json"}, tree="gone"),
        node("c", "Sub-Tree", "loop", fields={"File": "loop_BT.json"}, tree="loop"),
    ], [("r1", "a"), ("r1", "b"), ("r1", "c")])
    loop = tree([node("r3", "Root", composite="Sequence"),
                 node("c2", "Sub-Tree", "inner", fields={"File": "loop_BT.json"}, tree="loop")], [("r3", "c2")])
    run = EngineRun("", bundle=bundle({"robot": robot, "loop": loop})).start()
    assert run.runner.node_paths == ["r1", "a", "b", "c", "c/r3", "c/c2"]
    assert run.codes() == "F F F F F F"
    assert "no sub-tree file" in run.details("no file")["error"]
    assert "missing" in run.details("missing")["error"]
    assert "contains this Sub-Tree node itself" in run.details("inner")["error"]


def test_commands_in_a_sub_tree_act_on_the_whole_tree() -> None:
    robot = tree([node("r1", "Root", composite="Sequence"), node("p", "Sub-Tree", "p", fields={"File": "x"}, tree="x"),
                  node("s", "TestN", "after")], [("r1", "p"), ("r1", "s")])
    inner = tree([node("r", "Root", composite="Sequence"), node("stop", "Stop")], [("r", "stop")])
    run = EngineRun("", bundle=bundle({"robot": robot, "x": inner})).start()
    assert run.state == State.IDLE and run.result.outcome == Outcome.STOPPED
    assert "after" not in run.calls


def test_unreadable_sub_tree_document_fails_only_its_node() -> None:
    robot = tree([node("r1", "Root", composite="Sequence"), node("p", "Sub-Tree", "p", fields={"File": "x"}, tree="x")],
                 [("r1", "p")])
    run = EngineRun("", bundle=bundle({"robot": robot, "x": "not a tree"})).start()
    assert run.codes() == "F F"


def test_bundle_as_json_text() -> None:
    import json

    robot = tree([node("r1", "Root", composite="Sequence"), node("s", "TestS")], [("r1", "s")])
    run = EngineRun("", bundle=json.dumps(bundle({"robot": robot}))).start()  # type: ignore[arg-type]
    assert run.codes() == "S S"


def test_asyncio_scheduler_runs_a_tree() -> None:
    from bt_server_core.blackboard import Blackboard
    from bt_server_core.engine import TreeRunner
    from engine_helpers import Recorder, make_registry

    async def main() -> object:
        scheduler = AsyncioScheduler()
        done: asyncio.Future = asyncio.get_running_loop().create_future()
        robot = tree([node("r1", "Root", composite="Sequence"), node("w", "Evaluation", "W", fields={
            "ValueName": "battery", "CompareTo": None, "LiteralValue": 20.0, "Operator": ">=", "Wait": True})],
            [("r1", "w")], {"tick_interval_ms": 20})
        board = Blackboard([{"name": "battery", "type": "Double", "value": 10.0}])
        runner = TreeRunner(bundle({"robot": robot}), board, make_registry(), scheduler, Recorder(scheduler),  # type: ignore[arg-type]
                            done.set_result)
        runner.start()
        await asyncio.sleep(0.05)
        board.set("battery", 30.0)
        return await asyncio.wait_for(done, 2)

    result = asyncio.run(main())
    assert result.outcome == Outcome.SUCCEEDED  # type: ignore[attr-defined]
