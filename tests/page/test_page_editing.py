"""Browser tests of the page against a real gateway (Appendix B.13): editing.

P-01 (build and run a tree), P-02 (child order), P-03 (copy and paste), P-04 (zoom), P-07
(groups), P-08 (notes), P-10 (the Double editor), P-11 (two tabs).
"""

from __future__ import annotations

import pytest

from page_helpers import node, root, wait_until, write_tree

pytestmark = pytest.mark.page


def test_P_01_new_tree_add_connect_execute(start_gateway, open_tab):
    gateway = start_gateway()
    tab = open_tab(gateway, wait_tree=False)
    tab.file_dialog("btn-new", "p01")
    tab.page.wait_for_selector('[data-type="Root"]')
    root_id = tab.node_ids("Root")[0]
    canvas = tab.box("canvas")
    root_box = tab.box(f"node-{root_id}")
    below = root_box["y"] + root_box["height"]
    sequence = tab.add_node("Sequence", (root_box["x"] + 10, below + 90))
    sequence_box = tab.box(f"node-{sequence}")
    row = sequence_box["y"] + sequence_box["height"] + 90
    first = tab.add_node("Print Message", (max(canvas["x"] + 20, root_box["x"] - 330), row))
    second = tab.add_node("Log Message", (root_box["x"] + 120, row))
    tab.connect(root_id, sequence)
    tab.connect(sequence, first)
    tab.connect(sequence, second)
    assert tab.order(sequence, first) == "1" and tab.order(sequence, second) == "2"

    tab.tid("btn-execute").click()
    wait_until(lambda: "succeeded" in tab.run_state(), timeout=15, message=f"run state {tab.run_state()!r}")
    statuses = {node_id: tab.status(node_id) for node_id in (root_id, sequence, first, second)}
    assert statuses == dict.fromkeys(statuses, "succeeded"), statuses
    for node_id in statuses:
        assert tab.tid(f"status-{node_id}").text_content() == "Succeeded"

    tab.tid("tab-log").click()
    texts = wait_until(lambda: tab.log_texts() if any("Execution finished" in t for t in tab.log_texts()) else None)
    for title in ("Root", "Sequence", "Print Message", "Log Message"):
        assert any(f"'{title}'" in text and ("executed" in text or "finished" in text) for text in texts), (title, texts)
    assert not tab.errors


def _sequence_tree(trees, key, titles, xs, y=220.0):
    top = root(0, 0)
    sequence = node("Sequence", "Sequence", -20, 110, memory=True)
    leaves = [node("Print Message", title, x, y, fields={"Level": 3, "Source": None, "Message": title})
              for title, x in zip(titles, xs, strict=True)]
    write_tree(trees, key, [top, sequence, *leaves], [(top, sequence)] + [(sequence, leaf) for leaf in leaves])
    return top, sequence, leaves


def test_P_02_dragging_a_child_left_of_its_sibling_swaps_the_order(start_gateway, open_tab):
    nodes = {}
    gateway = start_gateway(lambda trees: nodes.update(zip(("root", "seq", "leaves"), _sequence_tree(
        trees, "p02", ["A", "B"], [-260.0, 140.0]))))
    tab = open_tab(gateway, wait_tree=False)
    tab.load("p02")
    seq, (a, b) = nodes["seq"]["id"], [leaf["id"] for leaf in nodes["leaves"]]
    assert (tab.order(seq, a), tab.order(seq, b)) == ("1", "2")
    a_box = tab.box(f"node-{a}")
    start = tab.center(f"title-{b}")
    tab.drag(start, (a_box["x"] - 200, start[1]))
    wait_until(lambda: (tab.order(seq, a), tab.order(seq, b)) == ("2", "1"),
               message=f"orders {(tab.order(seq, a), tab.order(seq, b))}")
    # The gateway has the new order: a tab opened now draws it from the gateway's document.
    other = open_tab(gateway)
    wait_until(lambda: (other.order(seq, a), other.order(seq, b)) == ("2", "1"),
               message=f"the gateway's order {(other.order(seq, a), other.order(seq, b))}")
    assert not tab.errors and not other.errors


def _overlaps(rect, other, gap):
    return (rect["x"] < other["x"] + other["w"] + gap and rect["x"] + rect["w"] > other["x"] - gap
            and rect["y"] < other["y"] + other["h"] + gap and rect["y"] + rect["h"] > other["y"] - gap)


