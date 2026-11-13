"""The blackboard of a run (design 7): ordered, typed entries with ids, Resets, the restore
snapshot, Object entries, and entries fed from ROS topics with their freshness.

Ported from the widget's ``BlackboardStore`` (``behavior_tree_widget/blackboard/store.py``), with
ids instead of the widget's internal identities, sources (7.4) and the ``bb.*`` operations of
the browser protocol (contract 5.3). No ROS here: the executor feeds sourced entries through
:meth:`Blackboard.feed`.

Entries
    Each entry has an ``id`` (32 hex digits, kept for the life of the entry, also across
    renames), a ``name`` (design 7.1: not empty, no spaces around it, no ``.`` or ``/``), a
    ``type`` (``Integer``, ``Double``, ``String``, ``Bool``, ``List``, ``Dictionary``, ``Set`` or
    ``Object``), a ``value``, a ``reset`` flag and an optional ``source``
    (``{"topic", "type", "field", "max_rate_hz", "max_age_s"}``). Values are Python values;
    the file and wire encodings belong to :mod:`bt_server_core.values`.

Writing
    * :meth:`Blackboard.set` is what leaf code and Set nodes use: it creates a missing entry
      (type inferred from the value) and refuses an entry that has a source.
    * :meth:`Blackboard.apply_op` applies one ``bb.*`` operation of contract 5.3 whose values
      are already decoded (Python values). A refusal raises :class:`OpError` with the reason.
    * :meth:`Blackboard.feed` is the only way to set a sourced entry: the executor calls it for
      each message a source lets through. It renews the entry's age.
    * :meth:`Blackboard.put_object` adds an Object entry (``setup(blackboard)`` hooks of leaf
      modules, design 7.5); it replaces an entry of that name.

Freshness
    A sourced entry is ``"nodata"`` until it is fed, ``"stale"`` when ``max_age_s`` is above 0
    and its last feed is that old (on the clock given to the constructor, the scheduler's
    steady clock), and ``"ok"`` otherwise. Entries without a source have freshness None.

Resets (design 7.3, the widget's rules)
    Between :meth:`start_loop` and :meth:`end_loop` the value of each entry is recorded just
    before its first change. :meth:`end_loop` gives entries whose Reset flag is set at that
    moment their recorded value (and type) back. An entry renamed during the loop is reset
    under its new name; one created during the loop is left alone; one removed is not brought
    back. Sourced entries are never reset.

Restore (design 7.3)
    :meth:`snapshot` copies every entry; :meth:`restore` makes the blackboard hold exactly those
    entries again, in their order: entries renamed since are renamed back, keeping their ids
    (K-04), entries created since are removed, removed ones come back with their ids. The value
    of a sourced entry is left alone.

Threads and notifications (design 7.6)
    Every method is thread-safe (one re-entrant lock, :attr:`lock`, which leaf code may hold to
    make several calls at once). After each change the listeners (:meth:`add_listener`) are
    called without arguments, on the thread that made the change and outside the lock; the
    runner uses this to request a tick. :meth:`drain_changes` returns what changed since the
    last call, in the shape of ``Sink.blackboard``: ``{"entries": [entry dicts], "removed":
    [ids], "order": [ids] | None}``, or None when nothing changed. :meth:`drain_renames` returns
    the renames since the last call, so a run's nodes can follow them.

Entry dicts (:meth:`to_list`, :meth:`drain_changes`, :meth:`entry`) have the file form plus the
id: ``{"id", "name", "type", "value": <file encoding>, "reset": bool, "source": {...} | None}``;
:meth:`drain_changes` and :meth:`entry` add ``"freshness"``. An Object entry's value there is its
bounded ``repr`` (Object entries are never saved).
"""

from __future__ import annotations

import copy
import itertools
import math
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from .values import (
    ENTRY_TYPES,
    canonical_type,
    coerce_value,
    convert_literal,
    decode_value,
    encode_value,
    entry_name_problem,
    equal_values,
    infer_type,
    safe_repr,
)

__all__ = ["Blackboard", "BlackboardSnapshot", "OpError", "SOURCE_DEFAULTS", "new_entry_id"]

#: Defaults of the settings of a source (design 7.4).
SOURCE_DEFAULTS = {"field": "", "max_rate_hz": 2.0, "max_age_s": 0.0}


class OpError(ValueError):
    """An operation or a write that is refused; the message says why."""


