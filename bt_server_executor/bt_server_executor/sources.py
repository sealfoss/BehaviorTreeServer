"""Blackboard entries fed from ROS topics during a run (design 7.4).

The executor makes one :class:`SourceManager` and calls ``start(blackboard, request_tick)`` when a
run starts and ``stop()`` when it ends. In between, every entry of the run's blackboard that has
a source gets its value from its topic through :meth:`Blackboard.feed`, which also renews the
entry's age (measured on the scheduler's steady clock, from the moment a message arrives).

* **One subscription per topic**, shared by the entries that read it. Entries added, removed or
  given another source during the run are followed (a blackboard listener).
* **Delivery adapts to the publishers** (:func:`~bt_server_executor.ros_types.adapted_qos`, as
  ``ros2 topic echo`` does): reliable or best effort, transient local or volatile, as all
  publishers offer; without a publisher, best effort and volatile, which every publisher can
  serve. Every :data:`CHECK_PERIOD_S` the publishers are looked at again and the subscription is
  made anew when their settings call for it. If ROS still reports the two as incompatible, the
  entries of the topic get a problem (:attr:`SourceManager.problems`) and a WARNING is logged.
* **Only messages that are used are decoded.** The subscription is raw; a message is decoded
  (``rclpy.serialization.deserialize_message``) only when an entry's *Max rate* allows it
  another update. A message that comes too soon is kept, undecoded, and applied when the entry
  may next be updated, so the latest message is never lost to the rate limit (S-04). A Max rate
  of 0 takes every message.
* **Values follow the table of 7.4** (:mod:`.ros_types`). A value that does not fit the entry
  (a ``uint32`` or 64-bit value outside 32 bits for an Integer entry, S-10; an index the array
  does not have) is skipped, and a WARNING is logged once per entry and kind of problem.
* **A changed value requests a tick**; so does an entry going stale (*Max age*), at the moment it
  does.

Everything runs on the engine thread (the thread that spins the executor), except the
blackboard listener, which may be called from a worker thread and only schedules work.
``log(level, text)`` takes the levels of the tree log (1 ERROR, 2 WARNING, 3 DEBUG, 4 INFO).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rclpy.event_handler import SubscriptionEventCallbacks
from rclpy.node import Node
from rclpy.serialization import deserialize_message

from bt_server_core.blackboard import Blackboard, OpError
from bt_server_core.treelog import LogLevel

from . import ros_types
from .ros_types import FieldInfo, SkipValue

__all__ = ["CHECK_PERIOD_S", "SourceManager"]

#: How often the publishers of each topic are looked at again.
CHECK_PERIOD_S = 1.0


@dataclass
class _Feed:
    """One entry fed from a topic."""

    entry_id: str
    name: str
    entry_type: str
    source: dict
    parts: list = field(default_factory=list)
    info: FieldInfo | None = None
    problem: str | None = None
    next_allowed: float = float("-inf")
    pending: bytes | None = None
    pending_timer: Any = None
    stale_timer: Any = None
    warned: set = field(default_factory=set)

    @property
    def interval(self) -> float:
        rate = self.source.get("max_rate_hz") or 0.0
        return 1.0 / rate if rate > 0 else 0.0

    def cancel_timers(self) -> None:
        for timer in (self.pending_timer, self.stale_timer):
            if timer is not None:
                timer.cancel()
        self.pending_timer = self.stale_timer = None


@dataclass
class _Topic:
    """One subscribed topic and the entries it feeds."""

    name: str
    feeds: dict[str, _Feed] = field(default_factory=dict)
    type_name: str = ""
    cls: Any = None
    subscription: Any = None
    qos_key: tuple | None = None
    publishers_key: tuple | None = None
    problem: str | None = None
    qos_problem: str | None = None
    warned: set = field(default_factory=set)
    last_raw: bytes | None = None
    last_message: Any = None


class SourceManager:
    """The sources of the current run (see the module docs)."""

    def __init__(self, node: Node, scheduler: Any, log: Callable[[int, str], None]) -> None:
        self._node = node
        self._scheduler = scheduler
        self._log = log
        self._board: Blackboard | None = None
        self._request_tick: Callable[[], None] = lambda: None
        self._topics: dict[str, _Topic] = {}
        self._check_timer: Any = None
        self._sync_pending = False
        self._feeding = False
        self._engine_thread = threading.get_ident()

    # ----------------------------------------------------------------- the executor's calls
    def start(self, blackboard: Blackboard, request_tick: Callable[[], None]) -> None:
        """A run starts: subscribe for every sourced entry of ``blackboard`` (engine thread)."""
        self.stop()
        self._engine_thread = threading.get_ident()
        self._board = blackboard
        self._request_tick = request_tick
        blackboard.add_listener(self._on_board_change)
        self._sync()
        self._arm_check()

    def stop(self) -> None:
        """The run ended: drop every subscription and timer (engine thread)."""
        if self._board is not None:
            self._board.remove_listener(self._on_board_change)
        self._board = None
        if self._check_timer is not None:
            self._check_timer.cancel()
            self._check_timer = None
        for topic in list(self._topics.values()):
            self._close(topic)
        self._topics.clear()
        self._sync_pending = False

    @property
    def problems(self) -> dict[str, str]:
        """``{entry id: text}`` of the sourced entries that cannot be fed now, and why."""
        result = {}
        for topic in self._topics.values():
            for feed in topic.feeds.values():
                problem = feed.problem or topic.problem or topic.qos_problem
                if problem is not None:
                    result[feed.entry_id] = problem
        return result

    # ----------------------------------------------------------------- following the blackboard
    def _on_board_change(self) -> None:
        if self._board is None or self._sync_pending:
            return
        if self._feeding and threading.get_ident() == self._engine_thread:
            return  # our own feed: no source changed
        self._sync_pending = True
        self._scheduler.call_soon(self._sync)

    def _wanted(self) -> dict[str, dict[str, tuple[str, str, dict]]]:
        """``{topic: {entry id: (name, entry type, source)}}`` of the blackboard now."""
        board = self._board
        wanted: dict[str, dict[str, tuple[str, str, dict]]] = {}
        if board is None:
            return wanted
        with board.lock:
            for entry_id in board.ids():
                name = board.name_of(entry_id)
                if name is None:
                    continue
                source = board.source_of(name)
                if source is not None:
                    wanted.setdefault(source["topic"], {})[entry_id] = (name, board.type_of(name), source)
        return wanted

    def _sync(self) -> None:
        """Make the subscriptions match the sourced entries."""
        self._sync_pending = False
        if self._board is None:
            return
        wanted = self._wanted()
        for name in [name for name in self._topics if name not in wanted]:
            self._close(self._topics.pop(name))
        for name, entries in wanted.items():
            topic = self._topics.get(name)
            if topic is None:
                topic = self._topics[name] = _Topic(name)
            changed = False
            for entry_id in [entry_id for entry_id in topic.feeds if entry_id not in entries]:
                topic.feeds.pop(entry_id).cancel_timers()
                changed = True
            for entry_id, (entry_name, entry_type, source) in entries.items():
                feed = topic.feeds.get(entry_id)
                if feed is not None and feed.entry_type == entry_type and feed.source == source:
                    feed.name = entry_name
                    continue
                if feed is not None:
                    feed.cancel_timers()
                topic.feeds[entry_id] = _Feed(entry_id, entry_name, entry_type, source)
                changed = True
            if changed or topic.subscription is None:
                self._configure(topic)

    # ----------------------------------------------------------------- one topic
    def _configure(self, topic: _Topic) -> None:
        """Work out the topic's type and its entries' fields, and (re)subscribe."""
        type_name, topic.problem = self._topic_type(topic)
        if type_name != topic.type_name:
            self._unsubscribe(topic)
            topic.type_name, topic.cls = type_name, None
        if type_name and topic.cls is None:
            try:
                topic.cls = ros_types.load_interface(type_name, "msg")
            except ValueError as error:
                topic.problem = str(error)
        for feed in topic.feeds.values():
            self._configure_feed(topic, feed)
        if topic.problem is not None:
            self._warn_topic(topic, "type", topic.problem)
        if topic.cls is not None:
            self._subscribe(topic)

    def _topic_type(self, topic: _Topic) -> tuple[str, str | None]:
        """The type to subscribe with (the one the entries name, else the publishers') and what is
        wrong with it, if anything. Only publishers count: the graph's types of the topic include
        this subscription's own."""
        named = [feed.source.get("type", "") for feed in topic.feeds.values() if feed.source.get("type")]
        network = sorted({info.topic_type for info in self._node.get_publishers_info_by_topic(topic.name)})
        if named:
            if network and named[0] not in network:
                return named[0], f"the topic {topic.name} carries {', '.join(network)}, not {named[0]}"
            return named[0], None
        if len(network) == 1:
            return network[0], None
        if not network:
            return "", f"the topic {topic.name} has no publisher yet: its type is unknown"
        return "", f"the topic {topic.name} has several types; name one ({', '.join(network)})"

    def _warn_topic(self, topic: _Topic, kind: str, text: str) -> None:
        if (kind, text) not in topic.warned:
            topic.warned.add((kind, text))
            names = ", ".join(repr(feed.name) for feed in topic.feeds.values())
            self._log(LogLevel.WARNING, f"Source of {names}: {text}")

    def _configure_feed(self, topic: _Topic, feed: _Feed) -> None:
        feed.problem = None
        own_type = feed.source.get("type", "")
        if own_type and topic.type_name and own_type != topic.type_name:
            feed.problem = f"another entry reads {topic.name} as {topic.type_name}, this one as {own_type}"
        elif topic.cls is not None:
            try:
                feed.parts = ros_types.field_parts(feed.source.get("field"))
                feed.info = ros_types.resolve_field(topic.cls, feed.source.get("field"))
                feed.problem = ros_types.entry_type_problem(feed.info, feed.entry_type)
            except ValueError as error:
                feed.problem = str(error)
        if feed.problem is not None:
            self._warn_once(feed, "config", feed.problem)

    def _subscribe(self, topic: _Topic) -> None:
        """Subscribe with the delivery the publishers call for, unless already so."""
        qos = ros_types.adapted_qos(self._node, topic.name)
        key = (qos.reliability, qos.durability)
        publishers = self._publishers_key(topic.name)
        if publishers != topic.publishers_key:
            topic.publishers_key = publishers
            topic.qos_problem = None  # other publishers: ROS reports anew
        if topic.subscription is not None and topic.qos_key == key:
            return
        self._unsubscribe(topic)
        events = SubscriptionEventCallbacks(incompatible_qos=lambda info: self._on_incompatible(topic, info),
                                            incompatible_type=lambda info: self._on_incompatible_type(topic),
                                            use_default_callbacks=False)
        topic.subscription = self._node.create_subscription(
            topic.cls, topic.name, lambda raw: self._on_message(topic, raw), qos, raw=True, event_callbacks=events)
        topic.qos_key = key

    def _unsubscribe(self, topic: _Topic) -> None:
        if topic.subscription is not None:
            self._node.destroy_subscription(topic.subscription)
            topic.subscription = None
            topic.qos_key = None

    def _close(self, topic: _Topic) -> None:
        for feed in topic.feeds.values():
            feed.cancel_timers()
        self._unsubscribe(topic)

    def _publishers_key(self, name: str) -> tuple:
        """What matters of the topic's publishers: their types and delivery settings."""
        return tuple(sorted((info.topic_type, int(info.qos_profile.reliability), int(info.qos_profile.durability))
                            for info in self._node.get_publishers_info_by_topic(name)))

    # ----------------------------------------------------------------- checking the publishers
    def _arm_check(self) -> None:
        self._check_timer = self._scheduler.call_at(self._scheduler.now() + CHECK_PERIOD_S, self._on_check)

    def _on_check(self) -> None:
        self._check_timer = None
        if self._board is None:
            return
        for topic in list(self._topics.values()):
            if topic.cls is None or topic.problem is not None:
                self._configure(topic)  # the type may be known now
            elif self._publishers_key(topic.name) != topic.publishers_key:
                self._configure(topic)
        self._arm_check()

    def _on_incompatible(self, topic: _Topic, info: Any) -> None:
        """ROS reports a publisher whose delivery settings this subscription cannot match: look
        at the publishers again; if that changes nothing, report it."""
        if self._topics.get(topic.name) is not topic:
            return
        before = topic.qos_key
        self._configure(topic)
        if topic.qos_key == before:
            policy = getattr(getattr(info, "last_policy_kind", None), "name", "a delivery setting")
            topic.qos_problem = (f"ROS reports the publishers of {topic.name} as incompatible with this "
                                 f"subscription ({policy})")
            self._warn_topic(topic, "qos", topic.qos_problem)

    def _on_incompatible_type(self, topic: _Topic) -> None:
        """ROS reports a publisher of another type: look again (the entries get the problem)."""
        if self._topics.get(topic.name) is topic:
            self._configure(topic)

    # ----------------------------------------------------------------- messages
    def _on_message(self, topic: _Topic, raw: bytes) -> None:
        if self._topics.get(topic.name) is not topic or self._board is None:
            return
        now = self._scheduler.now()
        for feed in list(topic.feeds.values()):
            if feed.problem is not None:
                continue
            if now >= feed.next_allowed:
                self._apply(topic, feed, raw, now)
            else:
                feed.pending = raw
                if feed.pending_timer is None:
                    feed.pending_timer = self._scheduler.call_at(
                        feed.next_allowed, lambda feed=feed: self._apply_pending(topic, feed))

    def _apply_pending(self, topic: _Topic, feed: _Feed) -> None:
        feed.pending_timer = None
        raw, feed.pending = feed.pending, None
        if raw is None or self._topics.get(topic.name) is not topic or topic.feeds.get(feed.entry_id) is not feed:
            return
        self._apply(topic, feed, raw, self._scheduler.now())

    def _decode(self, topic: _Topic, raw: bytes) -> Any:
        if raw is not topic.last_raw:
            topic.last_message = deserialize_message(raw, topic.cls)
            topic.last_raw = raw
        return topic.last_message

    def _apply(self, topic: _Topic, feed: _Feed, raw: bytes, now: float) -> None:
        """Decode ``raw`` and feed the entry its value."""
        feed.next_allowed = now + feed.interval
        board = self._board
        if board is None:
            return
        try:
            message = self._decode(topic, raw)
        except Exception as error:  # noqa: BLE001 - a message that cannot be decoded must not stop the executor
            self._warn_once(feed, "decode", f"a message of {topic.name} cannot be decoded: {error}")
            return
        try:
            value = ros_types.entry_value(ros_types.extract(message, feed.parts), feed.entry_type)
        except (IndexError, SkipValue) as error:
            self._warn_once(feed, type(error).__name__, f"a message of {topic.name} was skipped: {error}")
            return
        self._feeding = True
        try:
            changed = board.feed(feed.entry_id, value)
        except KeyError:
            self._on_board_change()  # the entry is gone
            return
        except OpError as error:
            self._warn_once(feed, "feed", f"a message of {topic.name} was skipped: {error}")
            return
        finally:
            self._feeding = False
        if changed:
            self._request_tick()
        max_age = feed.source.get("max_age_s") or 0.0
        if max_age > 0:
            if feed.stale_timer is not None:
                feed.stale_timer.cancel()
            feed.stale_timer = self._scheduler.call_at(now + max_age, lambda: self._on_stale(feed))

    def _on_stale(self, feed: _Feed) -> None:
        """The entry went stale: the tree must see it now (an Evaluation fails or keeps waiting)."""
        feed.stale_timer = None
        if self._board is not None:
            self._request_tick()

    def _warn_once(self, feed: _Feed, kind: str, text: str) -> None:
        if kind not in feed.warned:
            feed.warned.add(kind)
            self._log(LogLevel.WARNING, f"Source of {feed.name!r}: {text}")
