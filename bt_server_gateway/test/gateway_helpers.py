"""Helpers of the gateway's tests: browsers that speak the protocol of contract 5, a harness
around a gateway on aiohttp's test server, settings for tests."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType

from bt_server_gateway.app import GATEWAY
from bt_server_gateway.gateway import Gateway
from bt_server_gateway.settings import Settings

TIMEOUT = 5.0


def hexid() -> str:
    return uuid.uuid4().hex


class Browser:
    """One tab: a WebSocket, every message it received (``inbox``), and helpers."""

    def __init__(self, socket: Any) -> None:
        self.socket = socket
        self.inbox: list[dict] = []
        self.client_id: str | None = None
        self.snapshot: dict | None = None
        self._cid = 0
        self._arrived = asyncio.Event()
        self._reader = asyncio.get_running_loop().create_task(self._read())

    async def _read(self) -> None:
        async for message in self.socket:
            if message.type == WSMsgType.TEXT:
                self.inbox.append(json.loads(message.data))
                self._arrived.set()

    async def send(self, **message: Any) -> None:
        await self.socket.send_str(json.dumps(message))

    def mark(self) -> int:
        """The position in the inbox from which later waits look."""
        return len(self.inbox)

    async def wait_for(self, predicate: Callable[[dict], bool] | str, timeout: float = TIMEOUT, start: int = 0) -> dict:
        """The first message from ``start`` on that matches (a ``t`` value, or a predicate)."""
        test = (lambda message: message.get("t") == predicate) if isinstance(predicate, str) else predicate
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            for message in self.inbox[start:]:
                if test(message):
                    return message
            left = deadline - asyncio.get_running_loop().time()
            if left <= 0:
                raise AssertionError(f"no matching message within {timeout} s; got {[m.get('t') for m in self.inbox[start:]]}")
            self._arrived.clear()
            try:
                await asyncio.wait_for(self._arrived.wait(), left)
            except TimeoutError:
                pass

    def all(self, kind: str, start: int = 0) -> list[dict]:
        return [message for message in self.inbox[start:] if message.get("t") == kind]

    async def request(self, message_type: str, timeout: float = TIMEOUT, **fields: Any) -> dict:
        """Send a request with a new ``cid``; its ``ok`` or ``no``."""
        self._cid += 1
        cid = self._cid
        await self.send(t=message_type, cid=cid, **fields)
        return await self.wait_for(lambda m: m.get("t") in ("ok", "no") and m.get("cid") == cid, timeout)

    async def close(self) -> None:
        await self.socket.close()
        self._reader.cancel()


class Harness:
    """A gateway on a test server."""

    def __init__(self, client: Any, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        self.gateway: Gateway = client.server.app[GATEWAY]

    async def browser(self, executor: str | None = "local", **headers: str) -> Browser:
        socket = await self.client.ws_connect("/ws", headers=headers or None)
        browser = Browser(socket)
        await browser.send(t="hello", protocol=1)
        welcome = await browser.wait_for("welcome")
        browser.client_id = welcome["client"]
        if executor is not None:
            start = browser.mark()
            await browser.send(t="attach", executor=executor)
            browser.snapshot = await browser.wait_for("snapshot", start=start)
        return browser

    def doc(self, key: str):
        return self.gateway.docs.get(key)

    def session(self, name: str = "local"):
        return self.gateway.sessions[name]

    async def new_tree(self, browser: Browser, name: str, **extra: Any) -> str:
        """New tree ``name``; returns its Root's id."""
        reply = await browser.request("file", action="new", name=name, **extra)
        assert reply["t"] == "ok", reply
        document = self.doc(name)
        return document.root_id

    async def op(self, browser: Browser, doc: str, op: dict, rev: int | None = None) -> dict:
        return await browser.request("op", doc=doc, rev=self.doc(doc).rev if rev is None else rev, op=op)

    async def add(self, browser: Browser, doc: str, node_type: str, parent: str | None = None, *, x: float = 0.0,
                  y: float = 0.0, **data: Any) -> str:
        """Add a node (wire form) and connect it below ``parent``; returns its id."""
        node_id = hexid()
        node = {"id": node_id, "type": node_type, "title": data.pop("title", node_type), "x": x, "y": y, **data}
        reply = await self.op(browser, doc, {"op": "node.add", "node": node})
        assert reply["t"] == "ok", reply
        if parent is not None:
            reply = await self.op(browser, doc, {"op": "conn.add", "parent": parent, "child": node_id})
            assert reply["t"] == "ok", reply
        return node_id

    async def bb_add(self, browser: Browser, name: str, type_name: str, value: Any, **extra: Any) -> str:
        entry_id = hexid()
        reply = await browser.request("op", doc="@blackboard", rev=0, op={
            "op": "bb.add", "entry": {"id": entry_id, "name": name, "type": type_name, "value": value,
                                      "reset": False, **extra}})
        assert reply["t"] == "ok", reply
        return entry_id

    async def until(self, condition: Callable[[], bool], timeout: float = TIMEOUT) -> None:
        """Wait until ``condition()`` is true (an exception counts as false)."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            try:
                if condition():
                    return
            except (KeyError, OSError, ValueError, TypeError):
                pass
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("the condition did not come true in time")
            await asyncio.sleep(0.01)


def make_settings(trees: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "trees_dir": trees, "link": "local", "http_host": "127.0.0.1", "http_port": 8091,
        "public_names": ["bt.testhost.local", "bt-testhost.local"], "leaf_modules": ["gw_test_leaves"],
    }
    values.update(overrides)
    return Settings(**values)


def tree_file() -> bytes:
    """A small tree file (an upload)."""
    return json.dumps({"format": "behavior_tree_widget", "version": 1, "config": {}, "connections": [],
                       "nodes": [{"id": "a" * 32, "type": "Root", "title": "Root", "x": 0, "y": 0,
                                  "composite": "Sequence", "memory": True}]}).encode()
