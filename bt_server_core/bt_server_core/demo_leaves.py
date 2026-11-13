"""Example custom leaves: the widget's ``demo.py`` leaves, ported to :class:`LeafNodeWidget`
without Qt. Name this module in ``leaf_modules`` to offer them (``bt_server_core.demo_leaves``).
"""

from __future__ import annotations

import random
import time
from typing import Any

from .compat import LeafNodeWidget, LogLevel, Status

__all__ = [
    "DEMO_NODE_TYPES",
    "CounterAtLeast",
    "Fail",
    "IncrementCounter",
    "LogMessage",
    "MoveTo",
    "RandomOutcome",
    "RunningTicks",
    "Wait",
]


class Wait(LeafNodeWidget):
    """Waits for a number of seconds (runs on a worker thread, can be cancelled)."""

    _title = "Wait"
    _fields = {"seconds": 1.0}

    def OnRun(self, tree: Any) -> Any:  # noqa: N802 - the widget's name
        deadline = time.monotonic() + max(0.0, self.GetField("seconds"))
        while time.monotonic() < deadline:
            if self.CancelRequested():
                return False
            time.sleep(0.02)
        return True


class LogMessage(LeafNodeWidget):
    """Writes a message to the tree's log and to the "last_message" blackboard entry."""

    _title = "Log Message"
    _fields = {"message": "Hello from the behavior tree!", "level": ["INFO", "WARNING", "ERROR"]}
    RUN_IN_THREAD = False  # quick: runs on the engine thread

    def OnRun(self, tree: Any) -> Any:  # noqa: N802
        message = self.GetField("message")
        tree.Log(LogLevel.coerce(self.GetFieldSelection("level")), message)
        tree.SetEntry(message, "last_message")
        return True


class IncrementCounter(LeafNodeWidget):
    """Adds ``step`` to an Integer blackboard entry (created when missing)."""

    _title = "Increment Counter"
    _fields = {"entry": "counter", "step": 1}
    RUN_IN_THREAD = False

    def OnRun(self, tree: Any) -> Any:  # noqa: N802
        name = self.GetField("entry")
        value = tree.GetEntry(name) if tree.HasEntry(name) else 0
        tree.SetEntry(value + self.GetField("step"), name)
        return True


class CounterAtLeast(LeafNodeWidget):
    """Condition: succeeds when an Integer blackboard entry is >= ``threshold``."""

    _title = "Counter At Least"
    _fields = {"entry": "counter", "threshold": 3}
    RUN_IN_THREAD = False

    def OnRun(self, tree: Any) -> Any:  # noqa: N802
        name = self.GetField("entry")
        return tree.HasEntry(name) and tree.GetEntry(name) >= self.GetField("threshold")


class RandomOutcome(LeafNodeWidget):
    """Succeeds with the given probability."""

    _title = "Random Outcome"
    _fields = {"success_probability": 0.5}
    RUN_IN_THREAD = False

    def OnRun(self, tree: Any) -> Any:  # noqa: N802
        return random.random() < self.GetField("success_probability")


class MoveTo(LeafNodeWidget):
    """Simulates moving to (x, y); shows every field type. Writes "position" to the blackboard."""

    _title = "Move To"
    _fields = {
        "x": 5.0,
        "y": 2.5,
        "speed": 5,
        "mode": ["walk", "run", "crawl"],
        "avoid_obstacles": True,
        "label": "target",
    }

    def OnRun(self, tree: Any) -> Any:  # noqa: N802
        position = tree.GetEntry("position") if tree.HasEntry("position") else [0.0, 0.0]
        target = [self.GetField("x"), self.GetField("y")]
        factor = {"walk": 1.0, "run": 2.0, "crawl": 0.25}[self.GetFieldSelection("mode")]
        step = max(1, self.GetField("speed")) * factor * 0.05
        while True:
            if self.CancelRequested():
                return False
            delta = [t - p for t, p in zip(target, position, strict=True)]
            distance = (delta[0] ** 2 + delta[1] ** 2) ** 0.5
            if distance <= step:
                tree.SetEntry(target, "position")
                return True
            position = [p + d / distance * step for p, d in zip(position, delta, strict=True)]
            tree.SetEntry([round(p, 3) for p in position], "position")
            time.sleep(0.05)


class Fail(LeafNodeWidget):
    """Always fails."""

    _title = "Fail"
    RUN_IN_THREAD = False

    def OnRun(self, tree: Any) -> Any:  # noqa: N802
        return False


class RunningTicks(LeafNodeWidget):
    """Returns RUNNING for ``ticks`` ticks, then succeeds (polling style, engine thread)."""

    _title = "Running For Ticks"
    _fields = {"ticks": 3}
    RUN_IN_THREAD = False

    def OnStart(self, tree: Any) -> None:  # noqa: N802
        self._remaining = self.GetField("ticks")

    def OnRun(self, tree: Any) -> Any:  # noqa: N802
        if self._remaining > 0:
            self._remaining -= 1
            return Status.RUNNING
        return True


DEMO_NODE_TYPES = [Wait, LogMessage, IncrementCounter, CounterAtLeast, RandomOutcome, MoveTo, Fail, RunningTicks]
