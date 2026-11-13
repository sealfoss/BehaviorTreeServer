"""Files (design 9.2, 9.4, contract 5.4): Appendix B.11 G-18, G-20; file actions and their
questions, recovery, uploads and downloads."""

from __future__ import annotations

import asyncio
import io
import json
import zipfile

from aiohttp import FormData

from gateway_helpers import Harness, tree_file


async def test_G_18_unsaved_changes_survive_a_gateway_restart(start_gateway, settings) -> None:
    gw = await start_gateway(settings)
    browser = await gw.browser()
    root = await gw.new_tree(browser, "robot")
    stop = await gw.add(browser, "robot", "Stop", root, title="Changed")
    recovery = settings.trees_dir / ".recovery" / "robot_BT.json"
    await asyncio.sleep(2.0)
    assert recovery.is_file()  # within 2 s of the change, before any shutdown
    assert any(node["id"] == stop for node in json.loads(recovery.read_text(encoding="utf-8"))["nodes"])
    await browser.close()
    await gw.client.close()

    gw = await start_gateway(settings)
    browser = await gw.browser()
    assert browser.snapshot["root"] == "robot"
    assert browser.snapshot["recovery"] == ["robot"]
    assert not any(node["id"] == stop for node in browser.snapshot["docs"]["robot"]["nodes"])
    reply = await browser.request("file", action="recover", doc="robot", accept=True)
    assert reply["t"] == "ok"
    assert any(node["id"] == stop and node["title"] == "Changed" for node in reply["document"]["nodes"])
    assert gw.doc("robot").unsaved
    assert (await browser.request("file", action="save", doc="robot"))["t"] == "ok"
    await gw.until(lambda: not recovery.exists())


async def test_G_20_go_back_never_discards_and_revert_does(gw: Harness) -> None:
    first, second = await gw.browser(), await gw.browser()
    root = await gw.new_tree(first, "robot")
    await gw.new_tree(first, "patrol")
    reply = await first.request("file", action="load", name="robot")
    assert reply["t"] == "ok"
    sub = await gw.add(first, "robot", "Sub-Tree", root, fields={"File": "patrol_BT.json"})
    shown = []
    for browser in (first, second):
        reply = await browser.request("file", action="zoom_in", doc="robot", node=sub, path=sub)
        assert reply["t"] == "ok" and reply["doc"] == "patrol"
        shown.append(reply["document"])
    added = await gw.add(first, "patrol", "Stop", gw.doc("patrol").root_id)
    assert gw.doc("patrol").unsaved
    assert (await first.request("file", action="go_back", doc="patrol"))["t"] == "ok"
    assert gw.doc("patrol") is not None and gw.doc("patrol").has_node(added)  # nothing discarded
    start = second.mark()
    reply = await second.request("file", action="revert", doc="patrol")
    assert reply["t"] == "ok"
    event = await second.wait_for(lambda m: m["t"] == "doc" and m["event"] == "reverted", start=start)
    assert not any(node["id"] == added for node in event["document"]["nodes"])
    assert not gw.doc("patrol").unsaved and not gw.doc("patrol").has_node(added)
    reply = await first.request("file", action="zoom_in", doc="robot", node=sub, path=sub)
    assert reply["document"]["nodes"] == event["document"]["nodes"]  # both show the file's state
    for browser in (first, second):
        await browser.request("file", action="go_back", doc="patrol")
    assert gw.doc("patrol") is None  # closed: no browser shows it, no unsaved changes


async def test_new_and_load_ask_about_unsaved_changes(gw: Harness) -> None:
    browser = await gw.browser()
    root = await gw.new_tree(browser, "robot")
    await gw.add(browser, "robot", "Stop", root)
    reply = await browser.request("file", action="new", name="other")
    assert reply["t"] == "no" and reply["question"] == {"kind": "unsaved", "docs": ["robot"]}
    reply = await browser.request("file", action="new", name="robot", choice="discard")
    assert reply["t"] == "no" and reply["question"] == {"kind": "replace", "name": "robot"}
    reply = await browser.request("file", action="new", name="other", choice="save")
    assert reply["t"] == "ok"
    saved = json.loads((gw.settings.trees_dir / "robot_BT.json").read_text(encoding="utf-8"))
    assert len(saved["nodes"]) == 2
    assert gw.session().root == "other" and gw.doc("robot") is None
    reply = await browser.request("file", action="load", name="robot.json")
    assert reply["t"] == "ok" and gw.session().root == "robot"


