"""Helpers of the page tests (Appendix B.13): a real gateway process, tree files written before
it starts, a headless browser, and a driver for the page's canvas and panels.

The page is driven through the ``data-testid`` attributes listed in ``web/TESTIDS.md``.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
TIMEOUT = 10.0


# ---------------------------------------------------------------------------------------------
# Tree files

def new_id() -> str:
    return uuid.uuid4().hex


def node(type_name: str, title: str | None = None, x: float = 0.0, y: float = 0.0, **data: Any) -> dict:
    """A node in tree-file form; ``fields`` and other keys go in ``data``."""
    return {"id": new_id(), "type": type_name, "title": title or type_name, "x": float(x), "y": float(y), **data}


def root(x: float = 0.0, y: float = 0.0, composite: str = "Sequence") -> dict:
    return node("Root", "Root", x, y, composite=composite, memory=True)


def write_tree(trees_dir: Path, key: str, nodes: list[dict], connections: list[tuple[dict, dict]],
               config: dict | None = None, blackboard: list[dict] | None = None) -> None:
    """``<key>_BT.json`` (and ``<key>_BB.json`` with ``blackboard``), the widget's format."""
    path = trees_dir / f"{key}_BT.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "format": "behavior_tree_widget", "version": 1,
        "config": {"tick_interval_ms": 100, "repeat": False, "restore_blackboard": False, "default_memory": True,
                   **(config or {})},
        "view": {"zoom": 1.0, "center": [0.0, 150.0]},
        "nodes": nodes,
        "connections": [{"parent": parent["id"], "child": child["id"]} for parent, child in connections],
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    if blackboard is not None:
        board = {"format": "behavior_tree_widget_blackboard", "version": 1, "blackboard": blackboard}
        (trees_dir / f"{key}_BB.json").write_text(json.dumps(board, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------------------------
# The gateway

def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def child_env(extra_paths: list[Path] = ()) -> dict[str, str]:
    env = dict(os.environ)
    paths = [str(REPO / "bt_server_core"), str(REPO / "bt_server_gateway"), str(REPO / "bt_server_executor"),
             str(HERE), *(str(path) for path in extra_paths)]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["PYTHONUNBUFFERED"] = "1"
    return env


class GatewayProcess:
    """``python -m bt_server_gateway --config <toml>`` on a free port of 127.0.0.1."""

    def __init__(self, folder: Path, link: str = "local", executors: list[str] = (),
                 leaf_modules: list[str] = ("bt_server_core.demo_leaves", "page_leaves")) -> None:
        self.folder = folder
        self.trees_dir = folder / "trees"
        self.trees_dir.mkdir(parents=True, exist_ok=True)
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}/"
        self.output = folder / "gateway.out"
        self.config = folder / "gateway.toml"
        lines = [
            f'link = "{link}"',
            'http_host = "127.0.0.1"',
            f"http_port = {self.port}",
            f'trees_dir = "{self.trees_dir.as_posix()}"',
            f"leaf_modules = {json.dumps(list(leaf_modules))}",
            f"executors = {json.dumps(list(executors))}",
            f'public_names = ["bt.{short_hostname()}.local", "bt-{short_hostname()}.local"]',
        ]
        self.config.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.proc: subprocess.Popen | None = None

    def start(self, env: dict[str, str] | None = None) -> GatewayProcess:
        with open(self.output, "ab") as handle:
            self.proc = subprocess.Popen([sys.executable, "-m", "bt_server_gateway", "--config", str(self.config)],
                                         env=env or child_env(), stdout=handle, stderr=subprocess.STDOUT,
                                         cwd=str(self.folder))
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"the gateway ended:\n{self.text()}")
            try:
                with urllib.request.urlopen(self.url, timeout=1.0) as response:
                    if response.status == 200:
                        return self
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.1)
        raise RuntimeError(f"the gateway did not answer:\n{self.text()}")

    def stop(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)

    def text(self) -> str:
        return self.output.read_text(encoding="utf-8", errors="replace") if self.output.exists() else ""

    def http(self, path: str, host: str | None = None) -> tuple[int, bytes]:
        """``GET path`` with a ``Host`` header of choice: (status, body)."""
        request = urllib.request.Request(self.url.rstrip("/") + path, headers={"Host": host} if host else {})
        try:
            with urllib.request.urlopen(request, timeout=5.0) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()


def short_hostname() -> str:
    return socket.gethostname().split(".")[0].lower()


# ---------------------------------------------------------------------------------------------
# The browser

def launch_browser(playwright: Any) -> Any:
    """Chromium as Playwright installed it (new headless mode first), else the installed Edge or
    Chrome; None when there is none."""
    attempts = [{"channel": "chromium"}, {}, {"channel": "msedge"}, {"channel": "chrome"}]
    for options in attempts:
        try:
            return playwright.chromium.launch(headless=True, **options)
        except Exception:  # noqa: BLE001 - not installed: try the next
            continue
    return None


def wait_until(condition: Callable[[], Any], timeout: float = TIMEOUT, message: str = "") -> Any:
    """Poll ``condition`` until it gives a true value (returned)."""
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = condition()
        if last:
            return last
        time.sleep(0.05)
    raise AssertionError(message or f"the condition did not come true within {timeout} s (last value {last!r})")


