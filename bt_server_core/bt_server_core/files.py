"""Tree names, paths confined to the trees folder, atomic writes, and reading and writing the
widget's tree and blackboard files (design 9.4, 12, 13; contract 2.1).

Names follow the widget (``serialization.tree_name``): ``robot``, ``robot.json``,
``robot_BT.json`` and ``robot_BB.json`` all mean the tree stored in ``robot_BT.json`` and
``robot_BB.json``. A tree is named on the server by its **document key**: its path relative to
the trees folder, POSIX separators, without ``_BT.json`` (``robot``, ``cells/patrol``).

Every path is resolved and refused unless it lies inside the trees folder (G-16): ``..`` that
leads out, absolute paths and symbolic links that lead out all raise :class:`PathError`.

Each file carries its own format version (design 12): the server reads versions 1 and 2 and
refuses higher ones with the widget's kind of message. A tree file is written with the version
:class:`bt_server_core.document.TreeDocument` decides; a blackboard file gets version 2 only when
one of its entries has a ``source``.
"""

from __future__ import annotations

import errno
import math
import os
import posixpath
import re
import secrets
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .values import (
    canonical_type,
    coerce_value,
    decode_value,
    dumps_json,
    entry_name_problem,
    loads_json,
)

__all__ = [
    "BLACKBOARD_FORMAT",
    "BLACKBOARD_SUFFIX",
    "LEGACY_SUFFIX",
    "READ_VERSION",
    "TREE_FORMAT",
    "TREE_SUFFIX",
    "LoadedTree",
    "PathError",
    "TreeFileError",
    "atomic_write_bytes",
    "atomic_write_json",
    "blackboard_file_data",
    "blackboard_file_path",
    "canonical_key",
    "check_tree_data",
    "document_key",
    "find_tree_file",
    "is_absolute_name",
    "key_of_file",
    "load_tree",
    "parse_blackboard",
    "parse_source",
    "read_blackboard_file",
    "read_json",
    "read_tree_file",
    "resolve_in_root",
    "subtree_key",
    "tree_file_name",
    "tree_file_path",
    "tree_name",
    "write_blackboard_file",
    "write_tree_file",
]

TREE_FORMAT = "behavior_tree_widget"
BLACKBOARD_FORMAT = "behavior_tree_widget_blackboard"
#: The highest format version the server reads (and writes).
READ_VERSION = 2
TREE_SUFFIX = "_BT.json"
BLACKBOARD_SUFFIX = "_BB.json"
#: A single-file tree of the widget's early versions (0.6.0 and before): ``robot.json``.
LEGACY_SUFFIX = ".json"

_JSON = ".json"
_TREE_TAG = "_BT"
_BLACKBOARD_TAG = "_BB"
_WINDOWS_DRIVE = re.compile(r"[A-Za-z]:")

#: Keys of a ``source`` (design 7.4) and their defaults.
SOURCE_DEFAULTS = {"field": "", "max_rate_hz": 2.0, "max_age_s": 0.0}


class TreeFileError(ValueError):
    """A tree or blackboard file cannot be read or is not such a file; the message says why."""


class PathError(ValueError):
    """A name that cannot be used: outside the trees folder, absolute, or malformed."""


# ============================================================================================ names
def tree_name(name: str) -> str:
    """The tree name of a file name (the widget's rule): its last part without ``.json`` and
    without a ``_BT`` / ``_BB`` suffix (case-insensitive): ``robot`` for ``robot_BT.json``."""
    base = posixpath.basename(name.replace("\\", "/"))
    if base.lower().endswith(_JSON):
        base = base[: -len(_JSON)]
    for tag in (_TREE_TAG, _BLACKBOARD_TAG):
        if base.upper().endswith(tag) and len(base) > len(tag):
            return base[: -len(tag)]
    return base


