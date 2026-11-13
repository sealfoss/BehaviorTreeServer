"""Regression tests for the findings of the gateway review (numbered as the review numbered them)."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from bt_server_core.files import read_blackboard_file

from bt_server_gateway.client import MAX_QUEUE_BYTES
from bt_server_gateway.link import LinkRun
from bt_server_gateway.settings import SettingsError, settings_from_dict
from gateway_fakes import FakeFactory, FakeLink, GatedFactory
from gateway_helpers import Harness, hexid, make_settings
from gw_test_leaves import Hold, Pick


def write_tree(path, nodes, connections=()) -> None:  # noqa: ANN001
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"format": "behavior_tree_widget", "version": 1, "config": {}, "nodes": nodes,
                                "connections": [{"parent": p, "child": c} for p, c in connections]}))


def root_node(node_id: str) -> dict:
    return {"id": node_id, "type": "Root", "title": "Root", "x": 0, "y": 0, "composite": "Sequence", "memory": True}


async def two_sessions_sharing(start_gateway, tmp_path, sub_nodes, sub_links, zoom: tuple[str, ...] = ("a", "b")):  # noqa: ANN001, ANN201
    """Trees a and b, each a Sub-Tree node running sub; session local has a, session second b."""
    trees = tmp_path / "trees"
    write_tree(trees / "sub_BT.json", sub_nodes, sub_links)
    ids = {}
    for name in ("a", "b"):
        root, sub = hexid(), hexid()
        ids[name] = sub
        write_tree(trees / f"{name}_BT.json", [root_node(root), {"id": sub, "type": "Sub-Tree", "title": "S", "x": 0,
                   "y": 100, "fields": {"File": "sub_BT.json"}}], [(root, sub)])
    gw = await start_gateway(make_settings(trees, executors=["second"]))
    one, two = await gw.browser("local"), await gw.browser("second")
    assert (await one.request("file", action="load", name="a"))["t"] == "ok"
    assert (await two.request("file", action="load", name="b"))["t"] == "ok"
    for browser, name in ((one, "a"), (two, "b")):
        if name in zoom:
            reply = await browser.request("file", action="zoom_in", doc=name, node=ids[name], path=ids[name])
            assert reply["t"] == "ok", reply
    return gw, one, two


# ---------------------------------------------------------------------------------------- 1
@pytest.mark.parametrize("action", ["load", "rename", "save_as"])
async def test_review_01_file_command_and_execute_cannot_both_change_the_tree(start_gateway, settings,
                                                                              action: str) -> None:
    gw = await start_gateway(settings)
    first, second = await gw.browser(), await gw.browser()
    await gw.new_tree(first, "other")
    root = await gw.new_tree(first, "robot")
    await gw.add(first, "robot", "Hold", root, fields={"Seconds": "1.0", "Label": "x"})
    request = {"load": {"action": "load", "name": "other", "choice": "save"},
               "rename": {"action": "rename", "name": "robot", "new_name": "robot2"},
               "save_as": {"action": "save_as", "doc": "robot", "name": "robot2"}}[action]
    m1, m2 = first.mark(), second.mark()
    await first.send(t="file", cid=1001, **request)
    await second.send(t="cmd", cid=1002, name="execute")
    done = await first.wait_for(lambda m: m.get("cid") == 1001 and m["t"] in ("ok", "no"), start=m1)
    execute = await second.wait_for(lambda m: m.get("cid") == 1002 and m["t"] in ("ok", "no"), start=m2)
    assert not (done["t"] == "ok" and execute["t"] == "ok")
    session = gw.session()
    if execute["t"] == "ok":  # the run holds the tree: its structure is locked
        node = {"id": hexid(), "type": "Hold", "title": "late", "x": 0, "y": 0}
        reply = await gw.op(second, session.root, {"op": "node.add", "node": node})
        assert reply["t"] == "no"
    else:
        assert "file command" in execute["reason"]
        assert not session.result_pending


# ---------------------------------------------------------------------------------------- 2
async def test_review_02_field_edit_held_while_starting_reaches_the_executor(start_gateway, settings) -> None:
    Hold.started.clear()
    gw = await start_gateway(settings, GatedFactory(settings))
    first, second = await gw.browser(), await gw.browser()
    root = await gw.new_tree(first, "robot")
    sequence = await gw.add(first, "robot", "Sequence", root, memory=True)
    await gw.add(first, "robot", "Hold", sequence, x=-100, fields={"Seconds": "0.3", "Label": "a"})
    second_hold = await gw.add(first, "robot", "Hold", sequence, x=100, fields={"Seconds": "0.1", "Label": "old"})
    link = gw.session().link
    link.gate.clear()
    start = first.mark()
    await first.send(t="cmd", cid=77, name="execute")
    await gw.until(lambda: gw.session().starting)
    mark = second.mark()
    await second.send(t="op", cid=5, doc="robot", rev=gw.doc("robot").rev,
                      op={"op": "node.set_field", "id": second_hold, "field": "Label", "value": "new"})
    await asyncio.sleep(0.1)
    link.gate.set()
    reply = await second.wait_for(lambda m: m.get("cid") == 5 and m["t"] in ("ok", "no"), start=mark)
    assert reply["t"] == "ok"
    await first.wait_for(lambda m: m["t"] == "run" and m["event"] == "ended", start=start)
    assert Hold.started == ["a", "new"]
    assert gw.doc("robot").fields(second_hold)["Label"] == "new"


# ---------------------------------------------------------------------------------------- 3
async def test_review_03_two_field_edits_of_one_node_during_a_run(gw: Harness) -> None:
    Hold.started.clear()
    browser = await gw.browser()
    root = await gw.new_tree(browser, "robot")
    sequence = await gw.add(browser, "robot", "Sequence", root, memory=True)
    await gw.add(browser, "robot", "Hold", sequence, x=-100, fields={"Seconds": "0.4", "Label": "a"})
    second = await gw.add(browser, "robot", "Hold", sequence, x=100, fields={"Seconds": "0.1", "Label": "old"})
    start = browser.mark()
    assert (await browser.request("cmd", name="execute"))["t"] == "ok"
    await browser.wait_for(lambda m: m["t"] == "st" and m["state"] == 1, start=start)
    assert (await gw.op(browser, "robot", {"op": "node.set_field", "id": second, "field": "Label",
                                           "value": "new"}))["t"] == "ok"
    assert (await gw.op(browser, "robot", {"op": "node.set_field", "id": second, "field": "Seconds",
                                           "value": "0.2"}))["t"] == "ok"
    await browser.wait_for(lambda m: m["t"] == "run" and m["event"] == "ended", start=start)
    fields = gw.doc("robot").fields(second)
    assert Hold.started == ["a", "new"]
    assert fields["Label"] == "new" and fields["Seconds"] == 0.2


# ---------------------------------------------------------------------------------------- 4
async def restart_with_recovery(start_gateway, settings):  # noqa: ANN001, ANN201
    gw = await start_gateway(settings)
    browser = await gw.browser()
    root = await gw.new_tree(browser, "robot")
    await gw.add(browser, "robot", "Touch", root, x=-100)
    assert (await browser.request("file", action="save", doc="robot"))["t"] == "ok"
    changed = await gw.add(browser, "robot", "Print Message", None, x=300, title="Changed",
                           fields={"Level": 4, "Source": None, "Message": ""})
    recovery = settings.trees_dir / ".recovery" / "robot_BT.json"
    await gw.until(lambda: recovery.is_file())
    await browser.close()
    await gw.client.close()
    gw = await start_gateway(settings)
    browser = await gw.browser()
    assert browser.snapshot["recovery"] == ["robot"]
    return gw, browser, changed, recovery


async def test_review_04_recovery_offer_survives_a_run_and_a_save(start_gateway, settings) -> None:
    gw, browser, changed, recovery = await restart_with_recovery(start_gateway, settings)
    start = browser.mark()
    assert (await browser.request("cmd", name="execute"))["t"] == "ok"  # Touch sets its title: a change
    await browser.wait_for(lambda m: m["t"] == "run" and m["event"] == "ended", start=start)
    assert (await browser.request("file", action="save", doc="robot"))["t"] == "ok"
    await asyncio.sleep(1.5)
    assert recovery.is_file()
    reply = await browser.request("file", action="recover", doc="robot", accept=True)
    assert reply["t"] == "ok" and any(node["id"] == changed for node in reply["document"]["nodes"])


async def test_review_04_recovery_offer_survives_an_edit(start_gateway, settings) -> None:
    gw, browser, changed, recovery = await restart_with_recovery(start_gateway, settings)
    document = gw.doc("robot")
    reply = await gw.op(browser, "robot", {"op": "node.move", "positions": {document.root_id: [10, 10]}})
    assert reply["t"] == "ok"
    await asyncio.sleep(1.5)
    reply = await browser.request("file", action="recover", doc="robot", accept=True)
    assert reply["t"] == "ok" and any(node["id"] == changed for node in reply["document"]["nodes"])
    reply = await browser.request("file", action="recover", doc="robot", accept=False)
    assert reply["t"] == "no"  # answered: nothing more to recover


async def test_review_04_declined_offer_is_removed(start_gateway, settings) -> None:
    gw, browser, changed, recovery = await restart_with_recovery(start_gateway, settings)
    assert (await browser.request("file", action="recover", doc="robot", accept=False))["t"] == "ok"
    assert not recovery.exists()
    again = await gw.browser()
    assert again.snapshot["recovery"] == []


# ---------------------------------------------------------------------------------------- 5
async def test_review_05_config_change_while_starting_reaches_the_run(start_gateway, settings) -> None:
    gw = await start_gateway(settings, GatedFactory(settings))
    first, second = await gw.browser(), await gw.browser()
    root = await gw.new_tree(first, "robot")
    await gw.add(first, "robot", "Hold", root, fields={"Seconds": "0.2", "Label": "x"})
    link = gw.session().link
    link.gate.clear()
    start = first.mark()
    await first.send(t="cmd", cid=901, name="execute")
    await gw.until(lambda: gw.session().starting)
    mark = second.mark()
    await second.send(t="op", cid=7, doc="robot", rev=gw.doc("robot").rev,
                      op={"op": "doc.set_config", "config": {"repeat": True}})
    await asyncio.sleep(0.1)
    link.gate.set()
    reply = await second.wait_for(lambda m: m.get("cid") == 7 and m["t"] in ("ok", "no"), start=mark)
    assert reply["t"] == "ok" and gw.doc("robot").config["repeat"] is True
    await asyncio.sleep(1.0)
    assert not [m for m in first.inbox[start:] if m["t"] == "run" and m["event"] == "ended"]
    assert link._runner.config["repeat"] is True
    assert (await first.request("cmd", name="stop"))["t"] == "ok"


# ---------------------------------------------------------------------------------------- 6
async def test_review_06_discard_cannot_revert_a_running_sub_tree(start_gateway, tmp_path) -> None:
    sub_root, hold = hexid(), hexid()
    nodes = [root_node(sub_root), {"id": hold, "type": "Hold", "title": "H", "x": 0, "y": 100,
                                   "fields": {"Seconds": 1.5, "Label": "s"}}]
    gw, one, two = await two_sessions_sharing(start_gateway, tmp_path, nodes, [(sub_root, hold)])
    extra = {"id": hexid(), "type": "Print Message", "title": "unsaved", "x": 200, "y": 100,
             "fields": {"Level": 4, "Source": None, "Message": ""}}
    assert (await gw.op(two, "sub", {"op": "node.add", "node": extra}))["t"] == "ok"
    start = one.mark()
    assert (await one.request("cmd", name="execute"))["t"] == "ok"
    await one.wait_for(lambda m: m["t"] == "st" and m["state"] == 1, start=start)
    (tmp_path / "trees" / "c_BT.json").write_text((tmp_path / "trees" / "a_BT.json").read_text())
    reply = await two.request("file", action="load", name="c", choice="discard")
    assert reply["t"] == "no" and "executing" in reply["reason"]
    assert gw.doc("sub").has_node(extra["id"])
    assert (await one.request("cmd", name="stop"))["t"] == "ok"


# ---------------------------------------------------------------------------------------- 7
async def test_review_07_field_edit_reaches_every_run_of_the_sub_tree(start_gateway, tmp_path) -> None:
    Hold.started.clear()
    sr, sq, h1, h2 = hexid(), hexid(), hexid(), hexid()
    nodes = [root_node(sr), {"id": sq, "type": "Sequence", "title": "Q", "x": 0, "y": 100, "memory": True},
             {"id": h1, "type": "Hold", "title": "H1", "x": -100, "y": 200, "fields": {"Seconds": 0.5, "Label": "first"}},
             {"id": h2, "type": "Hold", "title": "H2", "x": 100, "y": 200, "fields": {"Seconds": 0.1, "Label": "old"}}]
    gw, one, two = await two_sessions_sharing(start_gateway, tmp_path, nodes, [(sr, sq), (sq, h1), (sq, h2)],
                                              zoom=("a",))
    s1, s2 = one.mark(), two.mark()
    assert (await one.request("cmd", name="execute"))["t"] == "ok"
    assert (await two.request("cmd", name="execute"))["t"] == "ok"
    await asyncio.sleep(0.15)
    reply = await gw.op(one, "sub", {"op": "node.set_field", "id": h2, "field": "Label", "value": "new"})
    assert reply["t"] == "ok"
    await one.wait_for(lambda m: m["t"] == "run" and m["event"] == "ended", start=s1)
    await two.wait_for(lambda m: m["t"] == "run" and m["event"] == "ended", start=s2)
    assert Hold.started.count("new") == 2
    assert gw.doc("sub").fields(h2)["Label"] == "new"


# ---------------------------------------------------------------------------------------- 8
async def test_review_08_selection_goes_to_the_executor_and_bindings_wait(start_gateway, tmp_path) -> None:
    Pick.seen.clear()
    gw = await start_gateway(make_settings(tmp_path / "trees"))
    browser = await gw.browser()
    root = await gw.new_tree(browser, "robot")
    sequence = await gw.add(browser, "robot", "Sequence", root, memory=True)
    await gw.add(browser, "robot", "Hold", sequence, x=-100, fields={"Seconds": "0.4", "Label": "a"})
    choose = await gw.add(browser, "robot", "Pick", sequence, x=100)
    start = browser.mark()
    assert (await browser.request("cmd", name="execute"))["t"] == "ok"
    await browser.wait_for(lambda m: m["t"] == "st" and m["state"] == 1, start=start)
    rev = gw.doc("robot").rev
    reply = await gw.op(browser, "robot", {"op": "node.set_selection", "id": choose, "field": "Choice", "index": 1})
    assert reply["t"] == "ok" and reply["rev"] == rev  # applied by the executor first
    for op in ({"op": "node.set_binding", "id": choose, "key": "Seconds", "entry": "x"},
               {"op": "node.set_output", "id": choose, "key": "Result", "entry": "y"},
               {"op": "node.set_option", "id": choose, "name": "cancel_timeout_ms", "value": 100}):
        reply = await gw.op(browser, "robot", op)
        assert reply["t"] == "no" and "between runs" in reply["reason"]
    await browser.wait_for(lambda m: m["t"] == "run" and m["event"] == "ended", start=start)
    assert Pick.seen == ["b"]
    back = [m for m in browser.all("op", start) if m["op"]["op"] == "node.set_selection"]
    assert back and back[0]["by"] == "executor"
    assert gw.doc("robot").file_item(choose)["field_selections"]["Choice"] == 1 and gw.doc("robot").unsaved


# ---------------------------------------------------------------------------------------- 9
async def test_review_09_run_not_started_here_ends_for_browsers(start_gateway, settings) -> None:
    link = FakeLink("local")
    gw = await start_gateway(settings, FakeFactory(link))
    browser, watcher = await gw.browser(), await gw.browser()
    await watcher.send(t="watch", executor="local")
    start, wstart = browser.mark(), watcher.mark()
    link.run = LinkRun(uuid.uuid4().hex, "run_tree", "10.0.0.9", {"root": "x", "trees": {}}, ["a"])
    link.emit(lambda listener: listener.on_run(link.run))
    link.status(1, statuses=b"\x01")
    await browser.wait_for(lambda m: m["t"] == "run" and m["event"] == "started", start=start)
    link.status(0, 1, b"\x02")
    ended = await browser.wait_for(lambda m: m["t"] == "run" and m["event"] == "ended", start=start)
    assert ended["outcome"] == 1 and ended["own"] is False
    watched = await watcher.wait_for(lambda m: m["t"] == "run" and m["event"] == "ended", start=wstart)
    assert watched["bundle"] is not None
    link.status(0, 1, b"\x02")
    await asyncio.sleep(0.1)
    assert len([m for m in browser.all("run", start) if m["event"] == "ended"]) == 1


# ---------------------------------------------------------------------------------------- 10
async def test_review_10_sourced_entry_added_during_a_run_keeps_its_declared_value(start_gateway, settings) -> None:
    link = FakeLink("local")
    gw = await start_gateway(settings, FakeFactory(link))
    browser = await gw.browser()
    await gw.new_tree(browser, "robot")
    assert (await browser.request("cmd", name="execute"))["t"] == "ok"
    entry_id = hexid()
    source = {"topic": "/t", "type": "std_msgs/msg/Int32", "field": "data", "max_rate_hz": 2.0, "max_age_s": 0.0}
    reply = await browser.request("op", doc="@blackboard", rev=0, op={
        "op": "bb.add", "entry": {"id": entry_id, "name": "s", "type": "Integer", "value": 1, "reset": False,
                                  "source": source}})
    assert reply["t"] == "ok"
    session = gw.session()
    for seq, value in ((2, 1), (3, 42)):
        session.on_blackboard(link.run.run_id, seq, False, {"entries": [
            {"id": entry_id, "name": "s", "type": "Integer", "value": value, "reset": False, "source": source,
             "freshness": "ok", "age": 0.0}], "removed": [], "order": None})
    assert session.board.entry(entry_id)["value"] == 42  # shown live
    await asyncio.sleep(0.6)
    items = {item["name"]: item for item in read_blackboard_file(settings.trees_dir / "robot_BB.json")}
    assert items["s"]["value"] == 1  # the file keeps the declared value
    link.finish([{"id": entry_id, "name": "s", "type": "Integer", "value": 42, "reset": False, "source": source}])
    reply = await browser.request("op", doc="@blackboard", rev=0, op={"op": "bb.set_reset", "id": entry_id,
                                                                      "reset": True})
    assert reply["t"] == "ok"
    await asyncio.sleep(0.4)
    items = {item["name"]: item for item in read_blackboard_file(settings.trees_dir / "robot_BB.json")}
    assert items["s"]["value"] == 1 and items["s"]["reset"] is True


# ---------------------------------------------------------------------------------------- 11
class EarlyLink(FakeLink):
    """The run's first (full) blackboard update arrives before the goal's answer, as it may over
    ROS, where ~/blackboard and the action are separate channels."""

    async def execute(self, bundle, blackboard, label, requested_by):  # noqa: ANN001, ANN201
        run_id = await super().execute(bundle, blackboard, label, requested_by)
        entries = [dict(item, value=5 if item["name"] == "x" else item["value"]) for item in blackboard]
        self.emit(lambda listener: listener.on_blackboard(run_id, 1, True, {
            "entries": entries, "removed": [], "order": [entry["id"] for entry in entries]}))
        await asyncio.sleep(0.05)
        return run_id


async def test_review_11_updates_before_the_goal_answer_are_mirrored(start_gateway, settings) -> None:
    link = EarlyLink("local")
    gw = await start_gateway(settings, FakeFactory(link))
    browser = await gw.browser()
    await gw.new_tree(browser, "robot")
    entry = await gw.bb_add(browser, "x", "Integer", 1)
    assert (await browser.request("cmd", name="execute"))["t"] == "ok"
    session = gw.session()
    assert session.board.entry(entry)["value"] == 5
    session.on_blackboard(link.run.run_id, 3, False, {"entries": [], "removed": [], "order": None})
    await asyncio.sleep(0.2)
    assert session.board.entry(entry)["value"] == 5


# ---------------------------------------------------------------------------------------- 12
async def test_review_12_entry_rename_stays_in_its_session(start_gateway, tmp_path) -> None:
    trees = tmp_path / "trees"
    sub_root, printer = hexid(), hexid()
    write_tree(trees / "sub_BT.json", [root_node(sub_root), {"id": printer, "type": "Print Message", "title": "P",
               "x": 0, "y": 100, "fields": {"Level": 4, "Source": "x", "Message": ""}}], [(sub_root, printer)])
    root, sub = hexid(), hexid()
    write_tree(trees / "b_BT.json", [root_node(root), {"id": sub, "type": "Sub-Tree", "title": "S", "x": 0, "y": 100,
               "fields": {"File": "sub_BT.json"}}], [(root, sub)])
    gw = await start_gateway(make_settings(trees, executors=["second"]))
    one, two = await gw.browser("local"), await gw.browser("second")
    own_root = await gw.new_tree(one, "a")
    mine = await gw.add(one, "a", "Print Message", own_root, fields={"Level": 4, "Source": "x", "Message": ""})
    assert (await two.request("file", action="load", name="b"))["t"] == "ok"
    assert (await two.request("file", action="zoom_in", doc="b", node=sub, path=sub))["t"] == "ok"
    entry = await gw.bb_add(one, "x", "Integer", 1)
    reply = await one.request("op", doc="@blackboard", rev=0, op={"op": "bb.rename", "id": entry, "name": "renamed"})
    assert reply["t"] == "ok"
    await asyncio.sleep(0.2)
    assert gw.doc("a").fields(mine)["Source"] == "renamed"
    assert gw.doc("sub").fields(printer)["Source"] == "x" and not gw.doc("sub").unsaved


# ---------------------------------------------------------------------------------------- 13
async def test_review_13_slow_browser_queue_is_bounded_in_bytes(gw: Harness) -> None:
    fast = await gw.browser()
    root = await gw.new_tree(fast, "robot")
    leaf = await gw.add(fast, "robot", "Print Message", root, fields={"Level": 4, "Source": None, "Message": ""})
    slow = await gw.browser()
    slow._reader.cancel()  # reads nothing from now on
    client = gw.gateway.clients[slow.client_id]
    big = "x" * (900 * 1024)
    peak = 0
    for _ in range(60):
        reply = await gw.op(fast, "robot", {"op": "node.set_field", "id": leaf, "field": "Message", "value": big})
        assert reply["t"] == "ok"
        peak = max(peak, sum(len(item) for item in client._queue))
    assert peak <= MAX_QUEUE_BYTES
    assert client._closed and not client._queue  # closed: the page reconnects and gets a snapshot


# ---------------------------------------------------------------------------------------- low
def test_review_low_settings_tls_port_and_placeholders() -> None:
    assert settings_from_dict({"trees_dir": "t", "tls_cert": "c.pem", "tls_key": "k.pem"}).http_port == 443
    assert settings_from_dict({"trees_dir": "t"}).http_port == 80
    for names in (["{host}.local"], ["bt{.local"]):
        with pytest.raises(SettingsError):
            settings_from_dict({"trees_dir": "t", "public_names": names})


async def test_review_low_preview_limit_counts_bytes(gw: Harness) -> None:
    browser = await gw.browser()
    await gw.new_tree(browser, "robot")
    entry = await gw.bb_add(browser, "s", "String", "€" * 60_000)
    start = browser.mark()
    await browser.send(t="attach", executor="local")
    snapshot = await browser.wait_for("snapshot", start=start)
    item = next(e for e in snapshot["blackboard"] if e["id"] == entry)
    assert item["truncated"] and len(item["value"].encode()) <= 64 * 1024
    reply = await browser.request("ask", kind="entry_value", id=entry)
    assert reply["value"] == "€" * 60_000


async def test_review_low_keys_in_the_case_on_disk(gw: Harness, monkeypatch) -> None:
    import bt_server_gateway.gateway as gateway_module

    browser = await gw.browser()
    await gw.new_tree(browser, "robot")
    monkeypatch.setattr(gateway_module, "canonical_key",
                        lambda root, key: "robot" if key.casefold() == "robot" else key)
    assert gw.gateway.tree_key("ROBOT_BT.json") == "robot"
    reply = await browser.request("file", action="save_as", doc="robot", name="ROBOT")
    assert reply["t"] == "ok" and gw.session().root == "robot"
