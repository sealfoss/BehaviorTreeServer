"""The tree log (design 8.5): levels, the widget's line format, the per-run log file and reading
records back by number.

Levels and their numbers are the widget's (``behavior_tree_widget.tree_log``): 0 None, 1 ERROR,
2 WARNING, 3 DEBUG, 4 INFO, INFO being the most detailed. A tree's *Log level* option is the
threshold: a record whose level number is higher is not logged, and 0 logs nothing.

Each run writes one file, ``<label>_<month>-<day>-<year>_<hour>-<minute>-<second>.log`` with the
time the run started, in the executor's ``log_dir``. Only letters, digits, ``-``, ``_`` and ``.``
of the label are kept (:func:`sanitize_label`), so a label cannot name another folder. Lines
have the widget's format::

    2026-11-13 15:04:07.414 INFO    Node 'Move To' (MoveTo) started: Running

A record whose text has several lines (a traceback) continues on the following lines, as in
the widget. A continuation line that would look like the start of a record is written with one
leading space, so that :func:`read_records` can count records exactly.

Records are numbered from 0 in each run (``seq``). :class:`TreeLogWriter` never blocks: it hands
records to a queue that one background thread (shared by every writer) writes to the files.
:func:`read_records` reads records back from a file by number, for ``~/get_log``; the node index
is not in the file, so records read back carry ``index == -1``.

API::

    writer = TreeLogWriter(log_dir, label, start=time.time())
    record = writer.write(LogLevel.INFO, "Execution started", index=-1)   # LogRecord(seq=0, ...)
    writer.flush(); writer.close()
    read_records(writer.path, from_seq=0, max_records=100) -> list[LogRecord]
    delete_old_logs(log_dir, keep_days=30) -> list[str]   # the files deleted
"""

from __future__ import annotations

import datetime
import enum
import os
import queue
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

__all__ = [
    "LEVEL_NAMES",
    "LogLevel",
    "LogRecord",
    "TreeLogWriter",
    "delete_old_logs",
    "format_line",
    "log_file_name",
    "read_records",
    "sanitize_label",
    "time_text",
]


class LogLevel(enum.IntEnum):
    """Precedence of a log record (lower numbers are more important); the widget's levels."""

    NONE = 0
    ERROR = 1
    WARNING = 2
    DEBUG = 3
    INFO = 4

    @classmethod
    def coerce(cls, value: Any) -> LogLevel:
        """A LogLevel from a LogLevel, its number (0 to 4) or its name (``"debug"``).

        Raises:
            ValueError: ``value`` names no level.
            TypeError: ``value`` is of another type (a bool, for example).
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, bool):
            raise TypeError("a log level is a LogLevel, an int from 0 to 4 or a level name, not a bool")
        if isinstance(value, int):
            try:
                return cls(value)
            except ValueError:
                raise ValueError(f"log levels are numbered 0 to 4, not {value}") from None
        if isinstance(value, str):
            try:
                return cls[value.strip().upper()]
            except KeyError:
                raise ValueError(f"unknown log level {value!r}") from None
        raise TypeError(f"a log level is a LogLevel, an int from 0 to 4 or a level name, not {type(value).__name__}")


#: The name each level has in the files (the names of Python's logging levels, as in the widget).
LEVEL_NAMES = {LogLevel.ERROR: "ERROR", LogLevel.WARNING: "WARNING", LogLevel.DEBUG: "DEBUG", LogLevel.INFO: "INFO"}
_LEVEL_BY_NAME = {name: level for level, name in LEVEL_NAMES.items()}

LOG_TIME_FORMAT = "%m-%d-%Y_%H-%M-%S"
LOG_EXTENSION = ".log"
_LINE_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_RECORD_START = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) (ERROR  |WARNING|DEBUG  |INFO   ) (.*)$")
_LABEL_CHARACTERS = re.compile(r"[^A-Za-z0-9._-]")


@dataclass(frozen=True)
class LogRecord:
    """One record of a run's log. ``stamp``: seconds since the epoch; ``index``: the node it is
    about, -1 for none (always -1 when read back from a file)."""

    seq: int
    stamp: float
    level: int
    text: str
    index: int = -1

    def line(self) -> str:
        """The record as it is written to the file (without the line end)."""
        return format_line(self.stamp, self.level, self.text)


def time_text(stamp: float) -> str:
    """``2026-11-13 15:04:07.414``: the time column of the log file (local time, milliseconds
    cut off as Python's logging does)."""
    moment = datetime.datetime.fromtimestamp(stamp)
    millis = int((stamp - int(stamp)) * 1000) if stamp >= 0 else moment.microsecond // 1000
    return f"{moment.strftime(_LINE_TIME_FORMAT)}.{millis:03d}"


def format_line(stamp: float, level: int, text: str) -> str:
    """A record in the widget's format: ``<time> <LEVEL, padded to 7> <text>``. Continuation
    lines of a multi-line text that would look like a record of their own get a leading space."""
    name = LEVEL_NAMES.get(LogLevel(level), "INFO")
    lines = str(text).split("\n")
    rest = [(" " + line) if _RECORD_START.match(line) else line for line in lines[1:]]
    return "\n".join([f"{time_text(stamp)} {name:<7} {lines[0]}", *rest])


def sanitize_label(label: str) -> str:
    """The label as it may appear in a file name: letters, digits, ``-``, ``_`` and ``.`` only,
    without leading dots; ``tree`` if nothing is left."""
    cleaned = _LABEL_CHARACTERS.sub("", str(label)).lstrip(".")
    return cleaned or "tree"


def log_file_name(label: str, start: datetime.datetime | float) -> str:
    """``<label>_<month>-<day>-<year>_<hour>-<minute>-<second>.log`` for a run started at ``start``
    (a datetime, or seconds since the epoch)."""
    if not isinstance(start, datetime.datetime):
        start = datetime.datetime.fromtimestamp(start)
    return f"{sanitize_label(label)}_{start.strftime(LOG_TIME_FORMAT)}{LOG_EXTENSION}"


def delete_old_logs(log_dir: str, keep_days: float, now: float | None = None) -> list[str]:
    """Delete the ``.log`` files in ``log_dir`` last written more than ``keep_days`` days ago.
    Returns the paths deleted; files that cannot be deleted are left alone."""
    if keep_days <= 0 or not os.path.isdir(log_dir):
        return []
    limit = (time.time() if now is None else now) - keep_days * 86400.0
    deleted = []
    for entry in os.scandir(log_dir):
        if not entry.is_file() or not entry.name.endswith(LOG_EXTENSION):
            continue
        try:
            if entry.stat().st_mtime < limit:
                os.remove(entry.path)
                deleted.append(entry.path)
        except OSError:
            continue
    return deleted


def read_records(path: str, from_seq: int = 0, max_records: int = 1000) -> list[LogRecord]:
    """Records ``from_seq`` onwards (at most ``max_records``) of the log file ``path``, numbered
    from 0 at the top of the file. A missing file gives no records."""
    records: list[LogRecord] = []
    if max_records <= 0:
        return records
    try:
        handle = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return records
    seq = -1
    current: list[Any] | None = None  # [stamp, level, lines]
    with handle:
        for raw in handle:
            line = raw.rstrip("\n").rstrip("\r")
            match = _RECORD_START.match(line)
            if match is None:
                if current is not None:
                    current[2].append(line[1:] if line.startswith(" ") and _RECORD_START.match(line[1:]) else line)
                continue
            if current is not None and seq >= from_seq:
                records.append(_record(seq, current))
                if len(records) >= max_records:
                    return records
            seq += 1
            current = [_parse_time(match.group(1)), _LEVEL_BY_NAME[match.group(2).rstrip()], [match.group(3)]]
    if current is not None and seq >= from_seq and len(records) < max_records:
        records.append(_record(seq, current))
    return records


def _record(seq: int, current: list[Any]) -> LogRecord:
    return LogRecord(seq, current[0], int(current[1]), "\n".join(current[2]))


def _parse_time(text: str) -> float:
    try:
        return datetime.datetime.strptime(text, _LINE_TIME_FORMAT + ".%f").timestamp()
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------------------------
# The writer thread: one queue and one daemon thread write the files of every writer, in the
# order the records were written.

_queue: queue.SimpleQueue = queue.SimpleQueue()
_thread: threading.Thread | None = None
_thread_lock = threading.Lock()


def _ensure_thread() -> None:
    global _thread
    with _thread_lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_write_forever, name="bt_server_core:treelog", daemon=True)
            _thread.start()


