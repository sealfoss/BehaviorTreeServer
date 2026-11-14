"""Runs, the blackboard and sessions (design 4.5, 7.2, 7.3, 9.2, 9.5, 9.6): Appendix B.11 G-09,
G-19, G-22, G-23 and B.6 K-01 (the file part), K-05, K-06, K-09, K-10, K-11."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from bt_server_core.files import read_blackboard_file
from bt_server_core.node_types import builtin_catalog
from bt_server_core.values import literal_eval_extended

from bt_server_gateway import session as session_module
from bt_server_gateway.link import NO_RUN
from gateway_helpers import Harness, hexid, make_settings
from gateway_fakes import FakeFactory, FakeLink, GatedFactory


def board_file(gw: Harness, key: str = "robot") -> dict:
    items = read_blackboard_file(gw.settings.trees_dir / f"{key}_BB.json")
    return {item["name"]: item for item in items}


async def run_to_end(gw: Harness, browser, confirm: bool = False) -> dict:
    start = browser.mark()
    reply = await browser.request("cmd", name="execute", confirm=confirm)
    assert reply["t"] == "ok", reply
    return await browser.wait_for(lambda m: m["t"] == "run" and m["event"] == "ended", start=start)


async def test_G_09_reconnect_in_the_middle_of_a_run(gw: Harness) -> None:
    browser = await gw.browser()
    root = await gw.new_tree(browser, "robot")
    sequence = await gw.add(browser, "robot", "Sequence", root, memory=True)
    await gw.bb_add(browser, "count", "Integer", 3)
    await gw.add(browser, "robot", "Chatter", sequence, x=-100, fields={"Lines": 800})
    await gw.add(browser, "robot", "Hold", sequence, x=100, fields={"Seconds": "2.0", "Label": "h"})
    start = browser.mark()
    assert (await browser.request("cmd", name="execute"))["t"] == "ok"
    await browser.wait_for(lambda m: m["t"] == "st" and m["v"].endswith("21"), start=start)
    session = gw.session()
    await gw.until(lambda: len(session.log) > 800)
    await browser.close()
    late = await gw.browser()
    snapshot = late.snapshot
    assert snapshot["root"] == "robot"
    assert snapshot["docs"]["robot"] == gw.doc("robot").to_wire(session.board.types())
    assert snapshot["blackboard"] == session.board.wire_entries()
    assert snapshot["st"]["v"] == "".join(str(byte) for byte in session.status.statuses) == "1121"
    assert snapshot["run"]["own"] is True and snapshot["run"]["node_paths"]
    assert len(snapshot["log"]["lines"]) == 500
    assert snapshot["log"]["lines"][-1] == session.log[-1][1]
    assert snapshot["log"]["first"] == session.log[-500][0]
    before = await late.request("ask", kind="log_before", before=snapshot["log"]["first"], count=100)
    assert before["t"] == "ok" and len(before["lines"]) == 100
    assert before["first"] == snapshot["log"]["first"] - 100


async def test_G_19_a_tree_is_the_tree_of_one_session(start_gateway, tmp_path) -> None:
    gw = await start_gateway(make_settings(tmp_path / "trees", executors=["second"]))
    first = await gw.browser("local")
    second = await gw.browser("second")
    await gw.new_tree(first, "robot")
    reply = await second.request("file", action="load", name="robot")
    assert reply["t"] == "no" and "local" in reply["reason"]
    reply = await second.request("file", action="new", name="robot", replace=True)
    assert reply["t"] == "no" and "local" in reply["reason"]


async def test_configured_executor_comes_first_also_when_discovery_reports_sooner(start_gateway, tmp_path) -> None:
    """A page that has not chosen an executor attaches to the first in the list: that is the
    configured one, also when discovery reports another server's executor (the same ROS domain)
    before start() makes the configured one's session, as RosLinkFactory can."""

    class EagerFactory(FakeFactory):
        async def start(self, on_executors) -> None:  # noqa: ANN001
            on_executors(sorted(self.links))  # "/a_other/..." sorts first ...
            await asyncio.sleep(0)  # ... and its session is made before start() goes on

    own, other = FakeLink("/orion/bt_executor"), FakeLink("/a_other/bt_executor")
    gw = await start_gateway(make_settings(tmp_path / "trees", link="ros", executors=["/orion/bt_executor"]),
                             EagerFactory(own, other))
    assert list(gw.gateway.sessions) == ["/a_other/bt_executor", "/orion/bt_executor"]  # the race, as made
    # the welcome's list, which the page takes its default from
    assert [item["name"] for item in gw.gateway.executor_list()] == ["/orion/bt_executor", "/a_other/bt_executor"]


