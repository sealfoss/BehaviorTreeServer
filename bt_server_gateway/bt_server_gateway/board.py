"""The gateway's copy of a session's blackboard (design 7.2, 9.4).

While idle the gateway holds the blackboard: browsers edit it through
:meth:`bt_server_core.blackboard.Blackboard.apply_op`, the same rules the executor applies. During a
run of this session the executor holds it and the gateway mirrors its updates
(:meth:`GatewayBoard.apply_update`); the result's values then become the gateway's copy
(:meth:`GatewayBoard.replace`).

:class:`FileWriter` writes the blackboard file on every change, at most once every 250 ms, in a
worker thread and atomically (K-10, K-11). A blackboard change is never an unsaved change. The
values of entries with a source are not written: the file keeps their declared value (7.4).
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from bt_server_core.blackboard import Blackboard, BlackboardSnapshot, OpError
from bt_server_core.files import blackboard_file_data
from bt_server_core.values import encode_value

from .wire import entry_to_wire

__all__ = ["FILE_GAP", "FileWriter", "GatewayBoard"]

log = logging.getLogger("bt_server_gateway")

#: Shortest time between two writes of a blackboard file (design 9.4).
FILE_GAP = 0.25


class GatewayBoard:
    """A :class:`Blackboard` with what the gateway needs around it: freshness reported by the
    executor during a run, the declared values of sourced entries, and the changes since the
    browsers were last told (:meth:`drain`)."""

    def __init__(self, entries: list[dict] | None = None) -> None:
        self.board = Blackboard(entries or [])
        self.problems = list(self.board.problems)
        self.fresh: dict[str, tuple[str | None, float | None]] = {}
        self.issues: dict[str, str] = {}  # entry id -> why its source cannot feed it (from the executor)
        self.declared: dict[str, Any] = {}
        self._note_declared()

    # ----------------------------------------------------------------- reading
    def types(self) -> dict[str, str]:
        """Entry name -> type (for the literals of Evaluation and Set nodes)."""
        return self.board.types()

    def ids(self) -> list[str]:
        return self.board.ids()

    def entry(self, entry_id: str) -> dict | None:
        """The entry with ``entry_id``, value decoded (a copy), or None."""
        board = self.board
        with board.lock:
            name = board.name_of(entry_id)
            if name is None:
                return None
            return {"id": entry_id, "name": name, "type": board.type_of(name), "value": board.get(name),
                    "reset": board.is_reset(name), "source": board.source_of(name)}

    def wire_entry(self, entry_id: str) -> dict | None:
        entry = self.entry(entry_id)
        if entry is None:
            return None
        freshness, age = self.fresh.get(entry_id, (None, None))
        return entry_to_wire(entry, freshness, age, self.issues.get(entry_id))

    def wire_entries(self) -> list[dict]:
        return [entry for entry in (self.wire_entry(entry_id) for entry_id in self.ids()) if entry is not None]

    def to_list(self) -> list[dict]:
        """The entries in file form with ids and encoded values (what a goal carries)."""
        return self.board.to_list()

    def file_items(self) -> list[dict]:
        """The entries as the blackboard file holds them: sourced entries with their declared
        value, Object entries left out."""
        items = []
        for item in self.board.to_list(ids=True):
            if item.get("source") is not None and item["id"] in self.declared:
                try:
                    item["value"] = encode_value(self.declared[item["id"]])
                except TypeError:
                    pass
            items.append(item)
        return items

    # ----------------------------------------------------------------- changes
    def drain(self) -> tuple[dict | None, list[tuple[str, str]]]:
        """The changes since the last call (``Blackboard.drain_changes``) and the renames."""
        return self.board.drain_changes(), self.board.drain_renames()

    def apply_op(self, op: dict) -> None:
        """A ``bb.*`` operation with Python values, while the gateway holds the blackboard.

        Raises:
            OpError: refused; nothing changed.
        """
        self.board.apply_op(op)
        self._note_declared()

    def snapshot(self) -> BlackboardSnapshot:
        return self.board.snapshot()

    def restore(self, snapshot: BlackboardSnapshot) -> None:
        self.board.restore(snapshot)
        self._note_declared()

    def _note_declared(self) -> None:
        """Remember the value of each sourced entry the gateway holds: it is what the file keeps."""
        board = self.board
        with board.lock:
            for entry_id in board.ids():
                name = board.name_of(entry_id)
                if name is not None and board.type_of(name) != "Object" and board.source_of(name) is not None:
                    self.declared[entry_id] = board.get(name)
                else:
                    self.declared.pop(entry_id, None)

    def replace(self, entries: list[dict]) -> dict:
        """Hold ``entries`` (decoded values, with ids) from now on: the result of a run, or a full
        update. Returns the change to tell the browsers (every entry, the removed ids, the order)."""
        old_ids = set(self.ids())
        encoded = []
        for item in entries:
            if item.get("type") == "Object":
                continue
            try:
                encoded.append({**item, "value": encode_value(item.get("value"))})
            except TypeError:
                continue
        declared = self.declared
        self.board = Blackboard(encoded)
        self.board.drain_renames()
        self.declared = {entry_id: value for entry_id, value in declared.items() if entry_id in set(self.ids())}
        for item in entries:  # the run's Object entries stay visible until the next run
            if item.get("type") == "Object" and self.board.name_of(item["id"]) is None and not self.board.has(item["name"]):
                try:
                    self.board.add(item["name"], "Object", item.get("value"), id=item["id"])
                except OpError:
                    pass
        self.board.drain_changes()
        ids = self.ids()
        return {"entries": ids, "removed": [entry_id for entry_id in old_ids if entry_id not in set(ids)], "order": ids}

    def apply_update(self, update: dict) -> None:
        """Mirror an update of the executor's blackboard (entries named by id, decoded values;
        a known id with a new name is a rename). The changes are left for :meth:`drain`."""
        board = self.board
        with board.lock:
            for entry_id in update.get("removed") or ():
                name = board.name_of(entry_id)
                if name is not None:
                    board.remove(name)
                self.fresh.pop(entry_id, None)
                self.issues.pop(entry_id, None)
            items = [item for item in update.get("entries") or () if isinstance(item, dict) and "id" in item]
            self._rename(items)
            for item in items:
                try:
                    self._upsert(item)
                except (OpError, KeyError, TypeError, ValueError) as error:
                    log.warning("blackboard entry %r from the executor not mirrored: %s", item.get("name"), error)
                self.note_reported(item)
            if update.get("order") is not None:
                board.reorder(update["order"])

    def note_reported(self, item: dict) -> None:
        """Keep what the executor reports about an entry besides its value: freshness, age and
        the problem of its source."""
        entry_id = item["id"]
        if item.get("source") is not None or item.get("freshness") is not None:
            self.fresh[entry_id] = (item.get("freshness"), item.get("age"))
        if isinstance(item.get("problem"), str) and item["problem"]:
            self.issues[entry_id] = item["problem"]
        else:
            self.issues.pop(entry_id, None)

    def _rename(self, items: list[dict]) -> None:
        board = self.board
        pending = {item["id"]: item["name"] for item in items
                   if board.name_of(item["id"]) not in (None, item["name"])}
        while pending:
            free = [entry_id for entry_id, name in pending.items() if not board.has(name)]
            if free:
                entry_id = free[0]
                board.rename(board.name_of(entry_id), pending.pop(entry_id))
                continue
            entry_id, name = next(iter(pending.items()))  # taken: move the holder aside first
            holder = board.name_of(board.id_of(name)) or name
            aside = next(f"{holder}~{n}" for n in itertools.count(1) if not board.has(f"{holder}~{n}"))
            board.rename(holder, aside)

    def _upsert(self, item: dict) -> None:
        board = self.board
        entry_id, name, type_name = item["id"], item["name"], item["type"]
        value, reset, source = item.get("value"), bool(item.get("reset", False)), item.get("source")
        current = board.name_of(entry_id)
        if current is not None and board.type_of(current) != type_name:
            if "Object" in (type_name, board.type_of(current)):
                board.remove(current)
                current = None
            else:
                try:
                    board.retype(current, type_name)
                except OpError:
                    board.remove(current)
                    current = None
        if current is None:
            if board.has(name):
                board.remove(name)
            board.add(name, type_name, value, reset=reset, source=source, id=entry_id)
            return
        if board.source_of(current) != source:
            board.set_source(current, source)
        if board.is_reset(current) != reset:
            board.set_reset(current, reset)
        if type_name == "Object":
            board.put_object(current, value)
        elif source is not None:
            board.feed(entry_id, value)
        else:
            board.set(current, value)


class FileWriter:
    """Writes a file on every change, at most once every 250 ms (K-10): the first change is
    written at once, later ones at the earliest 250 ms after the previous write began. The data
    is taken when the write starts, so the last write holds the final state. ``write`` is
    awaited (it runs the write in a thread); ``on_result(error or None)`` reports each outcome."""

    def __init__(self, write: Callable[[], Awaitable[None]], on_result: Callable[[Exception | None], None],
                 gap: float = FILE_GAP) -> None:
        self._write = write
        self._on_result = on_result
        self._gap = gap
        self._last = float("-inf")
        self._dirty = False
        self._sleeping = False
        self._hurry = False
        self._task: asyncio.Task | None = None

    def request(self) -> None:
        """Something changed: write it soon."""
        self._dirty = True
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        while self._dirty:
            wait = 0.0 if self._hurry else self._last + self._gap - time.monotonic()
            if wait > 0:
                self._sleeping = True
                try:
                    await asyncio.sleep(wait)
                finally:
                    self._sleeping = False
            self._dirty = False
            self._last = time.monotonic()
            await self._write_once()

    async def _write_once(self) -> None:
        try:
            await self._write()
        except Exception as error:  # noqa: BLE001 - reported, and the next change tries again
            self._on_result(error)
        else:
            self._on_result(None)

    async def flush(self) -> None:
        """Write what is pending now, without waiting for the gap (Save, Load, shutdown)."""
        self._hurry = True
        try:
            task = self._task
            if task is not None and not task.done():
                if self._sleeping:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                else:
                    await task
            if self._dirty:
                self._dirty = False
                self._last = time.monotonic()
                await self._write_once()
        finally:
            self._hurry = False

    @property
    def pending(self) -> bool:
        return self._dirty or (self._task is not None and not self._task.done())


def board_file_data(board: GatewayBoard) -> dict:
    """The blackboard file's data for ``board``."""
    return blackboard_file_data(board.file_items())
