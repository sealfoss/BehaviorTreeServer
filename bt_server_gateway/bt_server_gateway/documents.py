"""The tree documents the gateway holds (design 9.2, 9.4): each tree file is in memory once,
shared by every session that shows it, and its unsaved changes live on the server.

* Saving writes atomically in a worker thread (``asyncio.to_thread``).
* **Recovery** (G-18): a document with unsaved changes is also written to
  ``trees_dir/.recovery/<key>_BT.json`` within 2 s of each change (here: at once, then at most
  once a second); the file is removed when the document is saved, reverted or recovered, or its
  changes are discarded. After a restart the keys with a recovery file are offered to the page.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from bt_server_core.document import TreeDocument
from bt_server_core.files import (
    PathError,
    TreeFileError,
    atomic_write_json,
    document_key,
    load_tree,
    read_tree_file,
    resolve_in_root,
    tree_file_name,
    tree_file_path,
    write_tree_file,
)

from .board import FileWriter

__all__ = ["RECOVERY_DIR", "DocumentStore", "new_tree_data"]

log = logging.getLogger("bt_server_gateway")

#: The folder of recovery files, inside the trees folder.
RECOVERY_DIR = ".recovery"
#: Shortest time between two recovery writes of one document (each change is written within 2 s).
RECOVERY_GAP = 1.0


def new_tree_data(root_id: str) -> dict:
    """An empty tree: a Root (Sequence, Memory on), as New makes it."""
    return {
        "format": "behavior_tree_widget",
        "version": 1,
        "config": {},
        "nodes": [{"id": root_id, "type": "Root", "title": "Root", "x": 0.0, "y": 0.0,
                   "composite": "Sequence", "memory": True}],
        "connections": [],
    }


class DocumentStore:
    """The open documents, by key, and their recovery files.

    ``on_error(text)`` is told when a recovery file cannot be written (once per failure streak
    of a document)."""

    def __init__(self, trees_dir: Path, on_error: Callable[[str], None]) -> None:
        self.trees_dir = trees_dir
        self._docs: dict[str, TreeDocument] = {}
        self._recovery: dict[str, FileWriter] = {}
        self._failed: set[str] = set()
        self._on_error = on_error
        self.recovery_keys: set[str] = set()

    # ----------------------------------------------------------------- the open documents
    def get(self, key: str | None) -> TreeDocument | None:
        return self._docs.get(key) if key is not None else None

    def keys(self) -> list[str]:
        return list(self._docs)

    def add(self, document: TreeDocument) -> None:
        self._docs[document.key] = document

    def remove(self, key: str) -> None:
        self._docs.pop(key, None)
        writer = self._recovery.pop(key, None)
        if writer is not None and writer.pending:
            asyncio.get_running_loop().create_task(writer.flush())

    def rekey(self, old: str, new: str) -> None:
        """The document ``old`` is now stored as ``new`` (Save As, Rename)."""
        document = self._docs.pop(old)
        self._recovery.pop(old, None)
        document.key = new
        document.path = tree_file_name(new)
        self._docs[new] = document

    def path(self, key: str) -> Path:
        """The tree file of ``key`` (confined to the trees folder; raises PathError)."""
        return tree_file_path(self.trees_dir, key)

    def load(self, name: str, catalog: Iterable[dict]):
        """Read the tree ``name`` and its blackboard from disk (both checked completely).
        Returns ``(document, loaded)``; the document is not added to the store.

        Raises:
            PathError, TreeFileError: as :func:`bt_server_core.files.load_tree`.
        """
        loaded = load_tree(self.trees_dir, name)
        document = TreeDocument(loaded.data, list(catalog), key=loaded.key, path=loaded.path)
        return document, loaded

    def read(self, key: str, catalog: Iterable[dict]) -> TreeDocument | None:
        """The open document ``key``, or one read from disk (not added); None if unreadable."""
        found = self._docs.get(key)
        if found is not None:
            return found
        try:
            return self.load(key, catalog)[0]
        except (PathError, TreeFileError, OSError, ValueError):
            return None

    # ----------------------------------------------------------------- saving
    async def save(self, document: TreeDocument, view: dict | None = None, key: str | None = None) -> list[str]:
        """Write ``document`` to the tree file of ``key`` (default: its own); returns the save
        warnings (version 1 files). The document counts as saved only when the write worked.

        Raises:
            OSError, PathError: not written; the document stays unsaved.
        """
        target = self.path(key or document.key)
        data = document.to_file(view)
        warnings = document.save_warnings()
        await asyncio.to_thread(_write_tree, target, data)
        if key is None or key == document.key:
            document.path = tree_file_name(document.key)
            document.mark_saved()
            self.changed(document)
        return warnings

    def reload(self, key: str, catalog: Iterable[dict]) -> TreeDocument:
        """The file's state of ``key`` as a new document with a higher revision, replacing the
        open one (Revert).

        Raises:
            PathError, TreeFileError: the file cannot be read; nothing changed.
        """
        old = self._docs.get(key)
        document = self.load(key, catalog)[0]
        if old is not None:
            document.rev = old.rev + 1
        self._docs[key] = document
        self.changed(document)
        return document

    # ----------------------------------------------------------------- recovery
    def recovery_path(self, key: str) -> Path:
        return resolve_in_root(self.trees_dir, f"{RECOVERY_DIR}/{tree_file_name(key)}")

    def scan_recovery(self) -> None:
        """Find the recovery files left by an earlier run of the gateway."""
        folder = self.trees_dir / RECOVERY_DIR
        self.recovery_keys = set()
        if not folder.is_dir():
            return
        for path in folder.rglob("*_BT.json"):
            try:
                self.recovery_keys.add(document_key(path.relative_to(folder).as_posix()))
            except (PathError, ValueError):
                continue

    def changed(self, document: TreeDocument) -> None:
        """A document changed (or was saved): bring its recovery file up to date soon."""
        key = document.key
        if not document.unsaved and key not in self.recovery_keys and key not in self._recovery:
            return
        writer = self._recovery.get(key)
        if writer is None:
            writer = FileWriter(lambda: self._write_recovery(key), lambda error: self._recovery_result(key, error),
                                gap=RECOVERY_GAP)
            self._recovery[key] = writer
        writer.request()

    async def _write_recovery(self, key: str) -> None:
        document = self._docs.get(key)
        path = self.recovery_path(key)
        if document is None or not document.unsaved:
            await asyncio.to_thread(_remove, path)
            self.recovery_keys.discard(key)
            return
        data = document.to_file()
        data["recovery_of"] = document.path
        await asyncio.to_thread(_write_tree, path, data)
        self.recovery_keys.add(key)

    def _recovery_result(self, key: str, error: Exception | None) -> None:
        if error is None:
            self._failed.discard(key)
            return
        log.warning("the recovery file of %r cannot be written: %s", key, error)
        if key not in self._failed:
            self._failed.add(key)
            self._on_error(f"The recovery file of {key} cannot be written: {error}")

    def read_recovery(self, key: str) -> dict:
        """The data of a recovery file (a tree file).

        Raises:
            TreeFileError: missing or unreadable.
        """
        return read_tree_file(self.recovery_path(key))

    async def drop_recovery(self, key: str) -> None:
        """Remove the recovery file of ``key`` (its changes were saved, reverted or discarded)."""
        writer = self._recovery.pop(key, None)
        if writer is not None:
            await writer.flush()
        await asyncio.to_thread(_remove, self.recovery_path(key))
        self.recovery_keys.discard(key)

    async def flush(self) -> None:
        """Write every pending recovery file now (shutdown)."""
        for writer in list(self._recovery.values()):
            await writer.flush()


#: Attempts of a write that Windows refuses because a reader holds the file open.
_ATTEMPTS = 10


def _retrying(write, path: Path, data: object) -> None:  # noqa: ANN001
    """Make the folder and write; on Windows, replacing a file another program is reading fails
    with "access denied" for a moment, so such a write is tried again a few times."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(_ATTEMPTS):
        try:
            write(path, data)
            return
        except PermissionError:
            if os.name != "nt" or attempt == _ATTEMPTS - 1:
                raise
            time.sleep(0.02)


def _write_tree(path: Path, data: dict) -> None:
    _retrying(write_tree_file, path, data)


def _remove(path: Path) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def write_json(path: Path, data: object) -> None:
    """Write JSON atomically, making the folder first."""
    _retrying(atomic_write_json, path, data)