def new_entry_id() -> str:
    """A new entry id: 32 hex digits."""
    return uuid.uuid4().hex


@dataclass
class _Entry:
    id: str
    name: str
    type: str
    value: Any
    reset: bool = False
    source: dict | None = None
    fed_at: float | None = None


@dataclass
class BlackboardSnapshot:
    """What :meth:`Blackboard.snapshot` returns: every entry, in order, with its id."""

    entries: list[_Entry] = field(default_factory=list)

    def names(self) -> list[str]:
        return [entry.name for entry in self.entries]


def _copy(type_name: str, value: Any) -> Any:
    """A copy of a stored value for a reader or a snapshot (Object entries by reference)."""
    if type_name in ("List", "Dictionary", "Set"):
        try:
            return copy.deepcopy(value)
        except Exception:  # noqa: BLE001 - uncopyable items are kept by reference
            return value
    return value


def check_source(source: Any) -> dict:
    """A source's settings, checked and completed with the defaults.

    Raises:
        OpError: the settings are malformed.
    """
    if not isinstance(source, dict):
        raise OpError("a source is an object with topic, type, field, max_rate_hz and max_age_s")
    topic = source.get("topic")
    if not isinstance(topic, str) or not topic.strip():
        raise OpError("a source needs a topic")
    type_name = source.get("type", "")
    if not isinstance(type_name, str):
        raise OpError("the type of a source is text, such as sensor_msgs/msg/BatteryState")
    result = {"topic": topic.strip(), "type": type_name.strip(), **SOURCE_DEFAULTS}
    field_name = source.get("field", "")
    if field_name is None:
        field_name = ""
    if not isinstance(field_name, str):
        raise OpError("the field of a source is text, such as pose.position.x")
    result["field"] = field_name.strip()
    for key in ("max_rate_hz", "max_age_s"):
        value = source.get(key, SOURCE_DEFAULTS[key])
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise OpError(f"{key} of a source must be a number of at least 0")
        result[key] = float(value)
    return result


