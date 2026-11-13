"""Browser tests of the page for the findings of its audit against design 11 and the widget's
user guide (gestures and dialogs that unit tests cannot reach)."""

from __future__ import annotations

import json

import pytest

from page_helpers import node, root, wait_until, write_tree

pytestmark = pytest.mark.page


def _print(title, x, y):
    return node("Print Message", title, x, y, fields={"Level": 3, "Source": None, "Message": title})


def test_zoom_in_on_a_missing_file_creates_the_tree(start_gateway, open_tab):
    top = root()
    sub = node("Sub-Tree", "patrol", -60, 140, fields={"File": "patrol_BT.json"})
    gateway = start_gateway(lambda trees: write_tree(trees, "z1", [top, sub], [(top, sub)]))
    tab = open_tab(gateway, wait_tree=False)
    tab.load("z1")
    tab.menu(sub["id"], "Zoom In")
    tab.tid("dialog-choose-file").wait_for()
    assert tab.tid("dialog-input").input_value() == "patrol"
    tab.tid("dialog-btn-create").click()
    wait_until(lambda: "patrol" in (tab.tid("breadcrumb").text_content() or ""), message="not zoomed in")
    tab.page.wait_for_selector('[data-type="Root"]')
    assert tab.tid("dialog-choose-file").count() == 0  # asked once, not again
    assert (gateway.trees_dir / "patrol_BT.json").is_file()
    assert len(tab.node_ids()) == 1  # a new tree holding only a Root


def test_upload_over_an_existing_tree_asks_to_replace(start_gateway, open_tab, tmp_path):
    gateway = start_gateway(lambda trees: write_tree(trees, "up", [root()], []))
    upload = tmp_path / "up_BT.json"
    upload.write_text(json.dumps({"format": "behavior_tree_widget", "version": 1, "config": {}, "connections": [],
                                  "nodes": [root(), _print("Uploaded", 0, 200)]}), encoding="utf-8")
    tab = open_tab(gateway, wait_tree=False)
    tab.tid("btn-files").click()
    tab.tid("file-upload-input").set_input_files(str(upload))
    tab.tid("btn-file-upload").click()
    tab.tid("dialog-replace").wait_for()
    tab.tid("dialog-btn-yes").click()
    wait_until(lambda: "Uploaded" in (tab.page.locator("[data-testid=notice]").last.text_content() or ""),
               message="no confirmation")
    assert not any("{" in (text or "") for text in tab.page.locator("[data-testid=notice]").all_text_contents())
    assert "Uploaded" in (gateway.trees_dir / "up_BT.json").read_text(encoding="utf-8")


def test_literal_value_follows_the_type_of_value_name(start_gateway, open_tab):
    top = root()
    check = node("Evaluation", "Check", -60, 160, fields={"ValueName": "count", "CompareTo": None, "LiteralValue": 5,
                                                          "Operator": "=="})
    board = [{"name": "count", "type": "Integer", "value": 3, "reset": False},
             {"name": "speed", "type": "Double", "value": 1.5, "reset": False}]
    gateway = start_gateway(lambda trees: write_tree(trees, "lit", [top, check], [(top, check)], blackboard=board))
    tab = open_tab(gateway, wait_tree=False)
    tab.load("lit")
    literal = tab.tid(f"field-{check['id']}-LiteralValue")
    assert literal.get_attribute("type") == "number" and literal.input_value() == "5"
    tab.page.select_option(f'[data-testid="field-{check["id"]}-ValueName"]', "speed")
    wait_until(lambda: literal.get_attribute("type") == "text" and literal.input_value() == "5.0",
               message=f"literal editor {literal.get_attribute('type')!r} {literal.input_value()!r}")
    other = open_tab(gateway)
    assert other.tid(f"field-{check['id']}-LiteralValue").input_value() == "5.0"


def test_a_shortened_value_stays_in_its_column(start_gateway, open_tab):
    board = [{"name": "long", "type": "String", "value": "x" * 70_000, "reset": False},
             {"name": "n", "type": "Integer", "value": 1, "reset": False}]
    gateway = start_gateway(lambda trees: write_tree(trees, "tr", [root()], [], blackboard=board))
    tab = open_tab(gateway, wait_tree=False)
    tab.load("tr")
    button = tab.box("bb-value-long")
    reset = tab.box("bb-reset-long")
    assert button["x"] + button["width"] <= reset["x"], (button, reset)
    tab.tid("bb-value-long").click()
    tab.tid("dialog-entry-value").wait_for()
    wait_until(lambda: len(tab.tid("entry-value-text").input_value()) == 70_000, message="the whole value")