def test_P_03_copy_and_paste(start_gateway, open_tab):
    nodes = {}
    gateway = start_gateway(lambda trees: nodes.update(zip(("root", "seq", "leaves"), _sequence_tree(
        trees, "p03", ["L", "M"], [-260.0, 140.0]))))
    tab = open_tab(gateway, wait_tree=False)
    tab.load("p03")
    root_id, seq = nodes["root"]["id"], nodes["seq"]["id"]
    leaf = nodes["leaves"][0]["id"]
    before = set(tab.node_ids())
    tab.tid(f"title-{seq}").click()
    tab.tid(f"title-{leaf}").click(modifiers=["Shift"])
    tab.page.keyboard.press("Control+c")
    tab.page.keyboard.press("Control+v")
    new = wait_until(lambda: set(tab.node_ids()) - before if len(set(tab.node_ids()) - before) == 2 else None,
                     message="two nodes were not pasted")
    types = {tab.tid(f"node-{node_id}").get_attribute("data-type"): node_id for node_id in new}
    assert set(types) == {"Sequence", "Print Message"}
    assert tab.tid(f"conn-{types['Sequence']}-{types['Print Message']}").count() == 1
    selected = set(tab.page.eval_on_selector_all(".node.selected", "nodes => nodes.map(n => n.dataset.nodeId)"))
    assert selected == new
    wait_until(lambda: all(tab.page.evaluate("id => !!document.querySelector(`[data-testid=node-${id}]`)", i) for i in new))
    for pasted in new:
        rect = tab.world_rect(pasted)
        for other in before:
            assert not _overlaps(rect, tab.world_rect(other), 20 - 0.5), (pasted, other)

    # The Root is never copied.
    before = set(tab.node_ids())
    tab.tid(f"title-{root_id}").click()
    tab.tid(f"title-{seq}").click(modifiers=["Shift"])
    tab.page.keyboard.press("Control+c")
    tab.page.keyboard.press("Control+v")
    added = wait_until(lambda: set(tab.node_ids()) - before, message="nothing was pasted")
    tab.page.wait_for_timeout(300)
    added = set(tab.node_ids()) - before
    assert [tab.tid(f"node-{node_id}").get_attribute("data-type") for node_id in added] == ["Sequence"]
    assert len(tab.node_ids("Root")) == 1
    assert not tab.errors


def test_P_04_ctrl_wheel_zooms_the_canvas_not_the_page(start_gateway, open_tab):
    gateway = start_gateway(lambda trees: write_tree(trees, "p04", [root()], []))
    tab = open_tab(gateway, wait_tree=False)
    tab.load("p04")
    page = tab.page
    x, y = tab.center("canvas")
    page.mouse.move(x, y)
    start = tab.zoom()
    page_scale = page.evaluate("[window.visualViewport.scale, window.devicePixelRatio, window.outerWidth]")
    page.keyboard.down("Control")
    page.mouse.wheel(0, -100)
    page.keyboard.up("Control")
    wait_until(lambda: tab.zoom() > start, message="Ctrl+wheel did not zoom in")
    assert tab.zoom() == pytest.approx(start * 1.15, rel=1e-3)
    page.keyboard.down("Control")
    for _ in range(30):
        page.mouse.wheel(0, -100)
    page.keyboard.up("Control")
    wait_until(lambda: tab.zoom() == pytest.approx(3.0), message=f"zoom {tab.zoom()} not capped at 3")
    page.keyboard.down("Control")
    for _ in range(40):
        page.mouse.wheel(0, 100)
    page.keyboard.up("Control")
    wait_until(lambda: tab.zoom() == pytest.approx(0.2), message=f"zoom {tab.zoom()} not capped at 0.2")
    assert page.evaluate("[window.visualViewport.scale, window.devicePixelRatio, window.outerWidth]") == page_scale
    # Without Ctrl the wheel scrolls the view and keeps the zoom.
    page.mouse.wheel(0, 100)
    page.wait_for_timeout(100)
    assert tab.zoom() == pytest.approx(0.2)


def test_P_07_group_title_bar_moves_what_lies_completely_inside(start_gateway, open_tab):
    group = node("Group", "box", -420, 60, width=560, height=320, color="#3d7cc9")
    inside = node("Print Message", "Inside", -380, 140, fields={"Level": 3, "Source": None, "Message": "in"})
    overlap = node("Print Message", "Overlap", 40, 160, fields={"Level": 3, "Source": None, "Message": "half"})
    gateway = start_gateway(lambda trees: write_tree(trees, "p07", [root(0, -150), group, inside, overlap], []))
    tab = open_tab(gateway, wait_tree=False)
    tab.load("p07")
    zoom = tab.zoom()
    before = {key: tab.world_rect(item["id"]) for key, item in (("group", group), ("inside", inside), ("overlap", overlap))}
    assert before["overlap"]["x"] + before["overlap"]["w"] > before["group"]["x"] + before["group"]["w"], "set-up"
    title = tab.box(f"group-title-{group['id']}")
    start = (title["x"] + title["width"] - 60, title["y"] + title["height"] / 2)
    tab.drag(start, (start[0] + 100 * zoom, start[1] + 80 * zoom))
    wait_until(lambda: tab.world_rect(group["id"])["y"] != before["group"]["y"], message="the group did not move")
    after = {key: tab.world_rect(item["id"]) for key, item in (("group", group), ("inside", inside), ("overlap", overlap))}
    for key in ("group", "inside"):
        assert after[key]["x"] - before[key]["x"] == pytest.approx(100, abs=1)
        assert after[key]["y"] - before[key]["y"] == pytest.approx(80, abs=1)
    assert (after["overlap"]["x"], after["overlap"]["y"]) == (before["overlap"]["x"], before["overlap"]["y"])
    # The gateway has the positions: a new tab shows them.
    other = open_tab(gateway)
    wait_until(lambda: other.world_rect(inside["id"])["x"] == after["inside"]["x"], message="not stored")
    assert other.world_rect(overlap["id"])["x"] == before["overlap"]["x"]


