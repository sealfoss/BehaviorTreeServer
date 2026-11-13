"""The gateway's settings (design 9.1, contract 6), read from a TOML file with ``tomllib`` so the
gateway also starts without ROS.

| Setting | Default |
|---|---|
| ``link`` | ``"ros"`` (or ``"local"``: the engine inside the gateway, :mod:`.local_link`) |
| ``http_host`` | ``"127.0.0.1"``: the LAN address to listen on |
| ``http_port`` | 80 |
| ``public_names`` | ``["bt.{hostname}.local", "bt-{hostname}.local", "{hostname}.local"]`` |
| ``executors`` | ``[]``: executors that have a session even while they are down |
| ``allowed_networks`` | ``[]``: address ranges that may connect; empty allows all |
| ``trees_dir`` | required: the folder of all tree files |
| ``tls_cert``, ``tls_key`` | unset: set both to serve HTTPS |
| ``local_log_dir`` | ``<trees_dir>/.logs``: LocalLink's tree logs |
| ``local_log_keep_days`` | 30: LocalLink deletes older tree logs |
| ``leaf_modules`` | ``[]``: LocalLink's custom leaf modules |
| ``state_dir`` | ``<trees_dir>/.gateway``: catalog cache and session memory |

``{hostname}`` in ``public_names`` stands for the short host name. ``~`` in folders is expanded;
relative folders are relative to the settings file.
"""

from __future__ import annotations

import ipaddress
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .link import LinkFactory

__all__ = ["DEFAULT_PUBLIC_NAMES", "Settings", "SettingsError", "load_settings", "make_link_factory", "short_hostname"]

DEFAULT_PUBLIC_NAMES = ("bt.{hostname}.local", "bt-{hostname}.local", "{hostname}.local")


class SettingsError(ValueError):
    """The settings file is missing, not TOML, or holds an invalid setting."""


def short_hostname() -> str:
    """The host name without its domain: ``mother-brain`` for ``mother-brain.lan``."""
    return socket.gethostname().split(".")[0]


@dataclass
class Settings:
    """The gateway's settings (see the module docs). ``public_names`` holds the names with
    ``{hostname}`` filled in."""

    trees_dir: Path
    link: str = "ros"
    http_host: str = "127.0.0.1"
    http_port: int = 80
    public_names: list[str] = field(default_factory=lambda: [name.format(hostname=short_hostname())
                                                             for name in DEFAULT_PUBLIC_NAMES])
    executors: list[str] = field(default_factory=list)
    allowed_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = field(default_factory=list)
    tls_cert: Path | None = None
    tls_key: Path | None = None
    local_log_dir: Path | None = None
    local_log_keep_days: float = 30.0
    leaf_modules: list[str] = field(default_factory=list)
    state_dir: Path | None = None

    def __post_init__(self) -> None:
        self.trees_dir = Path(self.trees_dir).expanduser()
        if self.local_log_dir is None:
            self.local_log_dir = self.trees_dir / ".logs"
        if self.state_dir is None:
            self.state_dir = self.trees_dir / ".gateway"

    @property
    def tls(self) -> bool:
        return self.tls_cert is not None and self.tls_key is not None


def _text_list(data: dict, key: str) -> list[str]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SettingsError(f"'{key}' must be a list of texts")
    return list(value)


def _folder(value: Any, key: str, base: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise SettingsError(f"'{key}' must name a folder")
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path)


def load_settings(path: str | Path) -> Settings:
    """Read the settings file.

    Raises:
        SettingsError: the file cannot be read, is not TOML, or a setting is invalid.
    """
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise SettingsError(f"cannot read the settings file '{path}': {error.strerror or error}") from error
    except tomllib.TOMLDecodeError as error:
        raise SettingsError(f"'{path}' is not a TOML file: {error}") from error
    return settings_from_dict(data, path.parent)


def settings_from_dict(data: dict, base: Path | None = None) -> Settings:
    """Settings from parsed TOML (``base``: the folder relative folders are relative to).

    Raises:
        SettingsError: a setting is missing or invalid.
    """
    base = base or Path.cwd()
    known = {"link", "http_host", "http_port", "public_names", "executors", "allowed_networks", "trees_dir",
             "tls_cert", "tls_key", "local_log_dir", "local_log_keep_days", "leaf_modules", "state_dir"}
    unknown = sorted(set(data) - known)
    if unknown:
        raise SettingsError(f"unknown settings: {', '.join(unknown)}")
    if "trees_dir" not in data:
        raise SettingsError("'trees_dir' is not set")
    link = data.get("link", "ros")
    if link not in ("ros", "local"):
        raise SettingsError("'link' is 'ros' or 'local'")
    host = data.get("http_host", "127.0.0.1")
    if not isinstance(host, str) or not host.strip():
        raise SettingsError("'http_host' must be an address")
    port = data.get("http_port", 80)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        raise SettingsError("'http_port' must be a port number")
    hostname = short_hostname()
    names = [name.format(hostname=hostname) for name in _text_list(data, "public_names")] \
        if "public_names" in data else [name.format(hostname=hostname) for name in DEFAULT_PUBLIC_NAMES]
    try:
        networks = [ipaddress.ip_network(text, strict=False) for text in _text_list(data, "allowed_networks")]
    except ValueError as error:
        raise SettingsError(f"'allowed_networks': {error}") from error
    cert = _folder(data["tls_cert"], "tls_cert", base) if "tls_cert" in data else None
    key = _folder(data["tls_key"], "tls_key", base) if "tls_key" in data else None
    if (cert is None) != (key is None):
        raise SettingsError("set both 'tls_cert' and 'tls_key', or neither")
    keep = data.get("local_log_keep_days", 30.0)
    if isinstance(keep, bool) or not isinstance(keep, (int, float)) or keep < 0:
        raise SettingsError("'local_log_keep_days' must be a number of days, 0 or more")
    return Settings(
        trees_dir=_folder(data["trees_dir"], "trees_dir", base),
        link=link,
        http_host=host.strip(),
        http_port=port,
        public_names=names,
        executors=_text_list(data, "executors"),
        allowed_networks=networks,
        tls_cert=cert,
        tls_key=key,
        local_log_dir=_folder(data["local_log_dir"], "local_log_dir", base) if "local_log_dir" in data else None,
        local_log_keep_days=float(keep),
        leaf_modules=_text_list(data, "leaf_modules"),
        state_dir=_folder(data["state_dir"], "state_dir", base) if "state_dir" in data else None,
    )


def make_link_factory(settings: Settings) -> LinkFactory:
    """The link factory the settings name: ``ros`` imports ``ros_link.RosLinkFactory`` only now,
    so the gateway starts without ROS when ``link = "local"``."""
    if settings.link == "ros":
        from .ros_link import RosLinkFactory  # noqa: PLC0415 - needs rclpy; imported only when used

        return RosLinkFactory(settings)
    from .local_link import LocalLinkFactory  # noqa: PLC0415

    return LocalLinkFactory(settings)
