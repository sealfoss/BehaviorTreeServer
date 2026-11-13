"""Edit operations (design 9.3, contract 5.3): Appendix B.11 G-01 to G-08, G-17, G-21."""

from __future__ import annotations

from gateway_helpers import Harness, hexid
from gateway_fakes import GatedFactory, GatedLink
from gw_test_leaves import Hold


async def _running(gw: Harness, browser, seconds: str = "1.0", label: str = "a") -> tuple[str, str]:
    """A tree Root -> Hold, executing; returns (root id, hold id)."""
    root = await gw.new_tree(browser, "robot")
    hold = await gw.add(browser, "robot", "Hold", root, fields={"Seconds": seconds, "Label": label})
    start = browser.mark()
    assert (await browser.request("cmd", name="execute"))["t"] == "ok"
    await browser.wait_for(lambda m: m["t"] == "st" and m["state"] == 1, start=start)
    return root, hold


async def test_G_01_conn_add_that_makes_a_cycle_is_refused(gw: Harness) -> None:
    browser = await gw.browser()
    root = await gw.new_tree(browser, "robot")
    upper = await gw.add(browser, "robot", "Sequence", root, memory=True)
    lower = await gw.add(browser, "robot", "Selector", upper, memory=True)
    document = gw.doc("robot")
    rev, before = document.rev, document.to_file()
    reply = await gw.op(browser, "robot", {"op": "conn.add", "parent": lower, "child": upper})
    assert reply["t"] == "no" and "cycle" in reply["reason"].lower()
    assert document.rev == rev and document.to_file() == before


async def test_G_02_second_child_of_a_negation_replaces_the_first(gw: Harness) -> None:
    browser = await gw.browser()
    root = await gw.new_tree(browser, "robot")
    negation = await gw.add(browser, "robot", "Negation", root)
    first = await gw.add(browser, "robot", "Stop", negation)
    second = await gw.add(browser, "robot", "Pause")
    reply = await gw.op(browser, "robot", {"op": "conn.add", "parent": negation, "child": second})
    assert reply["t"] == "ok"
    assert gw.doc("robot").children(negation) == [second]
    assert gw.doc("robot").parent(first) is None


async def test_G_03_the_root_cannot_be_removed(gw: Harness) -> None:
    browser = await gw.browser()
    root = await gw.new_tree(browser, "robot")
    reply = await gw.op(browser, "robot", {"op": "node.remove", "ids": [root]})
    assert reply["t"] == "no"
    assert gw.doc("robot").has_node(root)


async def test_G_04_node_add_refused_and_node_move_applied_during_a_run(gw: Harness) -> None:
    browser = await gw.browser()
    _, hold = await _running(gw, browser)
    node = {"id": hexid(), "type": "Stop", "title": "Stop", "x": 0, "y": 0}
    reply = await gw.op(browser, "robot", {"op": "node.add", "node": node})
    assert reply["t"] == "no" and "executing" in reply["reason"]
    reply = await gw.op(browser, "robot", {"op": "node.move", "positions": {hold: [40.0, 300.0]}})
    assert reply["t"] == "ok"
    assert gw.doc("robot").position(hold) == (40.0, 300.0)


async def test_G_05_field_edit_during_a_run_comes_back_from_the_executor(gw: Harness) -> None:
    Hold.started.clear()
    browser = await gw.browser()
    root = await gw.new_tree(browser, "robot")
    sequence = await gw.add(browser, "robot", "Sequence", root, memory=True)
    await gw.add(browser, "robot", "Hold", sequence, x=-100, fields={"Seconds": "0.5", "Label": "a"})
    second = await gw.add(browser, "robot", "Hold", sequence, x=100, fields={"Seconds": "0.1", "Label": "old"})
    assert (await browser.request("file", action="save", doc="robot"))["t"] == "ok"
    start = browser.mark()
    assert (await browser.request("cmd", name="execute"))["t"] == "ok"
    await browser.wait_for(lambda m: m["t"] == "st" and m["state"] == 1, start=start)
    rev = gw.doc("robot").rev
    edit = browser.mark()
    reply = await gw.op(browser, "robot", {"op": "node.set_field", "id": second, "field": "Label", "value": "new"})
    assert reply["t"] == "ok" and reply["rev"] == rev  # applied by the executor, not (yet) here
    back = await browser.wait_for(lambda m: m["t"] == "op" and m["op"]["op"] == "node.set_field", start=edit)
    assert back["by"] == "executor" and back["op"]["value"] == "new"
    assert not [m for m in browser.all("op", edit) if m["by"] == browser.client_id]
    assert gw.doc("robot").fields(second)["Label"] == "new"
    assert gw.doc("robot").unsaved  # a browser's edit is an unsaved change
    await browser.wait_for(lambda m: m["t"] == "run" and m["event"] == "ended", start=start)
    assert Hold.started == ["a", "new"]  # the leaf that started afterwards used the new value


