"""A browser-shaped session through the WebSocket, end to end against LocalLink: hello, attach,
add nodes, connect, Execute; statuses, log lines and the result arrive."""

from __future__ import annotations

from gateway_helpers import Harness


async def test_browser_session_end_to_end(gw: Harness) -> None:
    browser = await gw.browser()
    assert browser.snapshot["exec"] == "local"
    assert browser.snapshot["online"] is True
    assert any(item["type"] == "Hold" for item in browser.snapshot["catalog"])
    assert not any(item["type"] == "CallAction" for item in browser.snapshot["catalog"])

    root = await gw.new_tree(browser, "robot")
    snapshot = await browser.wait_for(lambda m: m["t"] == "snapshot" and m["root"] == "robot")
    assert snapshot["docs"]["robot"]["nodes"][0]["type"] == "Root"
    sequence = await gw.add(browser, "robot", "Sequence", root, memory=True, x=0, y=100)
    first = await gw.add(browser, "robot", "Print Message", sequence, x=-200, y=200,
                         fields={"Message": "hello", "Source": None, "Level": 4})
    second = await gw.add(browser, "robot", "Hold", sequence, x=200, y=200, fields={"Seconds": "0.3", "Label": "b"})
    assert gw.doc("robot").children(sequence) == [first, second]
    op = await browser.wait_for(lambda m: m["t"] == "op" and m["op"]["op"] == "conn.add" and m["op"]["child"] == second)
    assert op["by"] == browser.client_id

    start = browser.mark()
    reply = await browser.request("cmd", name="execute")
    assert reply["t"] == "ok", reply
    run = await browser.wait_for(lambda m: m["t"] == "run" and m["event"] == "started", start=start)
    assert run["own"] is True and run["bundle"] is None
    assert len(run["node_paths"]) == 4
    running = await browser.wait_for(lambda m: m["t"] == "st" and m["state"] == 1 and "1" in m["v"], start=start)
    assert len(running["v"]) == 4
    ended = await browser.wait_for(lambda m: m["t"] == "run" and m["event"] == "ended", start=start)
    assert ended["outcome"] == 1
    final = await browser.wait_for(lambda m: m["t"] == "st" and m["state"] == 0 and m["v"] == "2222", start=start)
    assert final["outcome"] == 1
    logs = await browser.wait_for(lambda m: m["t"] == "log" and any("Hold" in line[2] for line in m["lines"]),
                                  start=start)
    assert all(len(line) == 3 for line in logs["lines"])
    texts = [line[2] for message in browser.all("log", start) for line in message["lines"]]
    assert texts[0] == "Execution started"
    assert any("finished: Succeeded" in text for text in texts)
    await browser.close()