def test_escape_keeps_the_old_group_name_and_node_title(start_gateway, open_tab):
    group = node("Group", "box", -420, 60, width=400, height=200, color="#3d7cc9")
    leaf = _print("Leaf", 200, 200)
    gateway = start_gateway(lambda trees: write_tree(trees, "esc", [root(0, -150), group, leaf], []))
    tab = open_tab(gateway, wait_tree=False)
    tab.load("esc")
    tab.tid(f"group-name-{group['id']}").click()
    editor = tab.tid(f"group-name-editor-{group['id']}")
    editor.fill("changed")
    editor.press("Escape")
    tab.page.wait_for_timeout(300)
    assert tab.tid(f"group-name-{group['id']}").text_content() == "box"
    tab.menu(leaf["id"], "Rename...")
    rename = tab.tid(f"rename-{leaf['id']}")
    rename.fill("Renamed")
    rename.press("Escape")
    tab.page.wait_for_timeout(300)
    assert tab.tid(f"title-{leaf['id']}").text_content() == "Leaf"
    other = open_tab(gateway)
    assert other.tid(f"group-name-{group['id']}").text_content() == "box"
    assert other.tid(f"title-{leaf['id']}").text_content() == "Leaf"
    # Enter keeps the new name.
    tab.tid(f"group-name-{group['id']}").click()
    editor.fill("kept")
    editor.press("Enter")
    wait_until(lambda: other.tid(f"group-name-{group['id']}").text_content() == "kept", message="Enter did not rename")


def test_add_entry_asks_name_type_and_value(start_gateway, open_tab):
    gateway = start_gateway(lambda trees: write_tree(trees, "ae", [root()], [], blackboard=[]))
    tab = open_tab(gateway, wait_tree=False)
    tab.load("ae")
    tab.tid("bb-add").click()
    tab.tid("add-entry-name").fill("speed")
    tab.page.select_option("[data-testid=add-entry-type]", "Double")
    tab.tid("add-entry-value").fill("1,5")
    tab.tid("btn-add-entry-ok").click()
    wait_until(lambda: tab.tid("bb-row-speed").count() == 1, message="no entry")
    assert tab.tid("bb-row-speed").locator(".bb-col-type").text_content() == "Double"
    assert tab.tid("bb-value-speed").input_value() == "1.5"
    tab.tid("bb-add").click()
    tab.tid("add-entry-name").fill("items")
    tab.page.select_option("[data-testid=add-entry-type]", "List")
    tab.tid("add-entry-value").fill("[1, 'a'")
    tab.tid("btn-add-entry-ok").click()
    tab.tid("add-entry-error").wait_for()
    tab.tid("add-entry-value").fill("[1, 'a']")
    tab.tid("btn-add-entry-ok").click()
    wait_until(lambda: tab.tid("bb-row-items").count() == 1, message="no list entry")
    assert tab.tid("bb-value-items").input_value() == "[1, 'a']"


def test_below_40_percent_zoom_only_title_and_status_and_the_order_stays(start_gateway, open_tab):
    top = root()
    sequence = node("Sequence", "Sequence", -20, 110, memory=True)
    # Narrow left of wide: with the widths drawn at full detail the narrow one comes first.
    narrow = node("Stop", "Narrow", -60, 240)
    wide = _print("Wide one with a long title", -150, 400)
    gateway = start_gateway(lambda trees: write_tree(trees, "lod", [top, sequence, narrow, wide],
                                                     [(top, sequence), (sequence, narrow), (sequence, wide)]))
    tab = open_tab(gateway, wait_tree=False)
    tab.load("lod")
    width = tab.world_rect(wide["id"])["w"]
    height = tab.world_rect(wide["id"])["h"]
    orders = (tab.order(sequence["id"], narrow["id"]), tab.order(sequence["id"], wide["id"]))
    x, y = tab.center("canvas")
    tab.page.mouse.move(x, y)
    tab.page.keyboard.down("Control")
    for _ in range(8):
        tab.page.mouse.wheel(0, 100)
    tab.page.keyboard.up("Control")
    wait_until(lambda: tab.zoom() < 0.4, message=f"zoom {tab.zoom()}")
    assert tab.tid(f"fields-{wide['id']}").is_hidden()
    assert tab.tid(f"parent-{wide['id']}").is_hidden()
    assert tab.tid(f"title-{wide['id']}").is_visible() and tab.tid(f"status-{wide['id']}").is_visible()
    wait_until(lambda: tab.world_rect(wide["id"])["h"] < height / 2, message="the node did not shrink")
    assert tab.world_rect(wide["id"])["w"] == width  # the full width is kept
    # Moving a node at low zoom keeps the order worked out with the full widths.
    tab.page.keyboard.down("Control")
    for _ in range(8):
        tab.page.mouse.wheel(0, -100)
    tab.page.keyboard.up("Control")
    wait_until(lambda: tab.zoom() >= 0.4, message=f"zoom {tab.zoom()}")
    assert tab.tid(f"fields-{wide['id']}").is_visible()
    assert (tab.order(sequence["id"], narrow["id"]), tab.order(sequence["id"], wide["id"])) == orders


