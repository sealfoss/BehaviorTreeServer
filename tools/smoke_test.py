"""Check a running Behavior Tree Server the way a browser uses it.

    python tools/smoke_test.py http://bt-mother-brain.local/ [--tree demo] [--executor /cell1/bt_executor]

Connects to the gateway's WebSocket (with the page's own Origin), says hello, attaches to an
executor, loads a tree from the server's trees folder, presses Execute (confirming findings, if
any), and waits for the run to end. Prints the run's log lines and exits 0 if the tree succeeded.
Needs only aiohttp.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import sys
from urllib.parse import urlsplit

import aiohttp

PROTOCOL = 1
STATES = {0: "IDLE", 1: "RUNNING", 2: "PAUSED", 3: "STOPPING", 4: "RESTARTING"}
OUTCOMES = {0: "none", 1: "succeeded", 2: "failed", 3: "stopped", 4: "error"}


class Browser:
    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self.ws = ws
        self.cids = itertools.count(1)
        self.inbox: list[dict] = []
        self.log_lines: list[list] = []

    async def send(self, message: dict) -> None:
        await self.ws.send_json(message)

    async def receive(self, timeout: float) -> dict:
        message = await self.ws.receive(timeout=timeout)
        if message.type != aiohttp.WSMsgType.TEXT:
            raise RuntimeError(f"the connection closed: {message.type.name} {message.data!r}")
        data = message.json()
        self.inbox.append(data)
        return data

    async def wait_for(self, predicate, timeout: float = 30.0) -> dict:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("no matching message from the gateway")
            data = await self.receive(remaining)
            if data.get("t") == "log":
                self.log_lines.extend(data.get("lines", []))
            if predicate(data):
                return data

    async def request(self, kind: str, timeout: float = 30.0, **body) -> dict:
        cid = next(self.cids)
        await self.send({"t": kind, "cid": cid, **body})
        return await self.wait_for(lambda m: m.get("t") in ("ok", "no") and m.get("cid") == cid, timeout)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("url", help="the page's address, such as http://bt-mother-brain.local/")
    parser.add_argument("--tree", default="demo", help="tree to load from the server's trees folder (default: demo)")
    parser.add_argument("--executor", help="executor to attach to (default: the first one the gateway knows)")
    parser.add_argument("--timeout", type=float, default=60.0, help="seconds to wait for the run to end")
    args = parser.parse_args()

    parts = urlsplit(args.url)
    origin = f"{parts.scheme}://{parts.netloc}"
    ws_url = f"{'wss' if parts.scheme == 'https' else 'ws'}://{parts.netloc}/ws"
    async with aiohttp.ClientSession() as http:
        async with http.get(args.url) as response:
            page = await response.text()
            print(f"GET {args.url}: {response.status}, {len(page)} bytes")
            if response.status != 200 or "<html" not in page.lower():
                print("the page did not load")
                return 1
        async with http.ws_connect(ws_url, origin=origin, autoping=True) as ws:
            browser = Browser(ws)
            await browser.send({"t": "hello", "protocol": PROTOCOL})
            welcome = await browser.wait_for(lambda m: m.get("t") in ("welcome", "reload"))
            if welcome["t"] != "welcome":
                print("the gateway asked the page to reload: protocol mismatch")
                return 1
            names = [item["name"] for item in welcome.get("executors", [])]
            print(f"welcome: executors {names}")
            executor = args.executor or (names[0] if names else None)
            if executor is None:
                print("the gateway knows no executor")
                return 1
            await browser.send({"t": "attach", "executor": executor})
            snapshot = await browser.wait_for(lambda m: m.get("t") == "snapshot")
            print(f"attached to {executor}: online={snapshot.get('online')}, tree={snapshot.get('root')}")
            if not snapshot.get("online"):
                print("the executor is not online")
                return 1

            reply = await browser.request("file", action="load", name=args.tree)
            if reply["t"] == "no" and reply.get("question"):
                reply = await browser.request("file", action="load", name=args.tree, choice="discard")
            if reply["t"] != "ok":
                print(f"load {args.tree}: {reply.get('reason')}")
                return 1
            print(f"loaded {args.tree}")

            reply = await browser.request("cmd", name="execute")
            if reply["t"] == "no" and reply.get("findings") is not None:
                for finding in reply["findings"]:
                    print(f"  finding ({finding.get('severity')}): {finding.get('text')}")
                reply = await browser.request("cmd", name="execute", confirm=True)
            if reply["t"] != "ok":
                print(f"execute: {reply.get('reason')}")
                return 1
            print("execute: accepted")
            ended = await browser.wait_for(lambda m: m.get("t") == "run" and m.get("event") == "ended", args.timeout)
            outcome = OUTCOMES.get(ended.get("outcome"), str(ended.get("outcome")))
            print(f"log: {len(browser.log_lines)} lines, the last ones:")
            for line in browser.log_lines[-12:]:
                print(f"  {line[0]} {line[2]}")
            print(f"run ended: {outcome} {ended.get('message') or ''}".rstrip())
            return 0 if ended.get("outcome") == 1 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
