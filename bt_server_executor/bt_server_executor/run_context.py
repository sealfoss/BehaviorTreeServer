"""What leaves get as ``ctx.run`` in the executor: :class:`RunContext`, and the
:class:`ClientCache` it hands out ROS clients from (design 6.5, 8.2, 8.4).

For the author of a ROS leaf, everything below runs on the engine thread (the thread that spins
the executor), which is also where rclpy delivers the replies, feedback and results of the
clients made here: no locks are needed, and no call may block.

``ctx.run`` (a :class:`RunContext`)::

    run.node              # the executor's rclpy node (also ctx.node)
    run.executor_name     # "/cell1/bt_executor"
    run.run_id            # this run's id: 32 hex digits (the ExecuteTree goal id)
    run.run_uuid          # the same, 16 bytes (for a unique_identifier_msgs/UUID)
    run.status_topic      # "/cell1/bt_executor/status": this executor's ~/status
    run.cancel_timeout_s  # the tree's Cancel timeout (its option, 5 s by default)
    run.bundle            # the run's bundle (design 4.3), parsed: a dict; do not change it
    run.journal           # the goal journal (bt_server_executor.journal.GoalJournal)
    run.clients           # the client cache (ClientCache)
    run.scheduler         # the executor's scheduler (now(), ros_now(), call_soon(), call_at())
    run.log(level, text)  # the tree log of this run (1 ERROR ... 4 INFO)

    run.action_client(action_type, name)          # = run.clients.action_client(...)
    run.service_client(srv_type, name)            # = run.clients.service_client(...)
    run.publisher(msg_type, topic, qos)           # = run.clients.publisher(...)
    run.record_goal(action_name, goal_id)         # = run.journal.sent(action_name, goal_id, run.run_id)
    run.goal_cancel_sent(goal_id, timeout_s=None) # = run.journal.cancel_requested(...) with the
                                                  #   tree's cancel timeout by default
    run.goal_done(goal_id)                        # = run.journal.done(goal_id)

**The goal journal.** A leaf that sends an action goal makes the goal id itself
(``uuid.uuid4()``), calls ``run.record_goal(action_name, goal_id)`` and then sends the goal
with ``send_goal_async(goal, feedback_callback=..., goal_uuid=UUID(uuid=list(goal_id.bytes)))``.
It calls ``run.goal_done(goal_id)`` when the goal settles: the reply rejected it, or a result
arrived (whatever its status, also after the leaf was halted and also after the run ended:
keep the result callback alive for that). When it sends a cancel it calls
``run.goal_cancel_sent(goal_id)``: if no result arrives within the cancel timeout, the journal
keeps the goal as unsettled and sends its cancel again every few seconds and when its server
reappears, until the server says the goal is over. When the run ends, every goal of the run
not yet done becomes unsettled the same way. A restarted executor cancels the goals its
journal file lists as open (8.4).

**The client cache.** ``action_client``, ``service_client`` and ``publisher`` return the entity
for that type, name (and QoS) and create it the first time. Entities are kept between runs and
destroyed after :data:`IDLE_DROP_S` (10 minutes) unused, so a re-run starts without the
discovery delay. Create them when the run is set up, not when the leaf runs: a leaf class may
define ``prepare(self, ctx)``, which the executor calls once per leaf node after the run's
leaves are made and before the first tick (bindings are not resolved yet: use literal
fields such as the action name); an exception there is logged as a WARNING and the run goes
on. Do not destroy cached entities yourself.

``action_type`` / ``srv_type`` / ``msg_type`` are interface classes (``Fibonacci``,
``Trigger``), not names; loading a class from a type name, with the safety check of 6.5, is
the ROS leaves' business.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from rclpy.action import ActionClient
from rclpy.node import Node
from rclpy.qos import QoSProfile

from .journal import GoalJournal

__all__ = ["IDLE_DROP_S", "ClientCache", "RunContext"]

#: Cached clients and publishers unused for this long are destroyed (design 8.2).
IDLE_DROP_S = 600.0
#: How often the cache looks for entities to drop.
_SWEEP_S = 60.0


@dataclass
class _Cached:
    kind: str
    entity: Any
    last_used: float
    in_use: bool


def _type_key(cls: Any) -> str:
    return f"{getattr(cls, '__module__', '')}.{getattr(cls, '__qualname__', repr(cls))}"


def _qos_key(qos: QoSProfile | int) -> Any:
    if isinstance(qos, int):
        return ("depth", qos)
    return (qos.history, qos.depth, qos.reliability, qos.durability, qos.deadline.nanoseconds,
            qos.lifespan.nanoseconds, qos.liveliness)


class ClientCache:
    """Action clients, service clients and publishers kept between runs (see the module docs).
    Engine thread only. ``now`` is the steady clock."""

    def __init__(self, node: Node, now: Callable[[], float] = time.monotonic) -> None:
        self._node = node
        self._now = now
        self._items: dict[tuple, _Cached] = {}

    def action_client(self, action_type: Any, name: str) -> ActionClient:
        """The action client of ``action_type`` for the action ``name``."""
        return self._get(("action", _type_key(action_type), name),
                         lambda: ActionClient(self._node, action_type, name))

    def service_client(self, srv_type: Any, name: str) -> Any:
        """The service client of ``srv_type`` for the service ``name``."""
        return self._get(("service", _type_key(srv_type), name), lambda: self._node.create_client(srv_type, name))

    def publisher(self, msg_type: Any, topic: str, qos: QoSProfile | int = 10) -> Any:
        """The publisher of ``msg_type`` on ``topic`` with ``qos``."""
        return self._get(("publisher", _type_key(msg_type), topic, _qos_key(qos)),
                         lambda: self._node.create_publisher(msg_type, topic, qos))

    def _get(self, key: tuple, make: Callable[[], Any]) -> Any:
        item = self._items.get(key)
        if item is None:
            item = _Cached(key[0], make(), self._now(), True)
            self._items[key] = item
        item.in_use = True
        item.last_used = self._now()
        return item.entity

    def run_ended(self) -> None:
        """The run ended: what it used counts as unused from now on."""
        now = self._now()
        for item in self._items.values():
            if item.in_use:
                item.in_use = False
                item.last_used = now

    def sweep(self, max_idle_s: float = IDLE_DROP_S) -> int:
        """Destroy the entities no run uses that were last used ``max_idle_s`` ago or longer;
        returns how many."""
        limit = self._now() - max_idle_s
        dropped = [key for key, item in self._items.items() if not item.in_use and item.last_used <= limit]
        for key in dropped:
            self._destroy(self._items.pop(key))
        return len(dropped)

    def close(self) -> None:
        """Destroy everything (shutting down)."""
        for item in self._items.values():
            self._destroy(item)
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)

    def _destroy(self, item: _Cached) -> None:
        try:
            if item.kind == "action":
                item.entity.destroy()
            elif item.kind == "service":
                self._node.destroy_client(item.entity)
            else:
                self._node.destroy_publisher(item.entity)
        except Exception:  # noqa: BLE001 - destroying twice at shutdown is harmless
            pass


class RunContext:
    """``ctx.run`` of a run in the executor (see the module docs)."""

    def __init__(self, *, node: Node, executor_name: str, status_topic: str, journal: GoalJournal,
                 clients: ClientCache, scheduler: Any, log: Callable[[int, str], None],
                 cancel_timeout_s: float = 5.0, bundle: dict | None = None) -> None:
        self.node = node
        self.executor_name = executor_name
        self.status_topic = status_topic
        self.journal = journal
        self.clients = clients
        self.scheduler = scheduler
        self.cancel_timeout_s = cancel_timeout_s
        self.bundle: dict = bundle if bundle is not None else {}
        self._log = log
        self.run_id = ""
        self.run_uuid = bytes(16)

    def set_run_id(self, run_uuid: bytes) -> None:
        """Name the run (the executor does this when the goal is accepted, before the first tick)."""
        self.run_uuid = bytes(run_uuid)
        self.run_id = self.run_uuid.hex()

    def log(self, level: int, text: str) -> None:
        """Write to this run's tree log (and, for ERROR and WARNING, to /rosout)."""
        self._log(int(level), str(text))

    def action_client(self, action_type: Any, name: str) -> ActionClient:
        return self.clients.action_client(action_type, name)

    def service_client(self, srv_type: Any, name: str) -> Any:
        return self.clients.service_client(srv_type, name)

    def publisher(self, msg_type: Any, topic: str, qos: QoSProfile | int = 10) -> Any:
        return self.clients.publisher(msg_type, topic, qos)

    def record_goal(self, action_name: str, goal_id: Any) -> None:
        """Journal a goal of this run just before it is sent."""
        self.journal.sent(action_name, goal_id, self.run_id)

    def goal_cancel_sent(self, goal_id: Any, timeout_s: float | None = None) -> None:
        """A cancel of the goal was sent: unsettled if not settled within ``timeout_s`` (default:
        the tree's cancel timeout)."""
        self.journal.cancel_requested(goal_id, self.cancel_timeout_s if timeout_s is None else timeout_s)

    def goal_done(self, goal_id: Any) -> None:
        """The goal settled: a result arrived, or the goal was rejected."""
        self.journal.done(goal_id)
