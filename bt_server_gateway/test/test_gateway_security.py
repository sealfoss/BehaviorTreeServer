"""Security (design 13): Appendix B.11 G-13, G-14, G-15, G-16; addresses, caching, settings."""

from __future__ import annotations

import ipaddress

import pytest
from aiohttp import FormData, WSServerHandshakeError

from bt_server_gateway.app import STATIC_DIR
from bt_server_gateway.security import host_allowed, host_names, origin_allowed, split_host
from bt_server_gateway.settings import SettingsError, load_settings, make_link_factory, short_hostname
from bt_server_gateway.local_link import LocalLinkFactory
from gateway_helpers import Harness, make_settings, tree_file


async def test_G_13_websocket_from_another_origin_is_refused(gw: Harness) -> None:
    with pytest.raises(WSServerHandshakeError) as refused:
        await gw.client.ws_connect("/ws", headers={"Origin": "http://evil.example"})
    assert refused.value.status == 403
    port = gw.client.port
    socket = await gw.client.ws_connect("/ws", headers={"Origin": f"http://127.0.0.1:{port}"})
    await socket.close()
    socket = await gw.client.ws_connect("/ws", headers={"Origin": f"http://bt.testhost.local:{port}",
                                                        "Host": f"BT.TestHost.local:{port}"})
    await socket.close()
    with pytest.raises(WSServerHandshakeError):  # same name, another port: another origin
        await gw.client.ws_connect("/ws", headers={"Origin": f"http://bt.testhost.local:{port + 1}",
                                                   "Host": f"bt.testhost.local:{port}"})


async def test_G_14_host_header_is_checked(gw: Harness) -> None:
    response = await gw.client.get("/", headers={"Host": "evil.example"})
    assert response.status == 403
    response = await gw.client.get("/", headers={"Host": f"evil.example:{gw.client.port}"})
    assert response.status == 403
    for host in ("bt.testhost.local", f"BT-TESTHOST.local:{gw.client.port}", f"127.0.0.1:{gw.client.port}"):
        response = await gw.client.get("/", headers={"Host": host})
        assert response.status in (200, 503), host
        assert response.headers["Cache-Control"] == "no-cache"
        assert "Access-Control-Allow-Origin" not in response.headers


async def test_G_15_upload_from_another_origin_is_refused(gw: Harness) -> None:
    form = FormData()
    form.add_field("file", tree_file(), filename="up_BT.json")
    response = await gw.client.post("/upload", data=form, headers={"Origin": "http://evil.example"})
    assert response.status == 403
    assert not (gw.settings.trees_dir / "up_BT.json").exists()
    form = FormData()
    form.add_field("file", tree_file(), filename="up_BT.json")
    response = await gw.client.post("/upload", data=form, headers={"Origin": f"http://127.0.0.1:{gw.client.port}"})
    assert response.status == 200
    assert (gw.settings.trees_dir / "up_BT.json").is_file()


async def test_G_16_paths_are_confined_to_the_trees_folder(gw: Harness) -> None:
    browser = await gw.browser()
    for name in ("../../etc/passwd", "/etc/passwd", "C:\\Windows\\win.ini", "..\\..\\x", ".recovery/robot"):
        reply = await browser.request("file", action="load", name=name)
        assert reply["t"] == "no", name
    response = await gw.client.get("/download/..%2F..%2Fetc%2Fpasswd")
    assert response.status == 404


async def test_allowed_networks(start_gateway, tmp_path) -> None:
    gw = await start_gateway(make_settings(tmp_path / "trees", allowed_networks=[ipaddress.ip_network("10.0.0.0/8")]))
    assert (await gw.client.get("/")).status == 403
    gw = await start_gateway(make_settings(tmp_path / "other", allowed_networks=[ipaddress.ip_network("127.0.0.0/8")]))
    assert (await gw.client.get("/")).status in (200, 503)


async def test_assets_are_cached_for_a_year(gw: Harness) -> None:
    assets = sorted((STATIC_DIR / "assets").glob("*")) if (STATIC_DIR / "assets").is_dir() else []
    if not assets:
        pytest.skip("the page is not built")
    response = await gw.client.get(f"/assets/{assets[0].name}")
    assert response.status == 200
    assert "max-age=31536000" in response.headers["Cache-Control"]
    assert (await gw.client.get("/assets/..%2F..%2Fapp.py")).status == 404


def test_host_and_origin_rules() -> None:
    names = host_names(["bt.mother-brain.local"], "192.168.68.77")
    assert split_host("[::1]:80") == ("::1", 80)
    assert host_allowed("BT.Mother-Brain.local:8091", names)
    assert host_allowed("192.168.68.77", names)
    assert not host_allowed("evil.example", names)
    assert not host_allowed(None, names)
    assert origin_allowed(None, "bt.mother-brain.local", "http", names)
    assert origin_allowed("http://bt.mother-brain.local", "bt.mother-brain.local:80", "http", names)
    assert not origin_allowed("null", "bt.mother-brain.local", "http", names)
    assert not origin_allowed("https://bt.mother-brain.local", "bt.mother-brain.local", "http", names)
    assert "10.1.2.3" in host_names([], "0.0.0.0", "10.1.2.3")


def test_settings_from_toml(tmp_path) -> None:
    path = tmp_path / "gateway.toml"
    path.write_text('link = "local"\nhttp_host = "192.168.68.77"\nhttp_port = 8091\ntrees_dir = "trees"\n'
                    'executors = ["/cell1/bt_executor"]\nallowed_networks = ["192.168.68.0/24"]\n', encoding="utf-8")
    settings = load_settings(path)
    assert settings.trees_dir == tmp_path / "trees"
    assert settings.local_log_dir == tmp_path / "trees" / ".logs"
    assert settings.state_dir == tmp_path / "trees" / ".gateway"
    assert settings.public_names == [f"bt.{short_hostname()}.local", f"bt-{short_hostname()}.local",
                                     f"{short_hostname()}.local"]
    assert settings.allowed_networks == [ipaddress.ip_network("192.168.68.0/24")]
    assert isinstance(make_link_factory(settings), LocalLinkFactory)
    path.write_text('link = "carrier pigeon"\ntrees_dir = "t"\n', encoding="utf-8")
    with pytest.raises(SettingsError):
        load_settings(path)
    path.write_text('trees_dir = "t"\nhttp_prot = 80\n', encoding="utf-8")
    with pytest.raises(SettingsError, match="http_prot"):
        load_settings(path)