class Blackboard:
    """The blackboard of a run (see the module docs).

    ``entries``: entries in the form of the blackboard file (a list, or the whole file object
    with its ``"blackboard"`` list), values in file encoding, each optionally with an ``"id"``;
    invalid entries are skipped and listed in :attr:`problems`. ``clock``: the steady clock the
    ages of fed entries are measured on (by default ``time.monotonic``).
    """

    def __init__(self, entries: Any = None, *, clock: Callable[[], float] | None = None) -> None:
        self._lock = threading.RLock()
        self._clock = clock if clock is not None else time.monotonic
        self._entries: dict[str, _Entry] = {}  # by id, in display order
        self._by_name: dict[str, str] = {}
        self._capture: dict[str, tuple[str, Any] | None] | None = None
        self._changed: set[str] = set()
        self._removed: list[str] = []
        self._order_changed = False
        self._renames: list[tuple[str, str]] = []
        self._listeners: list[Callable[[], None]] = []
        self._depth = 0
        self._dirty = False
        self.problems: list[str] = []
        if entries is not None:
            self.problems = self.load(entries)
            self.drain_changes()
            self._renames.clear()

    # ----------------------------------------------------------------- plumbing
    @property
    def lock(self) -> threading.RLock:
        """The lock guarding the store; hold it to make several calls at once."""
        return self._lock

    def set_clock(self, clock: Callable[[], float]) -> None:
        """Measure the ages of fed entries on ``clock`` from now on."""
        self._clock = clock

    def add_listener(self, listener: Callable[[], None]) -> None:
        """Call ``listener()`` after every change (on the changing thread, outside the lock)."""
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[], None]) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    @contextmanager
    def _write(self) -> Iterator[None]:
        with self._lock:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                notify = self._depth == 0 and self._dirty
                if notify:
                    self._dirty = False
                    listeners = list(self._listeners)
        if notify:
            for listener in listeners:
                listener()

    def _mark(self, entry: _Entry) -> None:
        self._changed.add(entry.id)
        self._dirty = True

    def _entry_by_name(self, name: str) -> _Entry:
        entry_id = self._by_name.get(name) if isinstance(name, str) else None
        if entry_id is None:
            raise KeyError(f"the blackboard has no entry named {name!r}")
        return self._entries[entry_id]

    def _entry_by_id(self, entry_id: Any) -> _Entry:
        entry = self._entries.get(entry_id) if isinstance(entry_id, str) else None
        if entry is None:
            raise OpError(f"no blackboard entry has the id {entry_id!r}")
        return entry

    def _capture_before(self, entry: _Entry) -> None:
        capture = self._capture
        if capture is not None and entry.id not in capture:
            capture[entry.id] = (entry.type, _copy(entry.type, entry.value) if entry.type != "Object" else entry.value)

    # ----------------------------------------------------------------- reading
    def names(self) -> list[str]:
        with self._lock:
            return [entry.name for entry in self._entries.values()]

    def ids(self) -> list[str]:
        with self._lock:
            return list(self._entries)

    def types(self) -> dict[str, str]:
        """``{name: type}`` of every entry, in order."""
        with self._lock:
            return {entry.name: entry.type for entry in self._entries.values()}

    def has(self, name: str) -> bool:
        with self._lock:
            return isinstance(name, str) and name in self._by_name

    def get(self, name: str) -> Any:
        """The value of entry ``name`` (a copy for List, Dictionary and Set). Raises KeyError."""
        with self._lock:
            entry = self._entry_by_name(name)
            return _copy(entry.type, entry.value)

    def type_of(self, name: str) -> str:
        """The type of entry ``name``. Raises KeyError."""
        with self._lock:
            return self._entry_by_name(name).type

    def id_of(self, name: str) -> str:
        """The id of entry ``name``. Raises KeyError."""
        with self._lock:
            return self._entry_by_name(name).id

    def name_of(self, entry_id: str) -> str | None:
        """The name of the entry with ``entry_id``, or None."""
        with self._lock:
            entry = self._entries.get(entry_id)
            return entry.name if entry is not None else None

    def is_reset(self, name: str) -> bool:
        with self._lock:
            return self._entry_by_name(name).reset

    def source_of(self, name: str) -> dict | None:
        """The source settings of entry ``name`` (a copy), or None. Raises KeyError."""
        with self._lock:
            source = self._entry_by_name(name).source
            return dict(source) if source is not None else None

    def freshness(self, name: str) -> str | None:
        """``"ok"``, ``"nodata"`` or ``"stale"`` for a sourced entry, None otherwise. Raises KeyError."""
        with self._lock:
            return self._freshness(self._entry_by_name(name), self._clock())

    def age(self, name: str) -> float | None:
        """Seconds since a sourced entry was last fed (None if never, or without a source)."""
        with self._lock:
            entry = self._entry_by_name(name)
            if entry.source is None or entry.fed_at is None:
                return None
            return max(0.0, self._clock() - entry.fed_at)

    @staticmethod
    def _freshness(entry: _Entry, now: float) -> str | None:
        if entry.source is None:
            return None
        if entry.fed_at is None:
            return "nodata"
        max_age = entry.source.get("max_age_s") or 0.0
        if max_age > 0 and now - entry.fed_at >= max_age:
            return "stale"
        return "ok"

    def next_stale_time(self) -> float | None:
        """The earliest time (on the clock) at which an entry now fresh becomes stale, or None."""
        with self._lock:
            now = self._clock()
            times = [
                entry.fed_at + entry.source["max_age_s"]
                for entry in self._entries.values()
                if entry.source is not None and entry.fed_at is not None and entry.source.get("max_age_s", 0) > 0
                and entry.fed_at + entry.source["max_age_s"] > now
            ]
            return min(times) if times else None

    def entry(self, key: str) -> dict:
        """The entry dict of the entry with id or name ``key`` (see the module docs). Raises KeyError."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = self._entry_by_name(key)
            return self._entry_dict(entry, self._clock())

    def _entry_dict(self, entry: _Entry, now: float | None = None) -> dict:
        if entry.type == "Object":
            value: Any = safe_repr(entry.value)
        else:
            try:
                value = encode_value(entry.value)
            except TypeError:
                value = safe_repr(entry.value)
        result = {
            "id": entry.id,
            "name": entry.name,
            "type": entry.type,
            "value": value,
            "reset": entry.reset,
            "source": dict(entry.source) if entry.source is not None else None,
        }
        if now is not None:
            result["freshness"] = self._freshness(entry, now)
        return result

    def to_list(self, *, ids: bool = True) -> list[dict]:
        """Every entry but Object entries in the form of the blackboard file (values encoded;
        ``reset`` and ``source`` only when set), with ``"id"`` unless ``ids`` is False. Values that
        cannot be encoded are left out."""
        result = []
        with self._lock:
            for entry in self._entries.values():
                if entry.type == "Object":
                    continue
                try:
                    value = encode_value(entry.value)
                except TypeError:
                    continue
                item: dict[str, Any] = {"name": entry.name, "type": entry.type, "value": value}
                if ids:
                    item = {"id": entry.id, **item}
                if entry.reset:
                    item["reset"] = True
                if entry.source is not None:
                    item["source"] = dict(entry.source)
                result.append(item)
        return result

    def drain_changes(self) -> dict | None:
        """What changed since the last call (see the module docs), or None."""
        with self._lock:
            if not self._changed and not self._removed and not self._order_changed:
                return None
            now = self._clock()
            entries = [self._entry_dict(entry, now) for entry in self._entries.values() if entry.id in self._changed]
            result = {
                "entries": entries,
                "removed": list(self._removed),
                "order": list(self._entries) if self._order_changed else None,
            }
            self._changed.clear()
            self._removed.clear()
            self._order_changed = False
            return result

    def drain_renames(self) -> list[tuple[str, str]]:
        """``[(old name, new name)]`` of the renames since the last call, in order."""
        with self._lock:
            renames, self._renames = self._renames, []
            return renames

    # ----------------------------------------------------------------- loading
    def load(self, entries: Any) -> list[str]:
        """Add the entries of a blackboard file (see the class docs); returns the problems."""
        if isinstance(entries, dict) and "blackboard" in entries:
            entries = entries["blackboard"]
        if not isinstance(entries, list):
            return ["the blackboard is not a list of entries"]
        problems = []
        with self._write():
            for index, item in enumerate(entries):
                if not isinstance(item, dict):
                    problems.append(f"blackboard item {index} is not an object")
                    continue
                name = item.get("name")
                try:
                    type_name = canonical_type(item.get("type"))
                    if type_name == "Object":
                        raise OpError("Object entries hold run-time values and are not loaded")
                    value = decode_value(item.get("value"))
                    reset = item.get("reset", False)
                    if not isinstance(reset, bool):
                        raise OpError(f"'reset' must be true or false, not {reset!r}")
                    source = item.get("source")
                    entry_id = item.get("id")
                    self.add(name, type_name, value, reset=reset, source=source,
                             id=entry_id if isinstance(entry_id, str) and entry_id else None)
                except (TypeError, ValueError, KeyError) as error:
                    problems.append(f"blackboard entry {name!r}: {error}")
        return problems

    # ----------------------------------------------------------------- writing
    def set(self, name: str, value: Any) -> None:
        """Set entry ``name``, creating it if needed (type inferred from ``value``; a container
        that cannot be copied becomes an Object entry). What leaf code and Set nodes use.

        Raises:
            OpError: the entry has a source, or the value does not fit its type.
        """
        with self._write():
            entry_id = self._by_name.get(name) if isinstance(name, str) else None
            if entry_id is None:
                inferred = infer_type(value)
                try:
                    self.add(name, inferred, value)
                except TypeError:
                    if not ENTRY_TYPES[inferred].container:
                        raise
                    self.add(name, "Object", value)
                return
            entry = self._entries[entry_id]
            if entry.source is not None:
                raise OpError(self._sourced_reason(entry))
            stored = self._coerce(entry, entry.type, value)
            self._capture_before(entry)
            entry.value = stored
            self._mark(entry)

    @staticmethod
    def _sourced_reason(entry: _Entry) -> str:
        topic = entry.source.get("topic") if entry.source else "?"
        return f"the entry {entry.name!r} is fed from the topic {topic}; only that topic sets its value"

    @staticmethod
    def _coerce(entry: _Entry | None, type_name: str, value: Any) -> Any:
        try:
            return coerce_value(type_name, value)
        except (TypeError, ValueError) as error:
            where = f"{entry.name!r}: " if entry is not None else ""
            raise OpError(f"{where}{error}") from error

    def add(
        self,
        name: str,
        type_name: str,
        value: Any = None,
        *,
        reset: bool = False,
        source: dict | None = None,
        id: str | None = None,  # noqa: A002 - the protocol's name
    ) -> str:
        """Create an entry (default value when ``value`` is None) and return its id.

        Raises:
            OpError: a bad or taken name or id, an unknown type, a value that does not fit, a
                malformed source.
        """
        problem = entry_name_problem(name)
        if problem is not None:
            raise OpError(problem)
        try:
            type_name = canonical_type(type_name)
        except ValueError as error:
            raise OpError(str(error)) from error
        if value is None and type_name != "Object":
            value = ENTRY_TYPES[type_name].default
        stored = self._coerce(None, type_name, value)
        checked_source = check_source(source) if source is not None else None
        with self._write():
            if name in self._by_name:
                raise OpError(f"an entry named {name!r} already exists")
            entry_id = id or new_entry_id()
            if entry_id in self._entries:
                raise OpError(f"an entry with the id {entry_id!r} already exists")
            entry = _Entry(entry_id, name, type_name, stored, bool(reset), checked_source)
            self._entries[entry_id] = entry
            self._by_name[name] = entry_id
            if self._capture is not None:
                self._capture[entry_id] = None  # created during the loop: no loop-start value
            self._order_changed = True
            self._mark(entry)
            return entry_id

    def put_object(self, name: str, value: Any) -> str:
        """Make ``name`` an Object entry holding ``value`` (replacing an entry of that name, whose
        id it keeps). Returns the id."""
        with self._write():
            entry_id = self._by_name.get(name)
            if entry_id is None:
                return self.add(name, "Object", value)
            entry = self._entries[entry_id]
            entry.type, entry.value, entry.reset, entry.source, entry.fed_at = "Object", value, False, None, None
            self._mark(entry)
            return entry_id

    def remove(self, name: str) -> None:
        """Remove entry ``name``. Raises KeyError."""
        with self._write():
            entry = self._entry_by_name(name)
            self._remove(entry)

    def _remove(self, entry: _Entry) -> None:
        del self._entries[entry.id]
        del self._by_name[entry.name]
        if self._capture is not None:
            self._capture.pop(entry.id, None)  # a removed entry is not brought back
        self._changed.discard(entry.id)
        self._removed.append(entry.id)
        self._order_changed = True
        self._dirty = True

    def rename(self, old: str, new: str) -> None:
        """Rename an entry; it keeps its id, place, Reset flag and loop-start value.

        Raises:
            KeyError: no entry ``old``.
            OpError: ``new`` is not a valid name or is taken.
        """
        with self._write():
            entry = self._entry_by_name(old)
            if new == old:
                return
            problem = entry_name_problem(new)
            if problem is not None:
                raise OpError(problem)
            if new in self._by_name:
                raise OpError(f"an entry named {new!r} already exists")
            del self._by_name[old]
            self._by_name[new] = entry.id
            entry.name = new
            self._renames.append((old, new))
            self._mark(entry)

    def retype(self, name: str, type_name: str) -> None:
        """Change the type of entry ``name``, converting its value as the widget converts a
        literal (``values.convert_literal``).

        Raises:
            KeyError: no such entry.
            OpError: an unknown type, or Object (entries cannot become or stop being Objects).
        """
        try:
            type_name = canonical_type(type_name)
        except ValueError as error:
            raise OpError(str(error)) from error
        with self._write():
            entry = self._entry_by_name(name)
            if type_name == entry.type:
                return
            if "Object" in (type_name, entry.type):
                raise OpError("an entry cannot become an Object entry or stop being one")
            value = self._coerce(entry, type_name, convert_literal(entry.value, type_name))
            self._capture_before(entry)
            entry.type, entry.value = type_name, value
            self._mark(entry)

    def set_reset(self, name: str, reset: bool) -> None:
        """Set the Reset flag of entry ``name``. Raises KeyError."""
        with self._write():
            entry = self._entry_by_name(name)
            if entry.reset != bool(reset):
                entry.reset = bool(reset)
                self._mark(entry)

    def set_source(self, name: str, source: dict | None) -> None:
        """Give entry ``name`` a source (or none). Raises KeyError, OpError."""
        checked = check_source(source) if source is not None else None
        with self._write():
            entry = self._entry_by_name(name)
            if entry.type == "Object":
                raise OpError(f"{entry.name!r} is an Object entry: it is read-only")
            if entry.source != checked:
                entry.source = checked
                entry.fed_at = None
                self._mark(entry)

    def reorder(self, ids: Iterable[str]) -> None:
        """Put the entries in the order of ``ids``; entries not named keep their order after them."""
        with self._write():
            wanted = [entry_id for entry_id in dict.fromkeys(ids) if entry_id in self._entries]
            order = wanted + [entry_id for entry_id in self._entries if entry_id not in wanted]
            if order != list(self._entries):
                self._entries = {entry_id: self._entries[entry_id] for entry_id in order}
                self._order_changed = True
                self._dirty = True

    def feed(self, key: str, value: Any) -> bool:
        """Set a sourced entry (by id or name) from its topic and renew its age. Returns whether
        anything the tree can see changed (the value, or the entry stopped being stale or without
        data); a message that repeats the value only renews the age.

        Raises:
            KeyError: no such entry.
            OpError: the value does not fit the entry's type.
        """
        with self._write():
            entry = self._entries.get(key) or self._entry_by_name(key)
            stored = self._coerce(entry, entry.type, value)
            now = self._clock()
            was_fresh = self._freshness(entry, now) == "ok"
            entry.fed_at = now
            if equal_values(entry.value, stored) and was_fresh:
                return False
            entry.value = stored
            self._mark(entry)
            return True

    # ----------------------------------------------------------------- bb.* operations
    def apply_op(self, op: dict) -> None:
        """Apply one ``bb.*`` operation of contract 5.3 whose values are Python values.

        Raises:
            OpError: the operation is refused; the message says why.
        """
        if not isinstance(op, dict):
            raise OpError("an operation is an object with 'op'")
        kind = op.get("op")
        try:
            handler = self._OPS[kind]
        except (KeyError, TypeError):
            raise OpError(f"unknown blackboard operation {kind!r}") from None
        try:
            handler(self, op)
        except KeyError as error:
            raise OpError(str(error.args[0]) if error.args else "missing field") from error

    def _op_entry(self, op: dict, writable: bool = True) -> _Entry:
        entry = self._entry_by_id(op.get("id"))
        if writable and entry.type == "Object":
            raise OpError(f"{entry.name!r} is an Object entry: it is read-only")
        return entry

    def _op_add(self, op: dict) -> None:
        item = op.get("entry")
        if not isinstance(item, dict):
            raise OpError("bb.add needs an entry")
        if canonical_type_or_none(item.get("type")) == "Object":
            raise OpError("Object entries are created by leaf modules, not by hand")
        reset = item.get("reset", False)
        if not isinstance(reset, bool):
            raise OpError("'reset' must be true or false")
        entry_id = item.get("id")
        if entry_id is not None and not isinstance(entry_id, str):
            raise OpError("an entry id is text")
        self.add(item.get("name"), item.get("type"), item.get("value"), reset=reset,
                 source=item.get("source"), id=entry_id or None)

    def _op_remove(self, op: dict) -> None:
        ids = op.get("ids")
        if not isinstance(ids, list):
            raise OpError("bb.remove needs a list of ids")
        with self._write():
            entries = [self._op_entry({"id": entry_id}) for entry_id in ids]
            for entry in entries:
                if entry.id in self._entries:
                    self._remove(entry)

    def _op_rename(self, op: dict) -> None:
        with self._write():
            entry = self._op_entry(op)
            self.rename(entry.name, op.get("name"))

    def _op_retype(self, op: dict) -> None:
        with self._write():
            entry = self._op_entry(op)
            self.retype(entry.name, op.get("type"))

    def _op_set_value(self, op: dict) -> None:
        if "value" not in op:
            raise OpError("bb.set_value needs a value")
        with self._write():
            entry = self._op_entry(op)
            if entry.source is not None:
                raise OpError(self._sourced_reason(entry))
            stored = self._coerce(entry, entry.type, op["value"])
            if entry.type != "Object" and equal_values(entry.value, stored):
                return
            self._capture_before(entry)
            entry.value = stored
            self._mark(entry)

    def _op_set_reset(self, op: dict) -> None:
        reset = op.get("reset")
        if not isinstance(reset, bool):
            raise OpError("'reset' must be true or false")
        with self._write():
            entry = self._op_entry(op)
            self.set_reset(entry.name, reset)

    def _op_move(self, op: dict) -> None:
        order = op.get("order")
        if not isinstance(order, list) or not all(isinstance(entry_id, str) for entry_id in order):
            raise OpError("bb.move needs the list of entry ids in the new order")
        with self._write():
            unknown = [entry_id for entry_id in order if entry_id not in self._entries]
            if unknown:
                raise OpError(f"no blackboard entry has the id {unknown[0]!r}")
            self.reorder(order)

    def _op_set_source(self, op: dict) -> None:
        if "source" not in op:
            raise OpError("bb.set_source needs a source (or null)")
        with self._write():
            entry = self._op_entry(op)
            self.set_source(entry.name, op["source"])

    _OPS: dict[str, Callable[[Blackboard, dict], None]] = {
        "bb.add": _op_add,
        "bb.remove": _op_remove,
        "bb.rename": _op_rename,
        "bb.retype": _op_retype,
        "bb.set_value": _op_set_value,
        "bb.set_reset": _op_set_reset,
        "bb.move": _op_move,
        "bb.set_source": _op_set_source,
    }

    # ----------------------------------------------------------------- loops
    def start_loop(self) -> None:
        """A loop begins: record the loop-start value of each entry before its first change."""
        with self._lock:
            self._capture = {}

    def end_loop(self) -> list[str]:
        """The loop ends: entries with the Reset flag get their loop-start value back. Returns the
        names of the entries that changed."""
        changed = []
        with self._write():
            captured, self._capture = self._capture, None
            for entry_id, start in (captured or {}).items():
                entry = self._entries.get(entry_id)
                if start is None or entry is None or not entry.reset or entry.source is not None:
                    continue
                type_name, value = start
                if entry.type == type_name and equal_values(entry.value, value):
                    continue
                entry.type = type_name
                entry.value = _copy(type_name, value)
                self._mark(entry)
                changed.append(entry.name)
        return changed

    def in_loop(self) -> bool:
        """True between :meth:`start_loop` and :meth:`end_loop`."""
        with self._lock:
            return self._capture is not None

    # ----------------------------------------------------------------- snapshot and restore
    def snapshot(self) -> BlackboardSnapshot:
        """A copy of every entry (Object entries by reference), for :meth:`restore`."""
        with self._lock:
            return BlackboardSnapshot([
                _Entry(e.id, e.name, e.type, _copy(e.type, e.value), e.reset,
                       dict(e.source) if e.source is not None else None)
                for e in self._entries.values()
            ])

    def restore(self, snapshot: BlackboardSnapshot) -> None:
        """Make the blackboard hold the entries of ``snapshot`` again (see the module docs)."""
        with self._write():
            wanted = {entry.id: entry for entry in snapshot.entries}
            for entry in [entry for entry in self._entries.values() if entry.id not in wanted]:
                self._remove(entry)
            self._rename_back(wanted)
            for saved in snapshot.entries:
                entry = self._entries.get(saved.id)
                if entry is None:
                    if saved.name in self._by_name:  # taken by an entry created since: removed above
                        continue
                    entry = _Entry(saved.id, saved.name, saved.type, _copy(saved.type, saved.value),
                                   saved.reset, dict(saved.source) if saved.source is not None else None)
                    self._entries[entry.id] = entry
                    self._by_name[entry.name] = entry.id
                    self._order_changed = True
                    self._mark(entry)
                    continue
                if entry.source is None or saved.source is None:
                    if entry.type != saved.type or not equal_values(entry.value, saved.value):
                        entry.type, entry.value = saved.type, _copy(saved.type, saved.value)
                        self._mark(entry)
                if entry.reset != saved.reset:
                    entry.reset = saved.reset
                    self._mark(entry)
                if entry.source != saved.source:
                    entry.source = dict(saved.source) if saved.source is not None else None
                    entry.fed_at = None
                    self._mark(entry)
            self.reorder([entry.id for entry in snapshot.entries])

    def _rename_back(self, wanted: dict[str, _Entry]) -> None:
        pending = {entry.id: wanted[entry.id].name for entry in self._entries.values()
                   if entry.id in wanted and wanted[entry.id].name != entry.name}
        while pending:
            free = [entry_id for entry_id, name in pending.items() if name not in self._by_name]
            if free:
                entry_id = free[0]
                self.rename(self._entries[entry_id].name, pending.pop(entry_id))
                continue
            # every old name is taken by another renamed entry (names were swapped): step aside
            entry_id, name = pending.popitem()
            current = self._entries[entry_id].name
            aside = next(f"{current}~{n}" for n in itertools.count(1) if f"{current}~{n}" not in self._by_name)
            self.rename(current, aside)
            pending[entry_id] = name


def canonical_type_or_none(type_name: Any) -> str | None:
    """The canonical entry type, or None for a name that is not one."""
    try:
        return canonical_type(type_name)
    except ValueError:
        return None