NO_SPLIT = """id => {
    const element = document.querySelector(`[data-testid="note-text-${id}"]`);
    const text = element.firstChild;
    const broken = [];
    for (const match of text.textContent.matchAll(/\\S+/g)) {
        const range = document.createRange();
        range.setStart(text, match.index);
        range.setEnd(text, match.index + match[0].length);
        const tops = new Set([...range.getClientRects()].map(r => Math.round(r.top)));
        if (tops.size > 1) broken.push(match[0]);
    }
    return {broken, fits: element.scrollWidth <= element.clientWidth + 1 && element.scrollHeight <= element.clientHeight + 1,
            size: parseFloat(getComputedStyle(element).fontSize)};
}"""


def test_P_08_note_font_shrinks_as_text_is_typed(start_gateway, open_tab):
    note = node("Notes", "Notes", -200, -40, text="Charging logic", width=260, height=120)
    gateway = start_gateway(lambda trees: write_tree(trees, "p08", [root(0, 160), note], []))
    tab = open_tab(gateway, wait_tree=False)
    tab.load("p08")
    first = tab.page.evaluate(NO_SPLIT, note["id"])
    assert first["fits"] and not first["broken"]
    tab.tid(f"note-text-{note['id']}").click()
    editor = tab.tid(f"note-editor-{note['id']}")
    editor.wait_for()
    tab.page.keyboard.press("End")
    tab.page.keyboard.type(": retries three times, then reports an extraordinarily long explanation to the operator")
    tab.page.keyboard.press("Escape")
    text = tab.tid(f"note-text-{note['id']}")
    wait_until(lambda: (text.text_content() or "").endswith("to the operator"), message="the text was not taken")
    tab.page.wait_for_timeout(100)
    result = tab.page.evaluate(NO_SPLIT, note["id"])
    assert result["size"] < first["size"], (first, result)
    assert result["fits"], result
    assert not result["broken"], result
    # The text reached the gateway.
    other = open_tab(gateway)
    wait_until(lambda: "extraordinarily" in (other.tid(f"note-text-{note['id']}").text_content() or ""))


def test_P_10_double_editor_accepts_nan_and_comma(start_gateway, open_tab):
    move = node("MoveTo", "Move To", -100, 200, fields={"x": 0.0, "y": 0.0, "speed": 5.0, "mode": ["walk", "run"],
                                                         "avoid_obstacles": True})
    gateway = start_gateway(lambda trees: write_tree(trees, "p10", [root(), move], []))
    tab = open_tab(gateway, wait_tree=False)
    tab.load("p10")
    other = open_tab(gateway)
    field = tab.tid(f"field-{move['id']}-x")
    mirror = other.tid(f"field-{move['id']}-x")
    field.fill("nan")
    field.press("Enter")
    wait_until(lambda: field.input_value() == "nan" and mirror.input_value() == "nan",
               message=f"nan: {field.input_value()!r} / {mirror.input_value()!r}")
    assert "invalid" not in (field.get_attribute("class") or "")
    field.fill("1,5")
    field.press("Enter")
    wait_until(lambda: field.input_value() == "1.5" and mirror.input_value() == "1.5",
               message=f"1,5: {field.input_value()!r} / {mirror.input_value()!r}")
    assert not tab.errors


def test_P_11_a_node_moved_in_one_tab_follows_in_the_other(start_gateway, open_tab):
    nodes = {}
    gateway = start_gateway(lambda trees: nodes.update(zip(("root", "seq", "leaves"), _sequence_tree(
        trees, "p11", ["A"], [-200.0]))))
    first = open_tab(gateway, wait_tree=False)
    first.load("p11")
    second = open_tab(gateway)
    leaf = nodes["leaves"][0]["id"]
    before = second.world_rect(leaf)
    zoom = first.zoom()
    start = first.center(f"title-{leaf}")
    first.drag(start, (start[0] + 150 * zoom, start[1] + 60 * zoom))
    moved = first.world_rect(leaf)
    assert moved["x"] - before["x"] == pytest.approx(150, abs=1)
    wait_until(lambda: (second.world_rect(leaf)["x"], second.world_rect(leaf)["y"]) == (moved["x"], moved["y"]),
               message=f"the other tab shows {second.world_rect(leaf)}, not {moved}")