def _write_forever() -> None:
    while True:
        item = _queue.get()
        try:
            item()
        except Exception:  # noqa: BLE001 - the writer thread must survive anything
            pass


class TreeLogWriter:
    """Writes the log of one run to its own file (see the module docs). Thread-safe; never blocks.

    ``on_error(text)`` is called (on the writer thread) once per failure to write the file, for
    example to show a notice; a failed record is dropped and the next one tries again.
    """

    def __init__(
        self,
        log_dir: str,
        label: str,
        start: float | None = None,
        *,
        keep_days: float = 0.0,
        on_error: Any = None,
    ) -> None:
        start = time.time() if start is None else start
        self.path = os.path.join(log_dir, log_file_name(label, start))
        self._log_dir = log_dir
        self._lock = threading.Lock()
        self._next_seq = 0
        self._handle: Any = None
        self._failed = False
        self._closed = False
        self._on_error = on_error
        _ensure_thread()
        if keep_days > 0:
            _queue.put(lambda: delete_old_logs(log_dir, keep_days))

    @property
    def next_seq(self) -> int:
        """The number the next record gets (the count of records written so far)."""
        with self._lock:
            return self._next_seq

    def write(self, level: int, text: str, *, index: int = -1, stamp: float | None = None) -> LogRecord | None:
        """Queue a record for the file and return it; None after :meth:`close` or for level 0."""
        if int(level) == 0:
            return None
        with self._lock:
            if self._closed:
                return None
            record = LogRecord(self._next_seq, time.time() if stamp is None else stamp, int(level), str(text), index)
            self._next_seq += 1
        _queue.put(lambda: self._write_line(record.line()))
        return record

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until every record queued so far is in the file; False after ``timeout`` seconds."""
        done = threading.Event()
        _queue.put(lambda: (self._flush_file(), done.set()))
        return done.wait(timeout)

    def close(self) -> None:
        """Write what was queued and close the file; later records are dropped."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        _queue.put(self._close_file)

    # ----------------------------------------------------------------- writer thread only
    def _write_line(self, line: str) -> None:
        try:
            if self._handle is None:
                os.makedirs(self._log_dir, exist_ok=True)
                self._handle = open(self.path, "a", encoding="utf-8")
            self._handle.write(line + "\n")
            if _queue.empty():
                self._handle.flush()
            self._failed = False
        except OSError as error:
            if not self._failed and self._on_error is not None:
                try:
                    self._on_error(f"the log file '{self.path}' cannot be written: {error}")
                except Exception:  # noqa: BLE001
                    pass
            self._failed = True
            self._close_file()

    def _flush_file(self) -> None:
        if self._handle is not None:
            try:
                self._handle.flush()
            except OSError:
                pass

    def _close_file(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