async def test_G_22_executor_down_tree_still_edited_and_saved(start_gateway, tmp_path) -> None:
    trees = tmp_path / "trees"
    state = trees / ".gateway" / "catalogs"
    state.mkdir(parents=True)
    custom = {"type": "MoveTo", "title": "Move To", "kind": "leaf", "category": "Custom", "builtin": False,
              "fields": [{"name": "Target", "kind": "str", "default": "", "doc": ""}], "leaf_kind": "async",
              "special": None, "waits_for_cancels": True, "needs_ros": False, "doc": ""}
    kept = builtin_catalog() + [custom]
    (state / "kept.json").write_text(json.dumps({"name": "/cell1/bt_executor", "types": kept}), encoding="utf-8")
    link = FakeLink("/cell1/bt_executor", online=False)
    gw = await start_gateway(make_settings(trees, link="ros", executors=["/cell1/bt_executor"]), FakeFactory(link))
    browser = await gw.browser("/cell1/bt_executor")
    assert browser.snapshot["online"] is False
    assert any(item["type"] == "MoveTo" for item in browser.snapshot["catalog"])
    root = await gw.new_tree(browser, "robot")
    move = await gw.add(browser, "robot", "MoveTo", root, fields={"Target": "dock"})
    assert not gw.doc("robot").is_placeholder(move)
    reply = await browser.request("file", action="save", doc="robot")
    assert reply["t"] == "ok"
    saved = json.loads((trees / "robot_BT.json").read_text(encoding="utf-8"))
    assert any(node["type"] == "MoveTo" and node["fields"]["Target"] == "dock" for node in saved["nodes"])
    reply = await browser.request("cmd", name="execute")
    assert reply["t"] == "no" and "cannot be reached" in reply["reason"]


async def test_G_23_execute_with_findings_needs_confirm(gw: Harness) -> None:
    browser = await gw.browser()
    root = await gw.new_tree(browser, "robot")
    negation = await gw.add(browser, "robot", "Negation", root)
    reply = await browser.request("cmd", name="execute")
    assert reply["t"] == "no"
    assert reply["findings"] == [{"path": f"tree:robot/{negation}", "severity": "error",
                                  "text": "the Negation has no child"}]
    assert gw.session().status.state == 0 and not gw.session().starting
    ended = await run_to_end(gw, browser, confirm=True)
    assert ended["outcome"] == 2  # the Negation without a child fails


async def test_K_01_resets_file_part(gw: Harness) -> None:
    browser = await gw.browser()
    root = await gw.new_tree(browser, "robot")
    sequence = await gw.add(browser, "robot", "Sequence", root, memory=True)
    entry = await gw.bb_add(browser, "n", "Integer", 0)
    reply = await browser.request("op", doc="@blackboard", rev=0, op={"op": "bb.set_reset", "id": entry, "reset": True})
    assert reply["t"] == "ok"
    await gw.add(browser, "robot", "Set", sequence, x=-100, fields={
        "ValueName": "n", "CompareTo": None, "LiteralValue": {"type": "Integer", "value": 5}})
    await gw.add(browser, "robot", "Hold", sequence, x=100, fields={"Seconds": "0.8", "Label": "A"})
    start = browser.mark()
    assert (await browser.request("cmd", name="execute"))["t"] == "ok"
    board = gw.session().board
    await gw.until(lambda: board.entry(entry) is not None and board.entry(entry)["value"] == 5)
    await gw.until(lambda: board_file(gw)["n"]["value"] == 5)
    await browser.wait_for(lambda m: m["t"] == "run" and m["event"] == "ended", start=start)
    assert gw.session().board.entry(entry)["value"] == 0
    await gw.until(lambda: board_file(gw)["n"]["value"] == 0)
    await browser.wait_for(lambda m: m["t"] == "bb" and any(e["id"] == entry and e["value"] == 0 for e in m["entries"]),
                           start=start)


