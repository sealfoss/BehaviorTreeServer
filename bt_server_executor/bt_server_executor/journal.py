"""The goal journal (design 5.6, 8.4): the action goals this executor has sent and not seen
settle, kept in a file so that a restarted executor can cancel what its predecessor left
running.

Who calls what (leaves reach the journal as ``ctx.run.journal``; ids are a 16-byte UUID, a
``unique_identifier_msgs/UUID`` or 32 hex digits)::

    journal.sent(action_name, goal_id)      # just before the goal is sent: appended to the file
    journal.cancel_requested(goal_id)       # the leaf sent a cancel; if the goal has not settled
                                            # within the cancel timeout it becomes unsettled
    journal.unsettled(goal_id)              # the goal is unsettled now (its cancel was not confirmed)
    journal.done(goal_id)                   # it settled: a result arrived, or it was rejected

``done`` may be called after the run that sent the goal has ended: a late result of an
unsettled goal settles it then (L-09).

**Unsettled goals.** A goal is unsettled when its cancel was not confirmed in time, when the
run that sent it ended while it was open, and when it was found unfinished in the file at
start-up. The journal sends the cancel of every unsettled goal again every
:data:`RESEND_PERIOD_S` seconds and at once when its server reappears, through the action's own
cancel service ``<action>/_action/cancel_goal`` (``action_msgs/srv/CancelGoal``: no interface
package is needed). A reply saying the goal is unknown or already terminated settles it; one
that accepts the cancel leaves it open until a later reply says so. ``~/status`` shows how many
there are (:meth:`GoalJournal.unsettled_count`).

**The file** is JSON lines: ``{"e": "sent", "action", "goal", "run", "time"}`` and
``{"e": "done", "goal"}``. At start-up the goals sent and not done are read, their cancels are
sent, and the file is rewritten to hold only them; it is rewritten the same way whenever it has
grown long. A write that fails (disk full, read-only) is reported once through ``log`` and
tried again, as a rewrite, at the next change; the run goes on (design 16).
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from action_msgs.msg import GoalInfo
from action_msgs.srv import CancelGoal
from rclpy.node import Node

from bt_server_core.files import atomic_write_bytes
from bt_server_core.treelog import LogLevel
from bt_server_core.values import dumps_json, loads_json

__all__ = ["GoalJournal", "RESEND_PERIOD_S", "goal_hex", "goal_uuid_bytes"]

#: How often the cancel of an unsettled goal is sent again.
RESEND_PERIOD_S = 3.0
#: How often the journal looks whether the server of an unsettled goal has (re)appeared.
_SERVER_CHECK_S = 1.0
#: The file is compacted when it has this many lines more than open goals.
_COMPACT_AFTER = 500

#: CancelGoal return codes that mean the goal is over: unknown to the server, or terminated.
_SETTLED_CODES = (CancelGoal.Response.ERROR_UNKNOWN_GOAL_ID, CancelGoal.Response.ERROR_GOAL_TERMINATED)


def goal_uuid_bytes(goal_id: Any) -> bytes:
    """16 bytes from a goal id: bytes, a ``unique_identifier_msgs/UUID``, a ``uuid.UUID``, a
    sequence of 16 integers or 32 hex digits. Raises ValueError."""
    if isinstance(goal_id, uuid.UUID):
        return goal_id.bytes
    if isinstance(goal_id, str):
        return uuid.UUID(hex=goal_id).bytes
    raw = getattr(goal_id, "uuid", goal_id)
    data = bytes(bytearray(raw))
    if len(data) != 16:
        raise ValueError(f"a goal id has 16 bytes, not {len(data)}")
    return data


def goal_hex(goal_id: Any) -> str:
    """32 hex digits of a goal id (any form :func:`goal_uuid_bytes` takes)."""
    return goal_uuid_bytes(goal_id).hex()


@dataclass
class _Goal:
    goal: str            # 32 hex digits
    action: str          # the action's full name
    run: str             # the run that sent it ("" for one found at start-up)
    time: float          # when it was sent, seconds since the epoch
    unsettled: bool = False
    cancel_deadline: Any = None
    last_sent: float = field(default=float("-inf"))


class GoalJournal:
    """The goal journal of one executor (see the module docs). Engine thread only.

    ``scheduler``: the executor's :class:`~bt_server_executor.rclpy_scheduler.RclpyScheduler`.
    ``log(level, text)``: where problems and cancels are reported. ``on_change()``: called when
    the number of unsettled goals changes (``~/status`` shows it).
    """

    def __init__(self, path: str, node: Node, scheduler: Any, log: Callable[[int, str], None],
                 on_change: Callable[[], None] = lambda: None) -> None:
        self.path = os.path.expanduser(path)
        self._node = node
        self._scheduler = scheduler
        self._log = log
        self._on_change = on_change
        self._goals: dict[str, _Goal] = {}
        self._clients: dict[str, Any] = {}
        self._ready: dict[str, bool] = {}
        self._lines = 0
        self._write_failed = False
        self._needs_rewrite = False
        self._timer: Any = None
        self._closed = False

    # ----------------------------------------------------------------- start-up
    def start(self) -> int:
        """Read the file, cancel every goal it lists as unfinished (8.4) and keep them as
        unsettled. Returns how many there were."""
        found = self._read()
        for item in found.values():
            item.unsettled = True
            self._goals[item.goal] = item
        self._rewrite()
        if found:
            self._log(LogLevel.WARNING, f"The goal journal lists {len(found)} unfinished goal(s) of an earlier "
                                        f"run of this executor: cancelling them")
            for item in found.values():
                self._send_cancel(item)
            self._on_change()
        self._arm()
        return len(found)

    def _read(self) -> dict[str, _Goal]:
        open_goals: dict[str, _Goal] = {}
        try:
            with open(self.path, encoding="utf-8") as handle:
                lines = handle.readlines()
        except FileNotFoundError:
            return open_goals
        except OSError as error:
            self._log(LogLevel.ERROR, f"The goal journal '{self.path}' cannot be read: {error}")
            return open_goals
        for line in lines:
            try:
                item = loads_json(line)
                goal = goal_hex(item["goal"])
                if item.get("e") == "sent" and isinstance(item.get("action"), str):
                    open_goals[goal] = _Goal(goal, item["action"], str(item.get("run", "")),
                                             float(item.get("time", 0.0)))
                elif item.get("e") == "done":
                    open_goals.pop(goal, None)
            except (ValueError, KeyError, TypeError):
                continue  # a line cut short by a crash
        return open_goals

    # ----------------------------------------------------------------- what leaves call
    def sent(self, action: str, goal_id: Any, run_id: str = "") -> None:
        """Record a goal before it is sent: ``action`` is the action's full name
        (``/robot1/navigate_to_pose``)."""
        goal = goal_hex(goal_id)
        item = _Goal(goal, action, run_id, time.time())
        self._goals[goal] = item
        self._append({"e": "sent", "action": action, "goal": goal, "run": run_id, "time": round(item.time, 3)})

    def cancel_requested(self, goal_id: Any, timeout_s: float) -> None:
        """The cancel of an open goal was sent: if the goal has not settled after ``timeout_s``
        seconds, it becomes unsettled."""
        item = self._goals.get(goal_hex(goal_id))
        if item is None or item.unsettled:
            return
        item.last_sent = time.monotonic()
        if item.cancel_deadline is not None:
            item.cancel_deadline.cancel()
        item.cancel_deadline = self._scheduler.call_later(max(0.0, timeout_s), lambda: self.unsettled(item.goal))

    def unsettled(self, goal_id: Any) -> None:
        """The goal's cancel was not confirmed: keep sending it until the goal settles."""
        item = self._goals.get(goal_hex(goal_id))
        if item is None or item.unsettled:
            return
        item.unsettled = True
        item.cancel_deadline = None
        self._log(LogLevel.WARNING, f"Goal {item.goal} of {item.action} is unsettled: its cancel is sent again "
                                    f"every {RESEND_PERIOD_S:g} s")
        self._send_cancel(item)
        self._arm()
        self._on_change()

    def done(self, goal_id: Any) -> None:
        """The goal settled (a result arrived, or it was rejected). Safe to call twice."""
        item = self._goals.pop(goal_hex(goal_id), None)
        if item is None:
            return
        if item.cancel_deadline is not None:
            item.cancel_deadline.cancel()
        self._append({"e": "done", "goal": item.goal})
        if item.unsettled:
            self._log(LogLevel.INFO, f"Goal {item.goal} of {item.action} settled")
            self._on_change()

    # ----------------------------------------------------------------- what the node calls
    def run_ended(self, run_id: str) -> None:
        """A run ended: its goals still open are unsettled (their leaves are gone)."""
        for item in list(self._goals.values()):
            if item.run == run_id and not item.unsettled:
                self.unsettled(item.goal)

    def unsettled_count(self, run_id: str | None = None) -> int:
        """Unsettled goals; with ``run_id``, only those of that run."""
        return sum(1 for item in self._goals.values() if item.unsettled and (run_id is None or item.run == run_id))

    def open_goals(self) -> list[dict]:
        """Every goal not settled: ``{"goal", "action", "run", "unsettled"}`` (for tests and tools)."""
        return [{"goal": item.goal, "action": item.action, "run": item.run, "unsettled": item.unsettled}
                for item in self._goals.values()]

    def close(self) -> None:
        """Stop resending (shutting down); the file keeps the open goals for the next start."""
        self._closed = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        for client in self._clients.values():
            self._node.destroy_client(client)
        self._clients.clear()

    # ----------------------------------------------------------------- resending
    def _arm(self) -> None:
        if self._timer is None and not self._closed and any(item.unsettled for item in self._goals.values()):
            self._timer = self._scheduler.call_later(_SERVER_CHECK_S, self._on_timer)

    def _on_timer(self) -> None:
        self._timer = None
        now = time.monotonic()
        for item in list(self._goals.values()):
            if not item.unsettled:
                continue
            client = self._client(item)
            if client is None:
                continue
            ready = client.service_is_ready()
            reappeared = ready and not self._ready.get(item.action, False)
            self._ready[item.action] = ready
            if ready and (reappeared or now - item.last_sent >= RESEND_PERIOD_S):
                self._send_cancel(item)
        self._arm()

    def _client(self, item: _Goal) -> Any:
        """The cancel client of the goal's action; None when the action name is not a valid ROS
        name (a damaged journal line): the goal is dropped then, so a bad line cannot stop the
        executor from starting."""
        action = item.action
        client = self._clients.get(action)
        if client is None:
            try:
                client = self._node.create_client(CancelGoal, f"{action.rstrip('/')}/_action/cancel_goal")
            except Exception as error:  # noqa: BLE001 - rclpy raises several kinds for bad names
                self._log(LogLevel.ERROR, f"Goal {item.goal} of the journal names the action {action!r}, which "
                                          f"cannot be cancelled ({error}); it is dropped from the journal")
                self.done(item.goal)
                return None
            self._clients[action] = client
        return client

    def _send_cancel(self, item: _Goal) -> None:
        item.last_sent = time.monotonic()
        client = self._client(item)
        if client is None:
            return
        self._ready[item.action] = client.service_is_ready()
        request = CancelGoal.Request()
        request.goal_info = GoalInfo()
        request.goal_info.goal_id.uuid = list(bytes.fromhex(item.goal))
        future = client.call_async(request)
        goal = item.goal
        future.add_done_callback(lambda done: self._on_cancel_reply(goal, done))

    def _on_cancel_reply(self, goal: str, future: Any) -> None:
        try:
            response = future.result()
        except Exception:  # noqa: BLE001 - the request was dropped; the next resend tries again
            return
        if response is not None and response.return_code in _SETTLED_CODES and goal in self._goals:
            self.done(goal)

    # ----------------------------------------------------------------- the file
    def _append(self, record: dict) -> None:
        if self._needs_rewrite or self._lines - len(self._goals) > _COMPACT_AFTER:
            self._rewrite()
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(dumps_json(record, indent=None) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._lines += 1
            self._write_ok()
        except OSError as error:
            self._write_error(error)

    def _rewrite(self) -> None:
        lines = [dumps_json({"e": "sent", "action": item.action, "goal": item.goal, "run": item.run,
                             "time": round(item.time, 3)}, indent=None) for item in self._goals.values()]
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            atomic_write_bytes(self.path, "".join(line + "\n" for line in lines).encode("utf-8"))
            self._lines = len(lines)
            self._needs_rewrite = False
            self._write_ok()
        except OSError as error:
            self._needs_rewrite = True
            self._write_error(error)

    def _write_ok(self) -> None:
        if self._write_failed:
            self._write_failed = False
            self._log(LogLevel.INFO, f"The goal journal '{self.path}' is written again")

    def _write_error(self, error: OSError) -> None:
        self._needs_rewrite = True
        if not self._write_failed:
            self._write_failed = True
            self._log(LogLevel.ERROR, f"The goal journal '{self.path}' cannot be written: {error}")