async def test_G_06_last_write_wins(gw: Harness) -> None:
    first, second = await gw.browser(), await gw.browser()
    root = await gw.new_tree(first, "robot")
    await second.wait_for(lambda m: m["t"] == "snapshot" and m["root"] == "robot")
    leaf = await gw.add(first, "robot", "Print Message", root, fields={"Level": 4, "Source": None, "Message": ""})
    rev = gw.doc("robot").rev
    marks = first.mark(), second.mark()
    reply_b = await gw.op(second, "robot", {"op": "node.set_field", "id": leaf, "field": "Level", "value": 2}, rev)
    reply_a = await gw.op(first, "robot", {"op": "node.set_field", "id": leaf, "field": "Level", "value": 1}, rev)
    assert reply_a["t"] == reply_b["t"] == "ok"
    assert gw.doc("robot").fields(leaf)["Level"] == 1
    assert gw.doc("robot").rev == rev + 2
    for browser, mark in zip((first, second), marks):
        await browser.wait_for(lambda m: m["t"] == "op" and m["rev"] == rev + 2, start=mark)
        values = [m["op"]["value"] for m in browser.all("op", mark)]
        assert values == [2, 1]  # every browser applies both, in order, and shows 1


async def test_G_07_stale_child_order_is_dropped_and_asked_for_again(gw: Harness) -> None:
    first, second = await gw.browser(), await gw.browser()
    root = await gw.new_tree(first, "robot")
    parent = await gw.add(first, "robot", "Sequence", root, memory=True)
    x = await gw.add(first, "robot", "Stop", parent, x=-200, y=200)
    y = await gw.add(first, "robot", "Pause", parent, x=0, y=200)
    z = await gw.add(first, "robot", "Restart", parent, x=200, y=200)
    rev = gw.doc("robot").rev
    reply_a = await gw.op(first, "robot", {"op": "node.move", "positions": {x: [400.0, 200.0]},
                                           "orders": {parent: [y, z, x]}}, rev)
    reply_b = await gw.op(second, "robot", {"op": "node.move", "positions": {z: [-400.0, 200.0]},
                                            "orders": {parent: [z, x, y]}}, rev)
    assert reply_a["t"] == "ok" and "reorder" not in reply_a
    assert reply_b["t"] == "ok" and reply_b["reorder"] == [parent]
    assert gw.doc("robot").children(parent) == [y, z, x]
    # B works the order out again from the positions it now has, at the current revision
    document = gw.doc("robot")
    order = sorted(document.children(parent), key=lambda node: document.position(node)[0])
    reply = await gw.op(second, "robot", {"op": "node.move", "positions": {}, "orders": {parent: order}})
    assert reply["t"] == "ok" and "reorder" not in reply
    assert document.children(parent) == [z, y, x]


async def test_G_08_structure_locked_from_the_click_on_execute(start_gateway, settings) -> None:
    gw = await start_gateway(settings, GatedFactory(settings))
    first, second = await gw.browser(), await gw.browser()
    root = await gw.new_tree(first, "robot")
    hold = await gw.add(first, "robot", "Hold", root, fields={"Seconds": "0.2", "Label": "x"})
    link: GatedLink = gw.session().link
    link.gate.clear()
    start = first.mark()
    await first.send(t="cmd", cid=99, name="execute")
    await gw.until(lambda: gw.session().starting)
    reply = await gw.op(second, "robot", {"op": "node.remove", "ids": [hold]})
    assert reply["t"] == "no"
    link.gate.set()
    run = await first.wait_for(lambda m: m["t"] == "run" and m["event"] == "started", start=start)
    assert run["node_paths"] == [root, hold]  # the tree that runs has the node
    assert (await first.wait_for(lambda m: m.get("cid") == 99, start=start))["t"] == "ok"