async def test_K_05_edit_after_the_end_of_the_run_is_applied_here(start_gateway, settings) -> None:
    link = FakeLink("local")
    gw = await start_gateway(settings, FakeFactory(link))
    browser = await gw.browser()
    root = await gw.new_tree(browser, "robot")
    await gw.add(browser, "robot", "Stop", root)
    entry = await gw.bb_add(browser, "x", "Integer", 1)
    assert (await browser.request("cmd", name="execute"))["t"] == "ok"
    link.apply_answer = (NO_RUN, "no run is active")
    start = browser.mark()
    await browser.send(t="op", cid=50, doc="@blackboard", rev=0, op={"op": "bb.set_value", "id": entry, "value": 7})
    await gw.until(lambda: link.applied)
    assert not [m for m in browser.inbox[start:] if m.get("cid") == 50]  # held for the result
    link.finish([{"id": entry, "name": "x", "type": "Integer", "value": 1, "reset": False, "source": None}])
    reply = await browser.wait_for(lambda m: m.get("cid") == 50, start=start)
    assert reply["t"] == "ok"
    assert gw.session().board.entry(entry)["value"] == 7
    await gw.until(lambda: board_file(gw)["x"]["value"] == 7)


async def test_K_06_edit_between_execute_and_acceptance_is_held_and_sent(start_gateway, settings) -> None:
    gw = await start_gateway(settings, GatedFactory(settings))
    first, second = await gw.browser(), await gw.browser()
    root = await gw.new_tree(first, "robot")
    await gw.add(first, "robot", "Hold", root, fields={"Seconds": "0.5", "Label": "x"})
    entry = await gw.bb_add(first, "x", "Integer", 1)
    link = gw.session().link
    link.gate.clear()
    start = first.mark()
    await first.send(t="cmd", cid=77, name="execute")
    await gw.until(lambda: gw.session().starting)
    mark = second.mark()
    await second.send(t="op", cid=5, doc="@blackboard", rev=0, op={"op": "bb.set_value", "id": entry, "value": 7})
    await asyncio.sleep(0.1)
    assert not [m for m in second.inbox[mark:] if m.get("cid") == 5]  # held back
    assert gw.session().board.entry(entry)["value"] == 1
    link.gate.set()
    reply = await second.wait_for(lambda m: m.get("cid") == 5, start=mark)
    assert reply["t"] == "ok"
    assert link._runner.blackboard.get("x") == 7  # sent to the executor
    await gw.until(lambda: gw.session().board.entry(entry)["value"] == 7)  # and back in the mirror
    assert (await first.wait_for(lambda m: m.get("cid") == 77, start=start))["t"] == "ok"
    await first.wait_for(lambda m: m["t"] == "run" and m["event"] == "ended", start=start)
    assert gw.session().board.entry(entry)["value"] == 7


async def test_K_09_long_values_are_previews_in_the_browser(gw: Harness) -> None:
    browser = await gw.browser()
    root = await gw.new_tree(browser, "robot")
    entry = await gw.bb_add(browser, "big", "List", "[]")
    await gw.add(browser, "robot", "BigList", root, fields={"Count": 100_000})
    start = browser.mark()
    await run_to_end(gw, browser)
    await gw.until(lambda: board_file(gw)["big"]["value"] == list(range(100_000)))
    message = await browser.wait_for(
        lambda m: m["t"] == "bb" and any(e["id"] == entry and e["value"] != "[]" for e in m["entries"]), start=start)
    preview = next(e for e in message["entries"] if e["id"] == entry)
    assert preview["truncated"] is True and len(preview["value"]) == 64 * 1024
    reply = await browser.request("ask", kind="entry_value", id=entry)
    assert reply["t"] == "ok" and literal_eval_extended(reply["value"]) == list(range(100_000))


async def test_K_10_file_written_at_most_every_250_ms(gw: Harness, monkeypatch) -> None:
    browser = await gw.browser()
    await gw.new_tree(browser, "robot")
    entry = await gw.bb_add(browser, "v", "Integer", 0)
    session = gw.session()
    await gw.until(lambda: not session.board_writer.pending)
    await asyncio.sleep(0.3)
    writes: list[tuple[float, dict]] = []
    real = session_module.write_json

    def counting(path, data):  # noqa: ANN001
        writes.append((time.monotonic(), data))
        real(path, data)

    monkeypatch.setattr(session_module, "write_json", counting)
    begin = time.monotonic()
    for value in range(1, 101):
        session._apply_bb_here({"op": "bb.set_value", "id": entry, "value": value})
        while time.monotonic() < begin + value * 0.001:
            await asyncio.sleep(0)
    assert time.monotonic() - begin < 0.25
    await asyncio.sleep(0.6)
    assert len(writes) == 2
    assert writes[0][0] - begin < 0.05
    assert writes[1][0] - writes[0][0] == pytest.approx(0.25, abs=0.04)
    assert board_file(gw)["v"]["value"] == 100


