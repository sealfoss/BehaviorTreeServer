"""Fixtures of the gateway's tests: a gateway on aiohttp's test server (LocalLink unless a test
brings its own link factory)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from bt_server_gateway.app import create_app
from bt_server_gateway.settings import Settings
from gateway_helpers import Harness, make_settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path / "trees")


@pytest.fixture
def start_gateway(aiohttp_client: Any) -> Callable:
    """``await start_gateway(settings, factory=None)`` -> :class:`Harness`."""

    async def start(settings: Settings, factory: Any = None) -> Harness:
        client = await aiohttp_client(create_app(settings, factory))
        return Harness(client, settings)

    return start


@pytest.fixture
async def gw(start_gateway: Callable, settings: Settings) -> Harness:
    return await start_gateway(settings)