def slug(label: str) -> str:
    """The test id suffix of a menu item (web/TESTIDS.md)."""
    label = re.sub(r"\.\.\.$", "", label)
    label = re.sub(r"\s*\(.*\)$", "", label).strip()
    return re.sub(r"[^A-Za-z0-9]+", "-", label)


class PageDriver:
    """One tab of the page."""

    def __init__(self, page: Any, gateway: GatewayProcess) -> None:
        self.page = page
        self.gateway = gateway
        self.errors: list[str] = []
        page.on("pageerror", lambda error: self.errors.append(str(error)))

    # ------------------------------------------------------------------ loading
    def open(self, executor: str | None = None, wait_tree: bool = True) -> PageDriver:
        url = self.gateway.url + (f"#exec={executor}" if executor else "")
        self.page.goto(url)
        self.page.wait_for_selector('[data-testid="connection-state"].online', timeout=TIMEOUT * 1000)
        if wait_tree:
            self.page.wait_for_selector('[data-type="Root"]', timeout=TIMEOUT * 1000)
        return self

    def tid(self, test_id: str) -> Any:
        return self.page.locator(f'[data-testid="{test_id}"]')

    def file_dialog(self, button: str, name: str) -> None:
        """New or Load through the server-side file dialog; answers an unsaved question with Discard."""
        self.tid(button).click()
        self.tid("file-name").fill(name)
        self.tid("btn-file-ok").click()
        self.page.wait_for_timeout(300)
        if self.tid("dialog-unsaved").count():
            self.tid("dialog-btn-discard").click()
        self.page.wait_for_selector("[data-testid=dialog-files]", state="detached", timeout=TIMEOUT * 1000)

    def load(self, name: str) -> None:
        self.file_dialog("btn-load", name)
        wait_until(lambda: name in (self.tid("breadcrumb").text_content() or ""), message=f"{name} was not loaded")
        self.page.wait_for_selector('[data-type="Root"]', timeout=TIMEOUT * 1000)

    # ------------------------------------------------------------------ nodes
    def node_ids(self, type_name: str | None = None) -> list[str]:
        selector = f'.node[data-type="{type_name}"]' if type_name else "[data-node-id].node"
        return self.page.eval_on_selector_all(selector, "nodes => nodes.map(n => n.dataset.nodeId)")

    def id_of(self, title: str) -> str:
        """The id of the node whose title starts with ``title``."""
        found = self.page.evaluate(
            "title => [...document.querySelectorAll('.node')].find(n => (n.querySelector('.title')?.textContent ?? '')"
            ".startsWith(title))?.dataset.nodeId", title)
        assert found, f"no node titled {title!r}"
        return found

    def box(self, test_id: str) -> dict:
        box = self.tid(test_id).first.bounding_box()
        assert box is not None, f"{test_id} is not shown"
        return box

    def center(self, test_id: str) -> tuple[float, float]:
        box = self.box(test_id)
        return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    def drag(self, start: tuple[float, float], end: tuple[float, float], steps: int = 12) -> None:
        mouse = self.page.mouse
        mouse.move(*start)
        mouse.down()
        mouse.move(start[0] + (end[0] - start[0]) * 0.1, start[1] + (end[1] - start[1]) * 0.1, steps=3)
        mouse.move(*end, steps=steps)
        mouse.up()

    def world_rect(self, node_id: str) -> dict:
        """A node's rectangle in world (tree file) coordinates: its position and drawn size."""
        return self.page.evaluate(
            """id => { const e = document.querySelector(`[data-testid="node-${id}"]`);
                 const m = /translate\\(([-\\d.e]+)px, ([-\\d.e]+)px\\)/.exec(e.style.transform);
                 return {x: Number(m[1]), y: Number(m[2]), w: e.offsetWidth, h: e.offsetHeight}; }""", node_id)

    def add_node(self, label: str, at: tuple[float, float]) -> str:
        """Right-click the canvas at a screen point and choose ``label``; the new node's id."""
        before = set(self.node_ids())
        self.page.mouse.click(*at, button="right")
        self.tid(f"menu-{slug(label)}").click()
        added = wait_until(lambda: set(self.node_ids()) - before, message=f"{label} was not added")
        return next(iter(added))

    def connect(self, parent: str, child: str) -> None:
        """Drag from the parent's Children label to the child's Parent label."""
        self.drag(self.center(f"children-{parent}"), self.center(f"parent-{child}"))
        wait_until(lambda: self.tid(f"conn-{parent}-{child}").count() == 1, message=f"{parent} -> {child} not connected")

    def status(self, node_id: str) -> str:
        return self.tid(f"node-{node_id}").get_attribute("data-status") or ""

    def order(self, parent: str, child: str) -> str:
        return self.tid(f"order-{parent}-{child}").text_content() or ""

    def run_state(self) -> str:
        return self.tid("run-state").text_content() or ""

    def zoom(self) -> float:
        style = self.tid("world").get_attribute("style") or ""
        match = re.search(r"scale\(([-\d.e]+)\)", style)
        assert match, style
        return float(match.group(1))

    def log_texts(self) -> list[str]:
        return self.page.eval_on_selector_all('[data-testid="log-line"] .log-text', "lines => lines.map(l => l.textContent)")

    def menu(self, node_id: str, label: str) -> None:
        """Right-click a node's title and choose ``label``."""
        x, y = self.center(f"title-{node_id}")
        self.page.mouse.click(x, y, button="right")
        self.tid(f"menu-{slug(label)}").click()
