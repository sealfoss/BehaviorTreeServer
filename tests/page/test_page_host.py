"""P-12 in part (the rest is done by hand from other machines): the page loads by the server's
address, and by the published names, which reach the gateway as the ``Host`` header."""

from __future__ import annotations

import re

import pytest

from page_helpers import root, short_hostname, write_tree

pytestmark = pytest.mark.page


def test_P_12_page_by_address_and_by_published_names(start_gateway, open_tab):
    gateway = start_gateway(lambda trees: write_tree(trees, "p12", [root()], []))
    for host in (f"127.0.0.1:{gateway.port}", f"bt-{short_hostname()}.local", f"bt.{short_hostname()}.local:{gateway.port}"):
        status, body = gateway.http("/", host=host)
        assert status == 200, (host, status)
        script = re.search(rb'src="(/assets/[^"]+\.js)"', body)
        assert script, body[:300]
        assert gateway.http(script.group(1).decode(), host=host)[0] == 200
    assert gateway.http("/", host="evil.example")[0] == 403
    tab = open_tab(gateway, wait_tree=False)  # by the address
    tab.load("p12")
    assert not tab.errors
