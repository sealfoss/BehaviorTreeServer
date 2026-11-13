"""Delivery settings (QoS) of the tree API (design 4.1), for its publishers and for clients that
subscribe to it (``RosLink``, ``run_tree``, the tests, Remote Sub-Tree leaves).

| Topic           | Publisher                                         | Subscriber                      |
|-----------------|---------------------------------------------------|---------------------------------|
| ``~/run``       | reliable, transient local, last 1                 | the same                        |
| ``~/status``    | reliable, transient local, last 1, deadline 1 s   | reliable, transient local, last 1 (no deadline asked) |
| ``~/blackboard``| reliable, volatile, last 100                      | the same                        |
| ``~/details``   | reliable, volatile, last 100                      | the same                        |
| ``~/log``       | reliable, transient local, last 200               | the same                        |

A subscriber that wants to be told of a missed ``~/status`` deadline asks for a deadline of 1 s
or more (:data:`STATUS_WATCH_QOS`); a requested deadline shorter than the offered one would not
match. :data:`WATCHDOG_QOS` is what an executor's watchdog uses on another run's ``~/status``:
volatile, so the last message kept for late joiners, which may be old, does not count as a sign
of life.
"""

from __future__ import annotations

from rclpy.duration import Duration
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy

__all__ = [
    "BLACKBOARD_QOS",
    "DETAILS_QOS",
    "LOG_QOS",
    "RUN_QOS",
    "STATUS_DEADLINE_S",
    "STATUS_QOS",
    "STATUS_SUB_QOS",
    "STATUS_WATCH_QOS",
    "WATCHDOG_QOS",
]

#: The deadline ``~/status`` declares (design 4.2).
STATUS_DEADLINE_S = 1.0


def _profile(depth: int, *, latched: bool, deadline_s: float | None = None) -> QoSProfile:
    profile = QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=depth,
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.TRANSIENT_LOCAL if latched else DurabilityPolicy.VOLATILE,
    )
    if deadline_s is not None:
        profile.deadline = Duration(seconds=deadline_s)
    return profile


RUN_QOS = _profile(1, latched=True)
STATUS_QOS = _profile(1, latched=True, deadline_s=STATUS_DEADLINE_S)
STATUS_SUB_QOS = _profile(1, latched=True)
STATUS_WATCH_QOS = _profile(1, latched=True, deadline_s=STATUS_DEADLINE_S)
WATCHDOG_QOS = _profile(10, latched=False)
BLACKBOARD_QOS = _profile(100, latched=False)
DETAILS_QOS = _profile(100, latched=False)
LOG_QOS = _profile(200, latched=True)
