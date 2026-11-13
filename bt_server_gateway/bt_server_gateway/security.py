"""Who may talk to the gateway (design 13).

* :func:`host_allowed`: a request is accepted only when its ``Host`` header names one of the
  gateway's ``public_names`` or the address it listens on (G-14); case does not matter and a
  port may follow.
* :func:`origin_allowed`: the WebSocket and every request that changes something must come from
  one of the gateway's own origins (G-13, G-15): the ``Origin`` header, when a browser sends
  one, must name the same host and port as ``Host``. Requests without ``Origin`` do not come
  from a web page.
* :func:`address_allowed`: ``allowed_networks``.
* :func:`allow`: the one function every operation, command, file request and question passes
  through, where logins would fit later. Today it allows everything.

The gateway sends no CORS headers.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

__all__ = ["address_allowed", "allow", "host_allowed", "host_names", "origin_allowed", "split_host"]

_DEFAULT_PORTS = {"http": 80, "https": 443}


def split_host(value: str) -> tuple[str, int | None]:
    """``("bt.x.local", 8080)`` from ``BT.X.local:8080``; IPv6 addresses in brackets. The name
    is lower case; a malformed port gives ``("", None)``."""
    text = value.strip().lower()
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            return "", None
        name, rest = text[1:end], text[end + 1:]
    elif text.count(":") == 1:
        name, _, port_text = text.partition(":")
        rest = ":" + port_text
    else:
        name, rest = text, ""
    if not rest:
        return name, None
    if not rest.startswith(":") or not rest[1:].isdigit():
        return "", None
    return name, int(rest[1:])


def host_names(public_names: Iterable[str], listen_host: str, local_address: str | None = None) -> set[str]:
    """The host names the gateway answers to: its public names, the address it listens on and,
    when it listens on every address, the address the request arrived at."""
    names = {name.strip().lower() for name in public_names if name.strip()}
    listen = listen_host.strip().lower().strip("[]")
    if listen in ("", "0.0.0.0", "::"):
        if local_address:
            names.add(local_address.lower())
    else:
        names.add(listen)
    return names


def host_allowed(host_header: str | None, names: set[str]) -> bool:
    """Whether a ``Host`` header names the gateway (G-14)."""
    if not host_header:
        return False
    name, _ = split_host(host_header)
    return bool(name) and name in names


def origin_allowed(origin: str | None, host_header: str | None, scheme: str, names: set[str]) -> bool:
    """Whether a request may change something (G-13, G-15): no ``Origin`` (not a web page), or
    an origin with the request's scheme, an allowed host and the same host and port as
    ``Host``."""
    if origin is None:
        return True
    try:
        parts = urlsplit(origin.strip())
        origin_port = parts.port
    except ValueError:
        return False
    if parts.scheme not in _DEFAULT_PORTS or parts.scheme != scheme or not parts.hostname:
        return False
    if parts.hostname.lower() not in names or not host_header:
        return False
    host_name, host_port = split_host(host_header)
    default = _DEFAULT_PORTS[parts.scheme]
    return host_name == parts.hostname.lower() and (origin_port or default) == (host_port or default)


def address_allowed(remote: str | None, networks: list[Any]) -> bool:
    """Whether the peer's address lies in ``allowed_networks`` (always, when that is empty)."""
    if not networks:
        return True
    if not remote:
        return False
    try:
        address = ipaddress.ip_address(remote.split("%")[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in network for network in networks if network.version == address.version)


def allow(request_kind: str, client: Any) -> bool:
    """Whether ``client`` may make a request of ``request_kind`` (``op``, ``cmd``, ``file``,
    ``ask``, ``upload``, ``download``). There are no logins (design 13): always True."""
    return True
