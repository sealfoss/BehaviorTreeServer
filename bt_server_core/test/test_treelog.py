"""The tree log (design 8.5): line format, file names, the writer, reading records back."""

from __future__ import annotations

import datetime
import os
import re
import time
from pathlib import Path

from bt_server_core.treelog import (
    LogLevel,
    TreeLogWriter,
    delete_old_logs,
    format_line,
    log_file_name,
    read_records,
    sanitize_label,
)


def test_line_format_is_the_widgets() -> None:
    stamp = datetime.datetime(2026, 11, 13, 15, 4, 7, 414000).timestamp()
    assert format_line(stamp, LogLevel.INFO, "Node 'Move To' (MoveTo) started: Running") == \
        "2026-11-13 15:04:07.414 INFO    Node 'Move To' (MoveTo) started: Running"
    assert format_line(stamp, LogLevel.WARNING, "w") == "2026-11-13 15:04:07.414 WARNING w"
    assert format_line(stamp, LogLevel.ERROR, "e") == "2026-11-13 15:04:07.414 ERROR   e"
    assert format_line(stamp, LogLevel.DEBUG, "d") == "2026-11-13 15:04:07.414 DEBUG   d"


def test_file_name_and_label() -> None:
    start = datetime.datetime(2026, 11, 13, 15, 4, 5)
    assert log_file_name("robot", start) == "robot_11-13-2026_15-04-05.log"
    assert sanitize_label("../../etc/passwd") == "etcpasswd"
    assert sanitize_label("cells/patrol v2") == "cellspatrolv2"
    assert sanitize_label("...") == "tree"
    assert log_file_name("a b/c", start) == "abc_11-13-2026_15-04-05.log"


def test_writer_numbers_records_and_reads_them_back(tmp_path: Path) -> None:
    start = datetime.datetime(2026, 11, 13, 15, 4, 5).timestamp()
    writer = TreeLogWriter(str(tmp_path), "robot", start)
    assert os.path.basename(writer.path) == "robot_11-13-2026_15-04-05.log"
    first = writer.write(LogLevel.INFO, "Execution started")
    assert first is not None and first.seq == 0
    writer.write(LogLevel.ERROR, "x: tick raised\nTraceback (most recent call last):\n"
                                 "2026-01-01 00:00:00.000 INFO    looks like a record")
    writer.write(LogLevel.DEBUG, "  leading spaces kept", index=3)
    assert writer.write(LogLevel.NONE, "dropped") is None
    assert writer.next_seq == 3
    assert writer.flush()
    lines = Path(writer.path).read_text(encoding="utf-8").splitlines()
    assert re.match(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} INFO    Execution started$", lines[0])
    assert len(lines) == 5
    records = read_records(writer.path, 0, 10)
    assert [record.seq for record in records] == [0, 1, 2]
    assert records[1].level == LogLevel.ERROR
    assert records[1].text.endswith("2026-01-01 00:00:00.000 INFO    looks like a record")
    assert records[2].text == "  leading spaces kept"
    assert [record.seq for record in read_records(writer.path, 1, 1)] == [1]
    assert read_records(writer.path, 3, 10) == []
    writer.close()
    assert writer.write(LogLevel.INFO, "late") is None


def test_delete_old_logs(tmp_path: Path) -> None:
    old = tmp_path / "old_01-01-2026_00-00-00.log"
    new = tmp_path / "new_11-13-2026_00-00-00.log"
    other = tmp_path / "keep.txt"
    for path in (old, new, other):
        path.write_text("x")
    long_ago = time.time() - 31 * 86400
    os.utime(old, (long_ago, long_ago))
    os.utime(other, (long_ago, long_ago))
    assert delete_old_logs(str(tmp_path), 30) == [str(old)]
    assert new.exists() and other.exists()


def test_writer_never_raises_on_an_unwritable_folder(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    errors: list[str] = []
    writer = TreeLogWriter(str(blocker / "sub"), "robot", on_error=errors.append)
    writer.write(LogLevel.INFO, "a")
    writer.write(LogLevel.INFO, "b")
    assert writer.flush()
    assert len(errors) == 1