def test_sub_tree_file_chosen_from_the_server_trees(start_gateway, open_tab):
    top = root()
    sub = node("Sub-Tree", "Sub", -60, 140, fields={"File": ""})

    def prepare(trees):
        write_tree(trees, "cells/robot", [top, sub], [(top, sub)])
        write_tree(trees, "shared/patrol", [root()], [])

    gateway = start_gateway(prepare)
    tab = open_tab(gateway, wait_tree=False)
    tab.load("cells/robot")
    tab.tid(f"browse-{sub['id']}").click()
    tab.tid("dialog-choose-subtree").wait_for()
    tab.tid("dialog-choices").get_by_text("shared/patrol").click()
    tab.tid("dialog-btn-ok").click()
    field = tab.tid(f"field-{sub['id']}-File")
    wait_until(lambda: field.input_value() == "../shared/patrol_BT.json", message=field.input_value())
    assert "shared/patrol_BT.json" in (field.get_attribute("title") or "")


def test_promote_selects_the_new_sub_tree_node(start_gateway, open_tab):
    top = root(0, -150)
    group = node("Group", "patrol", -420, 60, width=560, height=300, color="#3d7cc9")
    inside = _print("Inside", -380, 140)
    gateway = start_gateway(lambda trees: write_tree(trees, "pr", [top, group, inside], [(top, inside)]))
    tab = open_tab(gateway, wait_tree=False)
    tab.load("pr")
    box = tab.box(f"group-title-{group['id']}")
    tab.page.mouse.click(box["x"] + box["width"] - 40, box["y"] + box["height"] / 2, button="right")
    tab.tid("menu-Promote-to-Sub-Tree").click()
    wait_until(lambda: tab.node_ids("Sub-Tree"), message="no Sub-Tree node")
    sub = tab.node_ids("Sub-Tree")[0]
    selected = tab.page.eval_on_selector_all(".node.selected, .group.selected, .note.selected", "n => n.map(x => x.dataset.nodeId)")
    assert selected == [sub]
    assert tab.tid(f"title-{sub}").text_content() == "patrol"


def test_new_tree_clears_the_last_run_and_the_notice_says_the_outcome_once(start_gateway, open_tab):
    top = root()
    leaf = _print("Hello", -60, 140)

    def prepare(trees):
        write_tree(trees, "r1", [top, leaf], [(top, leaf)])
        write_tree(trees, "r2", [root()], [])

    gateway = start_gateway(prepare)
    tab = open_tab(gateway, wait_tree=False)
    tab.load("r1")
    tab.tid("btn-execute").click()
    wait_until(lambda: "succeeded" in tab.run_state(), message=tab.run_state())
    notice = tab.page.locator("[data-testid=notice]").last
    wait_until(lambda: "Execution finished" in (notice.text_content() or ""), message="no notice")
    assert (notice.text_content() or "").count("Succeeded") == 1, notice.text_content()
    box, remove = notice.bounding_box(), tab.box("bb-remove")
    assert box["x"] + box["width"] <= remove["x"] or box["x"] >= remove["x"] + remove["width"] \
        or box["y"] + box["height"] <= remove["y"] or box["y"] >= remove["y"] + remove["height"], (box, remove)
    tab.load("r2")
    wait_until(lambda: tab.run_state().strip() == "IDLE", message=tab.run_state())


def test_offline_disables_every_command(start_gateway, open_tab):
    leaf = _print("Leaf", -60, 140)
    gateway = start_gateway(lambda trees: write_tree(trees, "off", [root(), leaf], [], blackboard=[
        {"name": "n", "type": "Integer", "value": 1, "reset": False}]))
    tab = open_tab(gateway, wait_tree=False)
    tab.load("off")
    gateway.stop()
    tab.tid("offline").wait_for()
    for button in ("btn-new", "btn-load", "btn-save", "btn-save-as", "btn-files", "btn-revert", "btn-execute",
                   "btn-pause", "btn-stop", "btn-reset", "btn-configure", "chk-loop", "executor-picker", "bb-add",
                   "bb-source-n", "bb-reset-n"):
        assert tab.tid(button).is_disabled(), button
    assert tab.tid(f"field-{leaf['id']}-Message").is_disabled()
    assert "?" in (tab.tid("attached-count").text_content() or "")


