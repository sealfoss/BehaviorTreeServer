"""Browser tests of the page against a real gateway (Appendix B.13): runs.

P-05 (a page opened in the middle of a run), P-06 (Stop while a cancel is confirmed slowly),
P-09 (Zoom In on a Sub-Tree node during a run).
"""

from __future__ import annotations

import pytest

from page_helpers import node, root, wait_until, write_tree

pytestmark = pytest.mark.page

FIRST_PICTURE = """() => ({
    statuses: Object.fromEntries([...document.querySelectorAll('.node')].map(n => [n.dataset.nodeId, n.dataset.status])),
    rows: [...document.querySelectorAll('[data-testid^="bb-row-"]')].map(r => r.dataset.testid),
    values: Object.fromEntries([...document.querySelectorAll('[data-testid^="bb-value-"]')].map(v => [v.dataset.testid, v.value])),
    log: [...document.querySelectorAll('[data-testid="log-line"] .log-text')].map(l => l.textContent),
    state: document.querySelector('[data-testid="run-state"]').textContent,
})"""


def _hold_tree(trees, key, seconds=60.0):
    top = root()
    sequence = node("Sequence", "Sequence", -20, 110, memory=True)
    hold = node("PageHold", "Hold", -60, 230, fields={"Seconds": seconds})
    entry = {"name": "battery", "type": "Double", "value": 42.5, "reset": False}
    write_tree(trees, key, [top, sequence, hold], [(top, sequence), (sequence, hold)], blackboard=[entry])
    return top, sequence, hold


def test_P_05_page_opened_during_a_run_has_everything_in_the_first_picture(start_gateway, open_tab):
    nodes = {}
    gateway = start_gateway(lambda trees: nodes.update(zip(("root", "seq", "hold"), _hold_tree(trees, "p05"))))
    first = open_tab(gateway, wait_tree=False)
    first.load("p05")
    first.tid("btn-execute").click()
    hold = nodes["hold"]["id"]
    wait_until(lambda: first.status(hold) == "running", message="the run did not start")
    first.tid("tab-log").click()
    wait_until(lambda: any("Hold" in text for text in first.log_texts()), message="no log line for Hold")

    late = open_tab(gateway)  # waits only for the tree to be drawn
    picture = late.page.evaluate(FIRST_PICTURE)
    assert picture["statuses"][hold] == "running", picture
    assert picture["statuses"][nodes["seq"]["id"]] == "running"
    assert picture["statuses"][nodes["root"]["id"]] == "running"
    assert "RUNNING" in picture["state"]
    assert "bb-row-battery" in picture["rows"]
    assert picture["values"]["bb-value-battery"] == "42.5"
    assert picture["log"], picture
    assert any("Execution started" in text for text in picture["log"])
    assert any("'Hold'" in text and "Running" in text for text in picture["log"])
    first.tid("btn-stop").click()
    wait_until(lambda: "IDLE" in first.run_state(), message=first.run_state())
    assert not first.errors and not late.errors


def test_P_06_stop_while_a_cancel_is_confirmed_slowly(start_gateway, open_tab):
    top = root()
    sequence = node("Sequence", "Sequence", -20, 110, memory=True)
    slow = node("SlowCancel", "Slow", -60, 230, fields={"CancelSeconds": 4.0})
    gateway = start_gateway(lambda trees: write_tree(trees, "p06", [top, sequence, slow],
                                                     [(top, sequence), (sequence, slow)]))
    tab = open_tab(gateway, wait_tree=False)
    tab.load("p06")
    tab.tid("btn-execute").click()
    wait_until(lambda: tab.status(slow["id"]) == "running", message="the run did not start")
    tab.tid("btn-stop").click()
    state = wait_until(lambda: text if "STOPPING" in (text := tab.run_state()) else None,
                       timeout=3, message=f"no stopping state: {tab.run_state()!r}")
    assert "1 cancel outstanding" in state, state
    wait_until(lambda: tab.status(slow["id"]) == "cancelling", timeout=3, message=tab.status(slow["id"]))
    assert tab.tid(f"status-{slow['id']}").text_content() == "Cancelling"
    assert "STOPPING" in tab.run_state()  # still waiting for the confirmation
    wait_until(lambda: "IDLE" in tab.run_state(), timeout=10, message=f"never idle: {tab.run_state()!r}")
    wait_until(lambda: tab.status(slow["id"]) == "ready", message=tab.status(slow["id"]))
    assert not tab.errors


def test_P_09_zoom_in_on_a_sub_tree_during_a_run(start_gateway, open_tab):
    top = root()
    sub_a = node("Sub-Tree", "A", -320, 140, fields={"File": "patrol_BT.json"})
    sub_b = node("Sub-Tree", "B", 120, 140, fields={"File": "patrol_BT.json"})
    patrol_root = root()
    hold = node("PageHold", "Hold", -60, 140, fields={"Seconds": 60.0})

    def prepare(trees):
        write_tree(trees, "p09", [top, sub_a, sub_b], [(top, sub_a), (top, sub_b)])
        write_tree(trees, "patrol", [patrol_root, hold], [(patrol_root, hold)])

    gateway = start_gateway(prepare)
    tab = open_tab(gateway, wait_tree=False)
    tab.load("p09")
    tab.tid("btn-execute").click()
    wait_until(lambda: tab.status(sub_a["id"]) == "running", message="the run did not start")
    assert tab.status(sub_b["id"]) == "ready"

    tab.menu(sub_a["id"], "Zoom In")
    wait_until(lambda: "patrol" in (tab.tid("breadcrumb").text_content() or ""), message="no zoom")
    tab.tid(f"node-{hold['id']}").wait_for()
    wait_until(lambda: tab.status(hold["id"]) == "running", message=f"A's Hold shows {tab.status(hold['id'])}")
    assert tab.status(patrol_root["id"]) == "running"

    tab.tid("btn-go-back").click()
    wait_until(lambda: (tab.tid("breadcrumb").text_content() or "").startswith("p09")
               and "patrol" not in (tab.tid("breadcrumb").text_content() or ""), message="Go Back did not return")
    tab.tid(f"node-{sub_a['id']}").wait_for()
    assert tab.tid(f"node-{hold['id']}").count() == 0

    tab.menu(sub_b["id"], "Zoom In")
    tab.tid(f"node-{hold['id']}").wait_for()
    tab.page.wait_for_timeout(300)
    assert tab.status(hold["id"]) == "ready", "B's instance has not run"
    tab.tid("btn-go-back").click()
    tab.tid(f"node-{sub_b['id']}").wait_for()
    tab.tid("btn-stop").click()
    wait_until(lambda: "IDLE" in tab.run_state(), message=tab.run_state())
    assert not tab.errors