async def test_K_11_interrupted_write_leaves_the_old_file_whole(gw: Harness, monkeypatch) -> None:
    browser = await gw.browser()
    await gw.new_tree(browser, "robot")
    entry = await gw.bb_add(browser, "v", "Integer", 1)
    session = gw.session()
    await gw.until(lambda: board_file(gw).get("v", {}).get("value") == 1)
    await asyncio.sleep(0.3)
    import bt_server_core.files as files_module

    real_replace = files_module.os.replace

    def killed(source, target):  # noqa: ANN001
        raise RuntimeError("killed in the middle of the write")

    monkeypatch.setattr(files_module.os, "replace", killed)
    start = browser.mark()
    reply = await browser.request("op", doc="@blackboard", rev=0, op={"op": "bb.set_value", "id": entry, "value": 2})
    assert reply["t"] == "ok"
    notice = await browser.wait_for(lambda m: m["t"] == "notice" and "blackboard" in m["text"], start=start)
    assert notice["level"] == "error"
    folder = gw.settings.trees_dir
    assert board_file(gw)["v"]["value"] == 1  # the old file, whole
    assert not [path for path in folder.iterdir() if path.name.startswith(".robot_BB") or path.suffix == ".tmp"]
    await browser.request("op", doc="@blackboard", rev=0, op={"op": "bb.set_value", "id": entry, "value": 3})
    await asyncio.sleep(0.3)
    assert len([m for m in browser.all("notice", start) if "blackboard" in m["text"]]) == 1  # told once
    monkeypatch.setattr(files_module.os, "replace", real_replace)
    await browser.request("op", doc="@blackboard", rev=0, op={"op": "bb.set_value", "id": entry, "value": 4})
    await gw.until(lambda: board_file(gw)["v"]["value"] == 4)
    # The file is in place when the write's thread ends; its result reaches the session a moment
    # later, on the event loop (which a slower machine, a Jetson, shows).
    await gw.until(lambda: not session._board_failed)


async def test_reset_after_the_run_restores_the_blackboard(gw: Harness) -> None:
    browser = await gw.browser()
    root = await gw.new_tree(browser, "robot")
    reply = await gw.op(browser, "robot", {"op": "doc.set_config", "config": {"restore_blackboard": True}})
    assert reply["t"] == "ok"
    entry = await gw.bb_add(browser, "m", "Integer", 1)
    await gw.add(browser, "robot", "Set", root, fields={
        "ValueName": "m", "CompareTo": None, "LiteralValue": {"type": "Integer", "value": 9}})
    await run_to_end(gw, browser)
    assert gw.session().board.entry(entry)["value"] == 9
    assert (await browser.request("cmd", name="reset"))["t"] == "ok"
    assert gw.session().board.entry(entry)["value"] == 1
    await gw.until(lambda: board_file(gw)["m"]["value"] == 1)


async def test_rename_during_run_and_documents_follow(gw: Harness) -> None:
    browser = await gw.browser()
    root = await gw.new_tree(browser, "robot")
    sequence = await gw.add(browser, "robot", "Sequence", root, memory=True)
    entry = await gw.bb_add(browser, "m", "Integer", 1)
    evaluation = await gw.add(browser, "robot", "Evaluation", sequence, x=-100, fields={
        "ValueName": "m", "CompareTo": None, "LiteralValue": {"type": "Integer", "value": 1},
        "Operator": "==", "Wait": False, "WaitTimeout": "0.0"})
    await gw.add(browser, "robot", "Hold", sequence, x=100, fields={"Seconds": "0.6", "Label": "r"})
    start = browser.mark()
    assert (await browser.request("cmd", name="execute"))["t"] == "ok"
    await browser.wait_for(lambda m: m["t"] == "st" and m["state"] == 1, start=start)
    reply = await browser.request("op", doc="@blackboard", rev=0, op={"op": "bb.rename", "id": entry, "name": "k"})
    assert reply["t"] == "ok"
    await gw.until(lambda: gw.doc("robot").fields(evaluation)["ValueName"] == "k")
    assert gw.session().board.entry(entry)["name"] == "k"
    await browser.wait_for(lambda m: m["t"] == "run" and m["event"] == "ended", start=start)
    assert gw.session().board.entry(entry)["name"] == "k"
    assert board_file(gw)["k"]["value"] == 1