def test_findings_name_the_node_and_lead_into_the_sub_tree(start_gateway, open_tab):
    top = root()
    negation = node("Negation", "Not anything", 200, 140)
    sub = node("Sub-Tree", "patrol", -200, 140, fields={"File": "patrol_BT.json"})
    inner_root = root()
    inner = node("Negation", "Inner negation", -60, 140)

    def prepare(trees):
        write_tree(trees, "fi", [top, negation, sub], [(top, negation), (top, sub)])
        write_tree(trees, "patrol", [inner_root, inner], [(inner_root, inner)])

    gateway = start_gateway(prepare)
    tab = open_tab(gateway, wait_tree=False)
    tab.load("fi")
    tab.tid("btn-execute").click()
    tab.tid("dialog-findings").wait_for()
    texts = tab.page.locator('[data-testid^="finding-"]').all_text_contents()
    assert any("fi › Not anything" in text for text in texts), texts
    assert not any(negation["id"] in text for text in texts)
    inner_index = next(i for i, text in enumerate(texts) if "patrol" in text)
    tab.tid(f"finding-{inner_index}").click()
    wait_until(lambda: "patrol" in (tab.tid("breadcrumb").text_content() or ""), message="not zoomed into patrol")
    wait_until(lambda: "selected" in (tab.tid(f"node-{inner['id']}").get_attribute("class") or ""), message="not selected")


def test_tab_title_type_header_and_drag_line_on_top(start_gateway, open_tab):
    top = root()
    leaf = _print("Leaf", -60, 160)
    gateway = start_gateway(lambda trees: write_tree(trees, "vis", [top, leaf], [], blackboard=[
        {"name": "d", "type": "Dictionary", "value": {"k": 3, "b": "long text"}, "reset": False}]))
    tab = open_tab(gateway, wait_tree=False)
    tab.load("vis")
    assert tab.page.title() == "Behavior Tree - vis_BT.json"
    assert tab.page.locator(".bb-header .bb-col-type").is_visible()
    type_box = tab.page.locator("[data-testid=bb-row-d] .bb-col-type").bounding_box()
    name_box = tab.box("bb-name-d")
    assert type_box["x"] + type_box["width"] <= name_box["x"] + 1
    start = tab.center(f"children-{top['id']}")
    end = tab.center(f"title-{leaf['id']}")
    mouse = tab.page.mouse
    mouse.move(*start)
    mouse.down()
    mouse.move(*end, steps=8)
    on_top = tab.page.evaluate("""() => {
        const line = document.querySelector('[data-testid="drag-line"]');
        const nodes = document.querySelector('.layer-nodes');
        return !!line && !!(nodes.compareDocumentPosition(line) & Node.DOCUMENT_POSITION_FOLLOWING);
    }""")
    mouse.up()
    assert on_top


def test_recovery_answered_in_one_tab_closes_the_offer_in_the_other(start_gateway, open_tab):
    leaf = _print("Leaf", -60, 160)
    gateway = start_gateway(lambda trees: write_tree(trees, "rec", [root(), leaf], []))
    first = open_tab(gateway, wait_tree=False)
    first.load("rec")
    second = open_tab(gateway)
    first.menu(leaf["id"], "Rename...")
    rename = first.tid(f"rename-{leaf['id']}")
    rename.fill("Changed")
    rename.press("Enter")
    wait_until(lambda: second.tid(f"title-{leaf['id']}").text_content() == "Changed", message="not renamed")
    wait_until(lambda: any((gateway.trees_dir / ".recovery").rglob("*.json")) if (gateway.trees_dir / ".recovery").exists() else False,
               timeout=6, message="no recovery file")
    gateway.stop()
    first.tid("offline").wait_for()
    gateway.start()
    for tab in (first, second):
        tab.tid("dialog-recovery").wait_for(timeout=15_000)
    first.tid("dialog-btn-yes").click()
    second.tid("dialog-recovery").wait_for(state="detached", timeout=10_000)
    wait_until(lambda: second.tid(f"title-{leaf['id']}").text_content() == "Changed", message="not recovered")
