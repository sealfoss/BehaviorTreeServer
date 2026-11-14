"""Delivery to browsers (design 10.2): Appendix B.11 G-10, G-11, G-12, and the handshake."""

from __future__ import annotations

import asyncio
import json

from bt_server_gateway.client import LOG_QUEUE, Client
from bt_server_gateway.link import LinkRun, LinkStatus
from gateway_helpers import Harness, hexid


class SlowBrowser:
    """A connection whose browser reads nothing until ``gate`` is set."""

    def __init__(self, gw: Harness) -> None:
        self.gate = asyncio.Event()
        self.sent: list[dict] = []
        self.client = Client(gw.gateway, self._send, self._close, "10.0.0.9")
        gw.gateway.connect(self.client)
        gw.gateway.attach(self.client, "local")

    async def _send(self, text: str) -> None:
        await self.gate.wait()
        self.sent.append(json.loads(text))

    async def _close(self) -> None:
        pass


async def test_hello_with_another_protocol_gets_reload(gw: Harness) -> None:
    socket = await gw.client.ws_connect("/ws")
    await socket.send_str(json.dumps({"t": "hello", "protocol": 99}))
    message = json.loads((await socket.receive()).data)
    assert message == {"t": "reload"}
    await socket.close()


async def test_presence_counts_attached_browsers(gw: Harness) -> None:
    first = await gw.browser()
    start = first.mark()
    second = await gw.browser()
    assert second.snapshot["attached"] == 2
    await first.wait_for(lambda m: m["t"] == "presence" and m["attached"] == 2, start=start)
    await second.close()
    await first.wait_for(lambda m: m["t"] == "presence" and m["attached"] == 1, start=start)


async def test_G_10_operation_lost_with_the_connection(gw: Harness) -> None:
    browser = await gw.browser()
    root = await gw.new_tree(browser, "robot")
    node = {"id": hexid(), "type": "Stop", "title": "Stop", "x": 0, "y": 0}
    await browser.send(t="op", cid=1, doc="robot", rev=gw.doc("robot").rev, op={"op": "node.add", "node": node})
    await browser.socket.close()
    again = await gw.browser()
    await gw.until(lambda: not [c for c in gw.gateway.clients.values() if c.id == browser.client_id])
    document = gw.doc("robot")
    snapshot = again.snapshot
    assert snapshot["docs"]["robot"] == document.to_wire(gw.session().board.types())  # the server's state
    assert not [m for m in again.inbox if m.get("cid") == 1]  # nothing is replayed
    assert document.has_node(root)


async def test_G_11_slow_browser_memory_is_bounded(gw: Harness) -> None:
    browser = await gw.browser()
    await gw.new_tree(browser, "robot")
    slow = SlowBrowser(gw)
    session = gw.session()
    run_id = "a" * 32
    paths = [hexid() for _ in range(300)]
    session.on_run(LinkRun(run_id, "robot", "elsewhere", None, paths))
    backlogs = []
    for number in range(1000):
        session.on_status(LinkStatus(run_id, number, 1, 0, 0, 0, 0, "", bytes([number % 4]) * 300))
        if number % 50 == 0:
            await asyncio.sleep(0)
            backlogs.append(slow.client.backlog)
    assert max(backlogs) == min(backlogs) <= 3  # does not grow
    slow.gate.set()
    await gw.until(lambda: any(m["t"] == "st" for m in slow.sent))
    await asyncio.sleep(0.1)
    statuses = [m for m in slow.sent if m["t"] == "st"]
    assert len(statuses) == 1
    assert statuses[0]["v"] == "3" * 300  # the latest statuses
    kinds = [m["t"] for m in slow.sent]
    assert kinds[0] == "snapshot" and kinds.index("run") < kinds.index("st")


async def test_G_12_browser_falls_behind_the_log(gw: Harness) -> None:
    browser = await gw.browser()
    root = await gw.new_tree(browser, "robot")
    await gw.add(browser, "robot", "Chatter", root, fields={"Lines": 12_000})
    slow = SlowBrowser(gw)
    start = browser.mark()
    assert (await browser.request("cmd", name="execute"))["t"] == "ok"
    await browser.wait_for(lambda m: m["t"] == "run" and m["event"] == "ended", start=start, timeout=15)
    session = gw.session()
    total = session._log_next
    assert total > 12_000
    assert slow.client.backlog <= LOG_QUEUE + 5
    slow.gate.set()
    await gw.until(lambda: any(m["t"] == "log" for m in slow.sent))
    first = next(m for m in slow.sent if m["t"] == "log")
    assert first["skipped"] == total - LOG_QUEUE
    assert first["first"] == total - LOG_QUEUE
    reply = await browser.request("ask", kind="log_before", before=first["first"], count=first["first"])
    assert reply["t"] == "ok" and reply["first"] == 0 and len(reply["lines"]) == first["first"]
    assert reply["lines"][0][2] == "Execution started"