async def test_leaf_written_field_and_title_are_not_unsaved(gw: Harness) -> None:
    browser = await gw.browser()
    root = await gw.new_tree(browser, "robot")
    touch = await gw.add(browser, "robot", "Touch", root, fields={"Note": ""})
    assert (await browser.request("file", action="save", doc="robot"))["t"] == "ok"
    start = browser.mark()
    await run_to_end(gw, browser)
    await gw.until(lambda: gw.doc("robot").fields(touch)["Note"] == "touched")
    assert gw.doc("robot").title(touch) == "Touched"
    assert not gw.doc("robot").unsaved
    ops = [m for m in browser.all("op", start) if m["by"] == "executor"]
    assert {m["op"]["op"] for m in ops} == {"node.set_field", "node.set_title"}


async def test_runs_not_started_here_are_read_only_and_stopped_by_id(gw: Harness) -> None:
    browser = await gw.browser()
    await gw.new_tree(browser, "robot")
    link = gw.session().link
    bundle = {"format": "behavior_tree_bundle", "version": 1, "root": "t", "trees": {"t": {
        "config": {}, "connections": [{"parent": "r" * 32, "child": "h" * 32}],
        "nodes": [{"id": "r" * 32, "type": "Root", "title": "Root", "x": 0, "y": 0, "composite": "Sequence",
                   "memory": True},
                  {"id": "h" * 32, "type": "Hold", "title": "Hold", "x": 0, "y": 100,
                   "fields": {"Seconds": 5.0, "Label": "cli"}}]}}}
    start = browser.mark()
    run_id = await link.execute(bundle, [], "t", "command line")
    run = await browser.wait_for(lambda m: m["t"] == "run" and m["event"] == "started", start=start)
    assert run["own"] is False and run["bundle"]["root"] == "t" and run["requested_by"] == "command line"
    await browser.wait_for(lambda m: m["t"] == "st" and m["state"] == 1, start=start)
    assert (await browser.request("cmd", name="pause"))["t"] == "no"
    reply = await browser.request("cmd", name="execute")
    assert reply["t"] == "no" and "command line" in reply["reason"]
    root_doc = gw.doc("robot")
    assert root_doc.rev == gw.doc("robot").rev and not gw.session().result_pending
    assert (await browser.request("cmd", name="stop"))["t"] == "ok"
    await browser.wait_for(lambda m: m["t"] == "st" and m["state"] == 0 and m["run_id"] == run_id, start=start)


async def test_watch_another_executor(start_gateway, tmp_path) -> None:
    gw = await start_gateway(make_settings(tmp_path / "trees", executors=["second"]))
    runner, watcher = await gw.browser("second"), await gw.browser("local")
    root = await gw.new_tree(runner, "remote")
    await gw.add(runner, "remote", "Hold", root, fields={"Seconds": "0.5", "Label": "w"})
    start = watcher.mark()
    await watcher.send(t="watch", executor="second")
    assert (await runner.request("cmd", name="execute"))["t"] == "ok"
    run = await watcher.wait_for(lambda m: m["t"] == "run" and m["exec"] == "second", start=start)
    assert run["bundle"]["root"] == "remote"
    await watcher.wait_for(lambda m: m["t"] == "st" and m["exec"] == "second" and m["state"] == 1, start=start)
    mark = watcher.mark()
    await watcher.send(t="watch", executor="second")
    again = await watcher.wait_for(lambda m: m["t"] == "run" and m["exec"] == "second", start=mark)
    assert again["bundle"] is not None
    await watcher.wait_for(lambda m: m["t"] == "st" and m["exec"] == "second", start=mark)


async def test_remote_sub_tree_findings_are_merged(start_gateway, settings) -> None:
    ros_catalog = builtin_catalog()
    here, there = FakeLink("/a/bt_executor", catalog=ros_catalog), FakeLink("/b/bt_executor", catalog=ros_catalog)
    there.findings = [{"path": "tree:dock/x", "severity": "warning", "text": "checked over there"}]
    gw = await start_gateway(make_settings(settings.trees_dir, link="ros"), FakeFactory(here, there))
    browser = await gw.browser("/a/bt_executor")
    root = await gw.new_tree(browser, "robot")
    await gw.new_tree(browser, "dock")
    await browser.request("file", action="load", name="robot")
    await gw.add(browser, "robot", "RemoteSubTree", root, fields={
        "Executor": "/b/bt_executor", "File": "dock_BT.json", "Timeout": "5.0", "Watchdog": "5.0"})
    reply = await browser.request("cmd", name="execute")
    assert reply["t"] == "no"
    assert {"path": "tree:dock/x", "severity": "warning", "text": "checked over there",
            "executor": "/b/bt_executor"} in reply["findings"]


