"""Fixtures of the page tests: a headless browser for the session, a gateway process per test
and tabs on it. Without Playwright or a Chromium-family browser the tests are skipped."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from page_helpers import GatewayProcess, PageDriver, launch_browser

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - depends on the machine
    sync_playwright = None


@pytest.fixture(scope="session")
def bt_browser() -> Iterator[Any]:
    """A Chromium-family browser, headless (skips when there is none)."""
    if sync_playwright is None:
        pytest.skip("Playwright is not installed")
    manager = sync_playwright().start()
    browser = launch_browser(manager)
    if browser is None:
        manager.stop()
        pytest.skip("no Chromium, Edge or Chrome for Playwright")
    yield browser
    browser.close()
    manager.stop()


@pytest.fixture
def start_gateway(tmp_path: Path) -> Iterator[Callable[..., GatewayProcess]]:
    """``start_gateway(prepare=None, env=None, **options)`` -> a running gateway (stopped after
    the test). ``prepare(trees_dir)`` writes tree files before it starts; ``env`` replaces the
    process environment (ROS settings)."""
    started: list[GatewayProcess] = []

    def start(prepare: Callable[[Path], None] | None = None, env: dict[str, str] | None = None,
              **options: Any) -> GatewayProcess:
        gateway = GatewayProcess(tmp_path / f"gw{len(started)}", **options)
        if prepare is not None:
            prepare(gateway.trees_dir)
        started.append(gateway)
        return gateway.start(env)

    yield start
    for gateway in started:
        gateway.stop()


@pytest.fixture
def open_tab(bt_browser: Any) -> Iterator[Callable[..., PageDriver]]:
    """``open_tab(gateway, executor=None, wait_tree=True)`` -> a new tab (its own browser
    context, clipboard access granted) showing the page."""
    contexts: list[Any] = []

    def open_page(gateway: GatewayProcess, executor: str | None = None, wait_tree: bool = True) -> PageDriver:
        context = bt_browser.new_context(viewport={"width": 1500, "height": 900},
                                         permissions=["clipboard-read", "clipboard-write"])
        contexts.append(context)
        driver = PageDriver(context.new_page(), gateway)
        return driver.open(executor, wait_tree)

    yield open_page
    for context in contexts:
        context.close()