async def test_save_returns_version_1_warnings_and_save_as_renames(gw: Harness) -> None:
    browser = await gw.browser()
    root = await gw.new_tree(browser, "robot")
    sequence = await gw.add(browser, "robot", "Sequence", root, memory=True)
    await gw.add(browser, "robot", "Stop", sequence, x=0, y=200)
    await gw.add(browser, "robot", "Pause", sequence, x=10, y=300)
    await gw.bb_add(browser, "n", "Integer", 4)
    reply = await browser.request("file", action="save", doc="robot", view={"zoom": 1.5, "center": [10, 20]})
    assert reply["t"] == "ok" and len(reply["warnings"]) == 1 and "40 pixels" in reply["warnings"][0]
    assert json.loads((gw.settings.trees_dir / "robot_BT.json").read_text(encoding="utf-8"))["view"]["zoom"] == 1.5
    reply = await browser.request("file", action="save_as", doc="robot", name="cells/patrol")
    assert reply["t"] == "ok"
    event = await browser.wait_for(lambda m: m["t"] == "doc" and m["event"] == "renamed")
    assert event["doc"] == "robot" and event["document"]["key"] == "cells/patrol"
    assert gw.session().root == "cells/patrol"
    assert (gw.settings.trees_dir / "cells" / "patrol_BT.json").is_file()
    assert (gw.settings.trees_dir / "cells" / "patrol_BB.json").is_file()
    reply = await browser.request("file", action="save_as", doc="cells/patrol", name="robot")
    assert reply["t"] == "no" and reply["question"] == {"kind": "replace", "name": "robot"}
    listing = await browser.request("file", action="list")
    keys = {tree["key"]: tree for tree in listing["trees"]}
    assert keys["cells/patrol"]["open_in"] == "local" and keys["cells/patrol"]["dirs"] == ["cells"]
    assert keys["robot"]["open_in"] is None and listing["dirs"] == ["cells"]


async def test_rename_and_delete(gw: Harness) -> None:
    browser = await gw.browser()
    await gw.new_tree(browser, "robot")
    await gw.new_tree(browser, "spare")
    reply = await browser.request("file", action="delete", name="spare")
    assert reply["t"] == "no"  # open
    reply = await browser.request("file", action="rename", name="robot", new_name="cells/robot2")
    assert reply["t"] == "ok"
    assert (gw.settings.trees_dir / "cells" / "robot2_BT.json").is_file()
    assert (await browser.request("file", action="delete", name="cells/robot2"))["t"] == "ok"
    assert not (gw.settings.trees_dir / "cells" / "robot2_BT.json").exists()
    assert not (gw.settings.trees_dir / "cells" / "robot2_BB.json").exists()
    reply = await browser.request("file", action="rename", name="spare", new_name="spare2")
    assert reply["t"] == "ok" and gw.session().root == "spare2"


async def test_zoom_in_on_a_missing_file_asks_to_choose(gw: Harness) -> None:
    browser = await gw.browser()
    root = await gw.new_tree(browser, "robot")
    await gw.new_tree(browser, "patrol")
    await browser.request("file", action="load", name="robot")
    sub = await gw.add(browser, "robot", "Sub-Tree", root, fields={"File": "C:\\trees\\patrol_BT.json"})
    reply = await browser.request("file", action="zoom_in", doc="robot", node=sub, path=sub)
    assert reply["t"] == "no" and reply["question"]["kind"] == "choose_file" and reply["question"]["node"] == sub
    reply = await browser.request("file", action="zoom_in", doc="robot", node=sub, path=sub, file="patrol")
    assert reply["t"] == "ok" and reply["doc"] == "patrol"
    assert gw.doc("robot").fields(sub)["File"] == "patrol_BT.json"


async def test_upload_checks_and_download(gw: Harness) -> None:
    browser = await gw.browser()
    form = FormData()
    form.add_field("file", b"{not json", filename="bad_BT.json")
    response = await gw.client.post("/upload", data=form)
    assert response.status == 400 and not (gw.settings.trees_dir / "bad_BT.json").exists()
    board = json.dumps({"format": "behavior_tree_widget_blackboard", "version": 1,
                        "blackboard": [{"name": "n", "type": "Integer", "value": 3}]}).encode()
    form = FormData()
    form.add_field("file", tree_file(), filename="up_BT.json")
    form.add_field("file", board, filename="up_BB.json")
    form.add_field("dir", "cells")
    response = await gw.client.post("/upload", data=form)
    assert response.status == 200 and (await response.json())["key"] == "cells/up"
    form = FormData()
    form.add_field("file", tree_file(), filename="up_BT.json")
    form.add_field("dir", "cells")
    response = await gw.client.post("/upload", data=form)
    assert response.status == 409 and (await response.json())["question"] == {"kind": "replace", "name": "cells/up"}
    form = FormData()
    form.add_field("file", b" " * (11 * 1024 * 1024), filename="huge_BT.json")
    response = await gw.client.post("/upload", data=form)
    assert response.status == 413
    response = await gw.client.get("/download/cells/up")
    assert response.status == 200 and response.content_type == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(await response.read()))
    assert sorted(archive.namelist()) == ["up_BB.json", "up_BT.json"]
    reply = await browser.request("file", action="load", name="cells/up")
    assert reply["t"] == "ok"
    assert [entry["name"] for entry in gw.session().board.wire_entries()] == ["n"]


async def test_load_of_the_open_tree_with_discard_reads_the_file(gw: Harness) -> None:
    browser = await gw.browser()
    root = await gw.new_tree(browser, "robot")
    stop = await gw.add(browser, "robot", "Stop", root)
    reply = await browser.request("file", action="load", name="robot")
    assert reply["t"] == "no" and reply["question"]["kind"] == "unsaved"
    reply = await browser.request("file", action="load", name="robot", choice="discard")
    assert reply["t"] == "ok"
    assert not gw.doc("robot").has_node(stop) and not gw.doc("robot").unsaved