async def test_download_log_of_a_run(gw: Harness) -> None:
    browser = await gw.browser()
    root = await gw.new_tree(browser, "robot")
    await gw.add(browser, "robot", "Chatter", root, fields={"Lines": 20})
    ended = await run_to_end(gw, browser)
    response = await gw.client.get(f"/download-log/local/{ended['run_id']}")
    assert response.status == 200
    text = await response.text()
    assert "chatter 19" in text and "Execution started" in text


async def test_own_run_recognised_after_a_gateway_restart(start_gateway, settings) -> None:
    link = FakeLink("local")
    gw = await start_gateway(settings, FakeFactory(link))
    browser = await gw.browser()
    root = await gw.new_tree(browser, "robot")
    await gw.add(browser, "robot", "Stop", root)
    entry = await gw.bb_add(browser, "x", "Integer", 1)
    reply = await browser.request("cmd", name="execute")
    assert reply["t"] == "ok"
    run_id = reply["run_id"]
    await gw.until(lambda: (settings.state_dir / "sessions.json").is_file()
                   and json.loads((settings.state_dir / "sessions.json").read_text())["local"]["run_id"] == run_id)
    await browser.close()
    await gw.client.close()

    gw = await start_gateway(settings, FakeFactory(link))
    browser = await gw.browser()
    assert browser.snapshot["root"] == "robot"
    assert browser.snapshot["run"]["run_id"] == run_id and browser.snapshot["run"]["own"] is True
    assert gw.session().result_pending
    start = browser.mark()
    link.finish([{"id": entry, "name": "x", "type": "Integer", "value": 5, "reset": False, "source": None}])
    await browser.wait_for(lambda m: m["t"] == "run" and m["event"] == "ended", start=start)
    assert gw.session().board.entry(entry)["value"] == 5
    await gw.until(lambda: board_file(gw)["x"]["value"] == 5)


async def test_executor_lost_when_the_status_stops(start_gateway, settings) -> None:
    link = FakeLink("local")
    gw = await start_gateway(settings, FakeFactory(link))
    browser = await gw.browser()
    root = await gw.new_tree(browser, "robot")
    await gw.add(browser, "robot", "Stop", root)
    start = browser.mark()
    assert (await browser.request("cmd", name="execute"))["t"] == "ok"
    notice = await browser.wait_for(lambda m: m["t"] == "notice" and "lost" in m["text"], start=start, timeout=4)
    assert notice["level"] == "error"


async def test_questions_and_the_kept_catalog(gw: Harness) -> None:
    browser = await gw.browser()
    reply = await browser.request("ask", kind="graph")
    assert reply == {"t": "ok", "cid": reply["cid"], "graph": {"actions": [], "services": [], "topics": []}}
    reply = await browser.request("ask", kind="interface", type="std_msgs/msg/Bool")
    assert reply["t"] == "no" and "ROS" in reply["reason"]
    reply = await browser.request("ask", kind="executors")
    assert reply["executors"] == [{"name": "local", "online": True, "state": 0}]
    reply = await browser.request("ask", kind="entry_value", id=hexid())
    assert reply["t"] == "no"
    folder = gw.settings.state_dir / "catalogs"
    await gw.until(lambda: any(folder.glob("*.json")))
    kept = json.loads(next(folder.glob("*.json")).read_text(encoding="utf-8"))
    assert kept["name"] == "local" and any(item["type"] == "Hold" for item in kept["types"])


def test_entry_problem_from_the_executor_reaches_the_browser() -> None:
    from bt_server_gateway.board import GatewayBoard

    board = GatewayBoard()
    entry = {"id": "e" * 32, "name": "battery", "type": "Double", "value": 0.5, "reset": False,
             "source": {"topic": "/b", "type": "std_msgs/msg/Float64", "field": "data", "max_rate_hz": 2.0,
                        "max_age_s": 0.0}, "freshness": "nodata", "age": None, "problem": "no such topic type"}
    board.apply_update({"entries": [entry], "removed": [], "order": None})
    wire = board.wire_entry("e" * 32)
    assert wire["problem"] == "no such topic type" and wire["freshness"] == "nodata"
    board.apply_update({"entries": [{**entry, "problem": None, "freshness": "ok", "age": 0.1}]})
    wire = board.wire_entry("e" * 32)
    assert wire["problem"] is None and wire["freshness"] == "ok" and wire["value"] == "0.5"