def is_absolute_name(name: str) -> bool:
    """True for an absolute path of either system: ``/x``, ``\\x``, ``C:\\x``, ``C:x``, ``\\\\server\\x``."""
    text = name.replace("\\", "/")
    return text.startswith("/") or bool(_WINDOWS_DRIVE.match(text))


def _clean_relative(name: str, what: str) -> str:
    """``name`` (``\\`` read as ``/``) as a normalised relative POSIX path that stays inside the
    folder it is relative to.

    Raises:
        PathError: empty, absolute, holding a NUL, or leading outside with ``..``.
    """
    if not isinstance(name, str) or not name.strip():
        raise PathError(f"{what} is empty")
    if "\x00" in name:
        raise PathError(f"{what} {name!r} holds a NUL character")
    if is_absolute_name(name):
        raise PathError(f"{what} {name!r} is an absolute path; only names inside the trees folder can be used")
    text = posixpath.normpath(name.strip().replace("\\", "/"))
    if text == ".." or text.startswith("../"):
        raise PathError(f"{what} {name!r} leads outside the trees folder")
    if text in ("", "."):
        raise PathError(f"{what} {name!r} names no file")
    return text


def document_key(name: str) -> str:
    """The document key of a tree name given in any of the widget's four spellings, relative to
    the trees folder: ``cells\\patrol_BT.json`` -> ``cells/patrol``.

    Raises:
        PathError: the name is absolute, leads outside the trees folder or is empty.
    """
    text = _clean_relative(name, "the tree name")
    folder, _ = posixpath.split(text)
    base = tree_name(text)
    if not base or base in (".", ".."):
        raise PathError(f"the tree name {name!r} names no tree")
    return posixpath.join(folder, base) if folder else base


def tree_file_name(key: str) -> str:
    """The tree file of a document key, relative to the trees folder: ``cells/patrol_BT.json``."""
    return key + TREE_SUFFIX