async def test_G_17_literals(gw: Harness) -> None:
    browser = await gw.browser()
    reply = await browser.request("ask", kind="parse_literal", type="List", text="[1, 2.5, 'a']")
    assert reply["t"] == "ok" and reply["value"] == "[1, 2.5, 'a']" and reply["type"] == "List"
    reply = await browser.request("ask", kind="parse_literal", text="[1, 2.5, 'a']")
    assert reply["t"] == "ok" and reply["type"] == "List"
    reply = await browser.request("ask", kind="parse_literal", text="__import__('os')")
    assert reply["t"] == "no"
    reply = await browser.request("ask", kind="parse_literal", text="'" + "x" * (2 * 1024 * 1024) + "'")
    assert reply["t"] == "no" and "MB" in reply["reason"]
    reply = await browser.request("ask", kind="parse_literal", text="[" * 200 + "]" * 200)
    assert reply["t"] == "no" and "deeper" in reply["reason"]


async def test_G_21_new_and_load_refused_during_a_run(gw: Harness) -> None:
    browser = await gw.browser()
    await _running(gw, browser)
    reply = await browser.request("file", action="new", name="other")
    assert reply["t"] == "no" and "not idle" in reply["reason"]
    reply = await browser.request("file", action="load", name="robot")
    assert reply["t"] == "no" and "not idle" in reply["reason"]


async def test_values_are_converted_from_wire_form(gw: Harness) -> None:
    browser = await gw.browser()
    root = await gw.new_tree(browser, "robot")
    await gw.bb_add(browser, "battery", "Double", "17.5")
    evaluation = await gw.add(browser, "robot", "Evaluation", root, fields={
        "ValueName": "battery", "CompareTo": None, "LiteralValue": {"type": "Double", "value": "20.0"},
        "Operator": ">=", "Wait": False, "WaitTimeout": "0.0"})
    assert gw.doc("robot").fields(evaluation)["LiteralValue"] == 20.0
    reply = await gw.op(browser, "robot", {"op": "node.set_field", "id": evaluation, "field": "LiteralValue",
                                           "value": {"type": "Double", "value": "nan"}})
    assert reply["t"] == "ok"
    wire = next(n for n in gw.doc("robot").to_wire(gw.session().board.types())["nodes"] if n["id"] == evaluation)
    assert wire["fields"]["LiteralValue"] == {"type": "Double", "value": "nan"}
    entry = gw.session().board.wire_entries()[0]
    assert entry["value"] == "17.5" and entry["type"] == "Double"


async def test_paste_in_wire_form_and_promote(gw: Harness) -> None:
    browser = await gw.browser()
    root = await gw.new_tree(browser, "robot")
    sequence = await gw.add(browser, "robot", "Sequence", root, memory=False, x=0, y=100)
    group = await gw.add(browser, "robot", "Group", width=600, height=400, x=-300, y=50, color="#3d7cc9")
    a, b = hexid(), hexid()
    reply = await gw.op(browser, "robot", {"op": "paste", "encoding": "wire", "sizes": {a: [130, 74]},
                                           "nodes": [{"id": a, "type": "Stop", "title": "Stop", "x": -100, "y": 250},
                                                     {"id": b, "type": "Pause", "title": "Pause", "x": 50, "y": 250}],
                                           "connections": []})
    assert reply["t"] == "ok"
    for child in (a, b):
        assert (await gw.op(browser, "robot", {"op": "conn.add", "parent": sequence, "child": child}))["t"] == "ok"
    reply = await gw.op(browser, "robot", {"op": "group.promote", "id": group, "name": "part"})
    assert reply["t"] == "ok", reply
    document = gw.doc("robot")
    assert not document.has_node(group) and not document.has_node(a)
    assert (gw.settings.trees_dir / "part_BT.json").is_file()
    reply = await gw.op(browser, "robot", {"op": "group.promote", "id": hexid(), "name": "part"})
    assert reply["t"] == "no"
