"""Forms of the browser protocol (contract 5.1, design 10.3) that are not a document's.

Every value goes through :mod:`bt_server_core.values`. A blackboard value whose wire text is
longer than 64 KB (in UTF-8) is sent as its first 64 KB with ``"truncated": true`` (design 7.6);
``ask entry_value`` returns all of it.
"""

from __future__ import annotations

import json
import math
from typing import Any

from bt_server_core.treelog import time_text
from bt_server_core.values import to_wire

from .link import LinkRecord, LinkStatus

__all__ = ["PREVIEW_LIMIT", "dumps", "entry_to_wire", "log_line", "st_body", "value_to_wire"]

#: Longest wire text of a blackboard value sent without being asked for (design 7.6).
PREVIEW_LIMIT = 64 * 1024


def value_to_wire(type_name: str, value: Any) -> Any:
    """The whole wire form of an entry's value; an Object entry's value is its repr text."""
    if type_name == "Object":
        return value if isinstance(value, str) else repr(value)
    return to_wire(type_name, value)


def entry_to_wire(entry: dict, freshness: str | None = None, age: float | None = None,
                  problem: str | None = None) -> dict:
    """An entry dict (decoded value) in wire form, its value shortened to 64 KB. ``problem``:
    why its source cannot feed it, as the executor reports it."""
    wire = value_to_wire(entry["type"], entry.get("value"))
    truncated = False
    if isinstance(wire, str) and len(wire) > PREVIEW_LIMIT // 4:  # beyond 16 K characters it may exceed 64 KB
        encoded = wire.encode("utf-8", "surrogatepass")
        if len(encoded) > PREVIEW_LIMIT:
            truncated = True
            wire = encoded[:PREVIEW_LIMIT].decode("utf-8", "ignore")
    return {
        "id": entry["id"],
        "name": entry["name"],
        "type": entry["type"],
        "value": wire,
        "reset": bool(entry.get("reset", False)),
        "source": entry.get("source"),
        "freshness": freshness,
        "age": age,
        "truncated": truncated,
        "problem": problem,
    }


def st_body(name: str, status: LinkStatus | None, starting: bool) -> dict:
    """The ``st`` message of an executor: state, counts and one digit per node."""
    if status is None:
        return {"t": "st", "exec": name, "run_id": None, "state": 0, "loop": 0, "cancelling": 0, "unsettled": 0,
                "outcome": 0, "message": "", "v": "", "starting": starting}
    return {
        "t": "st",
        "exec": name,
        "run_id": status.run_id,
        "state": status.state,
        "loop": status.loop,
        "cancelling": status.cancelling,
        "unsettled": status.unsettled,
        "outcome": status.outcome,
        "message": status.message,
        "v": "".join(chr(48 + min(byte, 9)) for byte in status.statuses),
        "starting": starting,
    }


def log_line(record: LinkRecord) -> list:
    """``[time_text, level, text]``: a log line for a browser."""
    return [time_text(record.stamp), record.level, record.text]


def _finite(data: Any) -> Any:
    """``data`` with non-finite floats as their text (a browser's JSON parser refuses NaN)."""
    if isinstance(data, float) and not math.isfinite(data):
        return repr(data)
    if isinstance(data, dict):
        return {key: _finite(value) for key, value in data.items()}
    if isinstance(data, (list, tuple)):
        return [_finite(value) for value in data]
    return data


def dumps(message: dict) -> str:
    """A message as JSON text a browser can parse."""
    try:
        return json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except ValueError:
        return json.dumps(_finite(message), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