def key_of_file(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> str:
    """The document key of a file inside ``root``.

    Raises:
        PathError: the file lies outside ``root``.
    """
    resolved = Path(os.path.realpath(path))
    base = Path(os.path.realpath(root))
    try:
        relative = resolved.relative_to(base)
    except ValueError as error:
        raise PathError(f"'{path}' lies outside the trees folder") from error
    return document_key(relative.as_posix())


def resolve_in_root(root: str | os.PathLike[str], relative: str) -> Path:
    """The absolute path of ``relative`` (POSIX or Windows separators) inside ``root``, with
    symbolic links resolved.

    Raises:
        PathError: the path is absolute, or it or a link on the way leads outside ``root``.
    """
    text = _clean_relative(relative, "the path")
    base = Path(os.path.realpath(root))
    resolved = Path(os.path.realpath(base.joinpath(*text.split("/"))))
    if resolved != base and base not in resolved.parents:
        raise PathError(f"the path {relative!r} leads outside the trees folder")
    return resolved


def tree_file_path(root: str | os.PathLike[str], key: str) -> Path:
    """``<root>/<key>_BT.json``, confined to ``root`` (see :func:`resolve_in_root`)."""
    return resolve_in_root(root, key + TREE_SUFFIX)


def blackboard_file_path(root: str | os.PathLike[str], key: str) -> Path:
    """``<root>/<key>_BB.json``, confined to ``root``."""
    return resolve_in_root(root, key + BLACKBOARD_SUFFIX)


def find_tree_file(root: str | os.PathLike[str], key: str) -> Path | None:
    """The file holding the tree ``key``: ``<key>_BT.json``, else a single-file tree of the
    widget's early versions, ``<key>.json``; None when neither exists."""
    for suffix in (TREE_SUFFIX, LEGACY_SUFFIX):
        path = resolve_in_root(root, key + suffix)
        if path.is_file():
            return path
    return None


def canonical_key(root: str | os.PathLike[str], key: str) -> str:
    """``key`` spelt as the files on disk are: on a file system that ignores case (Windows,
    macOS), ``ROBOT`` and ``robot`` name one file, so they must name one document. A key whose
    tree does not exist, or that reaches its file through a symbolic link, is returned as given.

    Raises:
        PathError: the key leads outside the trees folder.
    """
    path = find_tree_file(root, key)
    if path is None:
        return key
    on_disk = key_of_file(path, root)
    return on_disk if on_disk.casefold() == key.casefold() else key


def subtree_key(holder_key: str, file_name: str) -> str:
    """The document key of a Sub-Tree's (or Remote Sub-Tree's) ``File``: relative to the folder
    of the tree holding the node (the widget's rule), ``\\`` read as ``/``.

    Raises:
        PathError: the name is empty, absolute (it then has to be chosen again, F-07) or
            leads outside the trees folder.
    """
    if not isinstance(file_name, str) or not file_name.strip():
        raise PathError("the node has no file")
    if is_absolute_name(file_name):
        raise PathError(f"the file {file_name!r} is an absolute path; choose the file again")
    folder = posixpath.dirname(holder_key)
    joined = posixpath.join(folder, file_name.strip().replace("\\", "/")) if folder else file_name
    return document_key(joined)


# ============================================================================================ writing
def _new_file_mode() -> int:
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


def _create_temp_file(path: Path) -> tuple[int, Path]:
    """A new temporary file next to ``path`` (not ``tempfile``: on Windows it retries for a long
    time in a folder that refuses new files, as the widget notes)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
    for _ in range(100):
        temp = path.with_name(f".bt_{secrets.token_hex(6)}.json.tmp")
        try:
            return os.open(temp, flags, 0o600), temp
        except FileExistsError:
            continue
    raise FileExistsError(errno.EEXIST, f"could not create a temporary file in '{path.parent}'")


def _sync_folder(folder: Path) -> None:
    """Make a rename in ``folder`` durable (POSIX; Windows has no folder handles for this)."""
    if sys.platform == "win32":
        return
    try:
        handle = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    except OSError:
        pass
    finally:
        os.close(handle)


def atomic_write_bytes(path: str | os.PathLike[str], content: bytes) -> None:
    """Write ``content`` to ``path`` so that the file is either the old one or the complete new one
    (design 9.4): a temporary file in the same folder, flushed and synced, then renamed over the
    old one. The permissions of an existing file are kept.

    Raises:
        OSError: the file could not be written; the old file is unchanged.
    """
    target = Path(os.path.realpath(path))
    if target.is_dir():
        raise IsADirectoryError(errno.EISDIR, f"could not write '{target}': it is a folder")
    if not target.parent.is_dir():
        raise FileNotFoundError(errno.ENOENT, f"could not write '{target}': the folder '{target.parent}' does not exist")
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        mode = _new_file_mode()
    handle, temp = _create_temp_file(target)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.chmod(temp, mode)
        except OSError:
            pass
        os.replace(temp, target)
        _sync_folder(target.parent)
    except BaseException:
        try:
            os.chmod(temp, stat.S_IREAD | stat.S_IWRITE)
            os.remove(temp)
        except OSError:
            pass
        raise


def atomic_write_json(path: str | os.PathLike[str], data: Any) -> None:
    """Write JSON as the widget does (2-space indent, UTF-8, ``NaN``/``Infinity`` allowed, a final
    newline), atomically. Text that cannot be encoded as UTF-8 (lone surrogates) is written with
    ASCII escapes instead, as in the widget."""
    try:
        content = dumps_json(data).encode("utf-8")
    except UnicodeEncodeError:
        import json

        content = json.dumps(data, indent=2, ensure_ascii=True, allow_nan=True).encode("ascii")
    atomic_write_bytes(path, content + b"\n")


def write_tree_file(path: str | os.PathLike[str], data: dict) -> None:
    """Write tree file data (:meth:`TreeDocument.to_file`) atomically."""
    atomic_write_json(path, data)


def blackboard_file_data(items: list[dict]) -> dict:
    """The blackboard file of ``items`` in file form (``name``, ``type``, ``value`` encoded with
    :func:`values.encode_value`, ``reset``, ``source``). Only those keys are written (an ``id``
    or a freshness is not); ``reset`` only when true, ``source`` only when set. The file has
    version 2 when an entry has a source, else 1 (design 12)."""
    entries = []
    for item in items:
        entry = {"name": item["name"], "type": item["type"], "value": item.get("value")}
        if item.get("reset") is True:
            entry["reset"] = True
        if item.get("source") is not None:
            entry["source"] = item["source"]
        entries.append(entry)
    version = 2 if any("source" in entry for entry in entries) else 1
    return {"format": BLACKBOARD_FORMAT, "version": version, "blackboard": entries}


def write_blackboard_file(path: str | os.PathLike[str], items: list[dict]) -> None:
    """Write a blackboard file (:func:`blackboard_file_data`) atomically."""
    atomic_write_json(path, blackboard_file_data(items))


# ============================================================================================ reading
def read_json(path: str | os.PathLike[str]) -> Any:
    """Parse a JSON file (UTF-8, a BOM allowed, ``NaN`` allowed).

    Raises:
        TreeFileError: it cannot be read or is not JSON.
    """
    name = os.path.basename(path)
    try:
        with open(path, encoding="utf-8-sig") as stream:
            return loads_json(stream.read())
    except OSError as error:
        raise TreeFileError(f"Could not read '{name}': {error.strerror or error}") from error
    except (ValueError, RecursionError, MemoryError) as error:
        raise TreeFileError(f"'{name}' is not a valid JSON file: {error}") from error


def _check_version(data: dict, name: str) -> int:
    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise TreeFileError(f"'{name}' has an invalid format version: {version!r}")
    if version > READ_VERSION:
        raise TreeFileError(
            f"'{name}' was written by a newer version of the Behavior Tree Server "
            f"(format {version}; this version reads up to {READ_VERSION})."
        )
    return version


def check_tree_data(data: Any, name: str) -> dict:
    """The widget's checks of a tree file's data (``read_tree_file``), for versions 1 and 2.

    Raises:
        TreeFileError: not a tree file, a version this server cannot read, or a malformed part.
    """
    if not isinstance(data, dict) or data.get("format") != TREE_FORMAT:
        raise TreeFileError(f"'{name}' is not a behavior tree file.")
    _check_version(data, name)
    for key, kind in (("nodes", list), ("connections", list), ("blackboard", list), ("config", dict)):
        if key in data and not isinstance(data[key], kind):
            raise TreeFileError(f"'{name}' is malformed: '{key}' must be a {kind.__name__}.")
    return data


def read_tree_file(path: str | os.PathLike[str]) -> dict:
    """Read a tree file (also a single-file tree of the widget's early versions).

    Raises:
        TreeFileError: see :func:`check_tree_data`.
    """
    return check_tree_data(read_json(path), os.path.basename(path))


def read_blackboard_file(path: str | os.PathLike[str]) -> list:
    """Read a blackboard file and return its list of entries (checked by :func:`parse_blackboard`).

    Raises:
        TreeFileError: not a blackboard file, a version this server cannot read, or malformed.
    """
    data = read_json(path)
    name = os.path.basename(path)
    if not isinstance(data, dict) or data.get("format") != BLACKBOARD_FORMAT:
        raise TreeFileError(f"'{name}' is not a blackboard file.")
    _check_version(data, name)
    items = data.get("blackboard", [])
    if not isinstance(items, list):
        raise TreeFileError(f"'{name}' is malformed: 'blackboard' must be a list.")
    return items


def _positive_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def parse_source(source: Any) -> dict:
    """A checked ``source`` of an entry (design 7.4) with its defaults filled in.

    Raises:
        ValueError: malformed; the message says why.
    """
    if not isinstance(source, dict):
        raise ValueError("a source must be an object")
    topic, type_name = source.get("topic"), source.get("type")
    if not isinstance(topic, str) or not topic.strip():
        raise ValueError("a source needs a topic")
    if not isinstance(type_name, str) or not type_name.strip():
        raise ValueError("a source needs a message type")
    result = {"topic": topic, "type": type_name, **SOURCE_DEFAULTS}
    field_name = source.get("field", "")
    if not isinstance(field_name, str):
        raise ValueError("a source's field must be text")
    result["field"] = field_name
    for key, minimum_open in (("max_rate_hz", True), ("max_age_s", False)):
        value = source.get(key, SOURCE_DEFAULTS[key])
        if not _positive_number(value) or value < 0 or (minimum_open and value == 0):
            raise ValueError(f"a source's {key} must be a number {'above' if minimum_open else 'of at least'} 0")
        result[key] = float(value)
    return result


def parse_blackboard(items: Any) -> tuple[list[dict], list[str]]:
    """Check the entries of a blackboard file (the widget's ``BlackboardStore.parse_items``, plus
    sources). Returns ``(entries, problems)``: entries as ``{"name", "type", "value" (decoded),
    "reset", "source" (or None)}``, and messages about the invalid ones, which are left out."""
    entries: list[dict] = []
    problems: list[str] = []
    if not isinstance(items, list):
        return entries, ["the blackboard section is not a list"]
    seen: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            problems.append(f"blackboard item {index} is not an object")
            continue
        name = item.get("name")
        try:
            if not isinstance(name, str):
                raise TypeError("the entry name is missing")
            problem = entry_name_problem(name)
            if problem is not None:
                raise ValueError(problem)
            if name in seen:
                raise ValueError("duplicate entry name")
            type_name = canonical_type(item.get("type"))
            if type_name == "Object":
                raise ValueError("Object entries hold run-time values and are not loaded from files")
            value = coerce_value(type_name, decode_value(item.get("value")))
            source = parse_source(item["source"]) if item.get("source") is not None else None
        except (TypeError, ValueError, KeyError) as error:
            problems.append(f"blackboard entry {name!r}: {error}")
            continue
        seen.add(name)
        flag = item.get("reset", False)
        if flag is not True and flag is not False:
            problems.append(f"blackboard entry {name!r}: 'reset' must be true or false, not {flag!r}; it is off")
        entries.append({"name": name, "type": type_name, "value": value, "reset": flag is True, "source": source})
    return entries, problems


@dataclass
class LoadedTree:
    """A tree read from the trees folder, as the widget's Load reads it.

    Attributes:
        key: The document key.
        path: The file read, relative to the trees folder (``robot_BT.json``, or ``robot.json``
            for a single-file tree of the widget's early versions).
        data: The tree file's data, checked by :func:`check_tree_data`.
        blackboard: The blackboard items: those of ``<key>_BB.json`` when it exists, else the
            ``blackboard`` list of an early single-file tree, else empty. Check them with
            :func:`parse_blackboard`.
        has_blackboard_file: Whether ``<key>_BB.json`` exists.
    """

    key: str
    path: str
    data: dict
    blackboard: list
    has_blackboard_file: bool


def load_tree(root: str | os.PathLike[str], name: str) -> LoadedTree:
    """Read the tree ``name`` (any of the widget's four spellings) and its blackboard; both files
    are checked completely before anything is returned.

    Raises:
        PathError: the name leads outside the trees folder.
        TreeFileError: no such tree, or a file cannot be read.
    """
    key = document_key(name)
    path = find_tree_file(root, key)
    if path is None:
        raise TreeFileError(f"'{tree_file_name(key)}' does not exist")
    key = canonical_key(root, key)
    data = read_tree_file(path)
    blackboard_path = blackboard_file_path(root, key)
    has_file = blackboard_path.is_file()
    blackboard = read_blackboard_file(blackboard_path) if has_file else list(data.get("blackboard", []))
    relative = path.relative_to(Path(os.path.realpath(root))).as_posix()
    return LoadedTree(key, relative, data, blackboard, has_file)
