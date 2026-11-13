"""The gateway's state and the browser protocol's dispatch (design 9, 10; contract 5).

:class:`Gateway` holds the sessions (one per executor: the configured ones always, found ones
added), the documents (one in-memory copy per tree file, shared by every session), the browser
connections, the session memory (which tree each session has open and the run it started, in
``state_dir/sessions.json``, restored at start) and the executors' last catalogs
(``state_dir/catalogs/``, so a tree can be edited and saved while its executor is down, G-22).

Every request of a browser is answered by exactly one ``ok`` or ``no`` with its ``cid``, and
passes through :func:`bt_server_gateway.security.allow`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import posixpath
import shutil
import time
import uuid
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from bt_server_core.document import REMOTE_SUB_TREE, SUB_TREE, OpRefused, TreeDocument
from bt_server_core.files import (
    BLACKBOARD_SUFFIX,
    TREE_FORMAT,
    TREE_SUFFIX,
    PathError,
    TreeFileError,
    blackboard_file_path,
    document_key,
    atomic_write_bytes,
    check_tree_data,
    find_tree_file,
    parse_blackboard,
    read_blackboard_file,
    read_json,
    resolve_in_root,
    subtree_key,
    tree_file_name,
    tree_name,
)
from bt_server_core.node_types import builtin_catalog
from bt_server_core.promote import promote
from bt_server_core.treelog import format_line
from bt_server_core.values import encode_value, infer_type, loads_json, parse_literal, to_wire

from .board import GatewayBoard
from .client import Client
from .documents import DocumentStore, new_tree_data, write_json
from .link import CommandRefused, LinkDown, LinkFactory
from .security import allow
from .session import Reply, Session
from .settings import Settings
from .wire import log_line, value_to_wire

__all__ = ["PROTOCOL", "Gateway"]

log = logging.getLogger("bt_server_gateway")

#: The browser protocol's number (contract 5).
PROTOCOL = 1
#: How often sessions are checked for an executor that stopped reporting.
LOST_CHECK = 0.5


class Gateway:
    """See the module docs. ``factory`` makes the executor links (contract 6)."""

    def __init__(self, settings: Settings, factory: LinkFactory) -> None:
        self.settings = settings
        self.factory = factory
        self.trees_dir = Path(os.path.realpath(settings.trees_dir))
        self.state_dir = Path(settings.state_dir or self.trees_dir / ".gateway")
        self.docs = DocumentStore(self.trees_dir, self._notice_all_error)
        self.sessions: dict[str, Session] = {}
        self.clients: dict[str, Client] = {}
        self.memory: dict[str, dict] = {}
        self.catalogs: dict[str, list[dict]] = {}
        self._tasks: set[asyncio.Task] = set()
        self._checker: asyncio.Task | None = None
        self._memory_failed = False

    # ================================================================== lifecycle
    async def start(self) -> None:
        self.trees_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.memory = self._read_state("sessions.json", {})
        for path in (self.state_dir / "catalogs").glob("*.json") if (self.state_dir / "catalogs").is_dir() else ():
            data = self._read_state(f"catalogs/{path.name}", None)
            if isinstance(data, dict) and isinstance(data.get("name"), str) and isinstance(data.get("types"), list):
                self.catalogs[data["name"]] = data["types"]
        self.docs.scan_recovery()
        await self.factory.start(self._on_executors)
        for name in [*self.settings.executors, *self.memory]:
            await self.add_session(name)
        while pending := [task for task in self._tasks if not task.done()]:  # sessions of executors found
            await asyncio.wait(pending)
        self._checker = asyncio.get_running_loop().create_task(self._check_lost())

    async def close(self) -> None:
        """Stop: write the blackboard and recovery files first (design 16), then close the links."""
        if self._checker is not None:
            self._checker.cancel()
        for client in list(self.clients.values()):
            await client.stop()
        for session in self.sessions.values():
            await session.board_writer.flush()
        await self.docs.flush()
        for session in self.sessions.values():
            try:
                await session.link.close()
            except Exception:  # noqa: BLE001 - closing the others matters more
                log.exception("closing the link to %s failed", session.name)
        await self.factory.close()
        for task in list(self._tasks):
            task.cancel()

    def spawn(self, coroutine: Coroutine) -> asyncio.Task:
        """Run ``coroutine`` as a task the gateway keeps until it ends."""
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("a gateway task failed", exc_info=task.exception())

    async def _check_lost(self) -> None:
        while True:
            await asyncio.sleep(LOST_CHECK)
            for session in list(self.sessions.values()):
                session.check_lost()

    # ================================================================== sessions and executors
    def _on_executors(self, names: list[str]) -> None:
        for name in names:
            if name not in self.sessions:
                self.spawn(self.add_session(name))
        self.executors_changed()

    async def add_session(self, name: str) -> Session:
        """The session of executor ``name``, made (with its remembered tree) if it is new."""
        if name in self.sessions:
            return self.sessions[name]
        link = self.factory.link(name)
        session = Session(self, name, link, self.catalogs.get(name) or builtin_catalog(ros=self.settings.link == "ros"))
        self.sessions[name] = session
        remembered = self.memory.get(name) or {}
        session.own_run_id = remembered.get("run_id") if isinstance(remembered.get("run_id"), str) else None
        if session.own_run_id is not None and remembered.get("ended") is True:
            session.finished.add(session.own_run_id)  # its result arrived before the restart
        if isinstance(remembered.get("root"), str):
            try:
                document, loaded = self.docs.load(remembered["root"], session.catalog)
                self.docs.add(self.docs.get(document.key) or document)
                session.root = document.key
                session.set_board(GatewayBoard(loaded.blackboard))
            except (PathError, TreeFileError, ValueError) as error:
                log.warning("%s: the remembered tree %r cannot be opened: %s", name, remembered["root"], error)
        link.subscribe(session)
        try:
            await link.start()
        except Exception:  # noqa: BLE001 - the session stays, offline
            log.exception("the link to %s cannot be started", name)
        self.executors_changed()
        return session

    def executor_list(self) -> list[dict]:
        return [{"name": name, "online": session.online,
                 "state": session.status.state if session.status is not None else 0}
                for name, session in self.sessions.items()]

    def welcome(self, client: Client) -> dict:
        return {"t": "welcome", "protocol": PROTOCOL, "client": client.id, "executors": self.executor_list(),
                "time": time.time(), "local": self.settings.link == "local"}

    def executors_changed(self) -> None:
        for client in self.clients.values():
            if client.greeted:
                client.push(self.welcome(client))

    def _notice_all_error(self, text: str) -> None:
        for client in self.clients.values():
            client.push({"t": "notice", "level": "error", "text": text})

    # ================================================================== memory and catalogs
    def _read_state(self, name: str, default: Any) -> Any:
        path = self.state_dir / name
        if not path.is_file():
            return default
        try:
            return read_json(path)
        except TreeFileError as error:
            log.warning("%s cannot be read: %s", path, error)
            return default

    def save_memory(self) -> None:
        """Remember which tree each session has open and the run it started (written soon)."""
        self.memory = {name: {"root": session.root, "run_id": session.own_run_id,
                              "ended": session.own_run_id in session.finished}
                       for name, session in self.sessions.items() if session.root or session.own_run_id}
        self.spawn(self._write_state("sessions.json", dict(self.memory)))

    def save_catalog(self, name: str, types: list[dict]) -> None:
        self.catalogs[name] = list(types)
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]
        self.spawn(self._write_state(f"catalogs/{digest}.json", {"name": name, "types": list(types)}))

    async def _write_state(self, name: str, data: Any) -> None:
        try:
            await asyncio.to_thread(write_json, self.state_dir / name, data)
            self._memory_failed = False
        except OSError as error:
            log.warning("%s cannot be written: %s", self.state_dir / name, error)
            if not self._memory_failed:
                self._memory_failed = True
                self._notice_all_error(f"The gateway's state file {name} cannot be written: {error}")

    # ================================================================== documents
    def run_owner(self, key: str) -> Session | None:
        """The session whose run (starting or going) includes the tree ``key``: its structure is
        locked (G-04, G-08)."""
        for session in self.sessions.values():
            if (session.starting or session.result_pending) and key in session.run_keys:
                return session
        return None

    def recipients(self, key: str) -> list[Client]:
        """The browsers that show the document ``key``."""
        return [client for client in self.clients.values()
                if client.session is not None and (client.session.root == key or key in client.shown)]

    def broadcast_op(self, key: str, op: dict, rev: int, by: str, cid: Any) -> None:
        """An applied operation, in order, to every browser that shows the document."""
        message = {"t": "op", "doc": key, "rev": rev, "op": op, "by": by, "cid": cid}
        for client in self.recipients(key):
            client.push(message)
        document = self.docs.get(key)
        if document is not None:
            self.docs.changed(document)

    def doc_event(self, key: str, event: str, document: TreeDocument | None, extra: list[Client] = ()) -> None:
        for client in {*self.recipients(key), *extra}:
            types = client.session.board.types() if client.session is not None else {}
            client.push({"t": "doc", "doc": key, "event": event,
                         "document": document.to_wire(types) if document is not None else None})

    def maybe_close(self, key: str | None) -> None:
        """Close a document no browser shows, that is no session's tree and has no unsaved
        changes (design 9.2)."""
        document = self.docs.get(key)
        if document is None or document.unsaved or self.run_owner(key) is not None:
            return
        if any(session.root == key for session in self.sessions.values()):
            return
        if any(key in client.shown for client in self.clients.values()):
            return
        self.docs.remove(key)
        for session in self.sessions.values():
            session.docs.discard(key)

    def rename_entry(self, origin: Session, old: str, new: str) -> None:
        """An entry was renamed: every open document follows (``rename_entry``), except the trees
        of other sessions, whose blackboards are their own."""
        others = {session.root for session in self.sessions.values() if session is not origin}
        for key in self.docs.keys():
            if key in others:
                continue
            document = self.docs.get(key)
            try:
                applied = document.rename_entry(old, new, origin.board.types())
            except OpRefused as error:
                log.warning("%s: renaming %r to %r: %s", key, old, new, error)
                continue
            for item in applied:
                self.broadcast_op(key, item.op, item.rev, "executor", None)

    def bundle_reader(self, session: Session, root: str) -> Callable[[str], TreeDocument | None]:
        """Documents for a bundle: open ones as they are in memory, others from disk."""
        return lambda key: self.docs.read(key, session.catalog)

    def read_blackboard_items(self, key: str) -> list[dict]:
        """The blackboard file of a tree run by a Remote Sub-Tree node (file form)."""
        try:
            items = read_blackboard_file(blackboard_file_path(self.trees_dir, key))
        except (PathError, TreeFileError):
            return []
        entries, _ = parse_blackboard(items)
        result = []
        for entry in entries:
            item = {"name": entry["name"], "type": entry["type"], "value": encode_value(entry["value"])}
            if entry["reset"]:
                item["reset"] = True
            if entry["source"] is not None:
                item["source"] = entry["source"]
            result.append(item)
        return result

    # ================================================================== browser connections
    def connect(self, client: Client) -> None:
        self.clients[client.id] = client
        client.start()

    async def disconnect(self, client: Client) -> None:
        self.clients.pop(client.id, None)
        await client.stop()
        session = client.session
        client.session = None
        for key in list(client.shown):
            client.shown.discard(key)
            self.maybe_close(key)
        if session is not None:
            self._presence(session)

    def _presence(self, session: Session) -> None:
        count = len(session.clients())
        for client in session.clients():
            client.push({"t": "presence", "attached": count})

    def attach(self, client: Client, name: Any) -> None:
        session = self.sessions.get(name) if isinstance(name, str) else None
        if session is None:
            client.push({"t": "notice", "level": "error", "text": f"There is no executor named {name!r}."})
            return
        previous = client.session
        old_shown = set(client.shown)
        client.session = session
        client.shown = {key for key in session.docs if self.docs.get(key) is not None}
        client.reset_streams()
        client.push(session.snapshot(client))
        for key in old_shown - client.shown:
            self.maybe_close(key)
        if previous is not None and previous is not session:
            self._presence(previous)
        self._presence(session)

    async def handle(self, client: Client, message: Any) -> None:
        """One message from a browser (contract 5.2)."""
        if not isinstance(message, dict) or not isinstance(message.get("t"), str):
            client.push({"t": "notice", "level": "warning", "text": "a message is an object with 't'"})
            return
        kind = message["t"]
        cid = message.get("cid")
        reply = Reply(client, cid)
        try:
            if kind == "hello":
                if message.get("protocol") != PROTOCOL:
                    client.push({"t": "reload"})
                    return
                client.greeted = True
                client.push(self.welcome(client))
            elif kind == "attach":
                self.attach(client, message.get("executor"))
            elif kind in ("op", "cmd", "file", "ask") and not allow(kind, client):
                reply.no("not allowed")
            elif kind in ("op", "cmd", "file") and client.session is None:
                reply.no("attach to an executor first")
            elif kind == "op":
                op = message.get("op")
                if not isinstance(op, dict):
                    reply.no("an operation is an object with 'op'")
                elif message.get("doc") == "@blackboard":
                    await client.session.bb_op(reply, op)
                else:
                    await client.session.doc_op(reply, client, str(message.get("doc")), message.get("rev"), op)
            elif kind == "cmd":
                name = message.get("name")
                coroutine = client.session.command(reply, client, name, message.get("confirm") is True)
                if name == "execute":
                    self.spawn(self._guarded(reply, coroutine))  # the browser may go on editing meanwhile
                else:
                    await coroutine
            elif kind == "file":
                await self.file_action(reply, client, message)
            elif kind == "ask":
                await self.ask(reply, client, message)
            elif kind == "watch":
                self.watch(client, message.get("executor"))
            elif kind == "unwatch":
                client.watching = None
            elif kind == "resync":
                key = message.get("doc")
                document = self.docs.get(key) if isinstance(key, str) else None
                types = client.session.board.types() if client.session is not None else {}
                client.push({"t": "doc", "doc": key, "event": "resync",
                             "document": document.to_wire(types) if document is not None else None})
            else:
                reply.no(f"unknown message {kind!r}") if cid is not None else None
        except Exception as error:  # noqa: BLE001 - every request gets its answer
            log.exception("handling %r from %s failed", kind, client.address)
            reply.no(f"internal error: {error}")
        finally:
            if cid is not None and kind in ("op", "file", "ask") and not reply.done and not reply.deferred:
                reply.no("the request was not answered")

    async def _guarded(self, reply: Reply, coroutine: Coroutine) -> None:
        try:
            await coroutine
        except Exception as error:  # noqa: BLE001
            log.exception("a command failed")
            reply.no(f"internal error: {error}")
        finally:
            if not reply.done:
                reply.no("the command was not answered")

    def watch(self, client: Client, name: Any) -> None:
        """Also receive another executor's run, read-only (contract 5.8 item 7)."""
        session = self.sessions.get(name) if isinstance(name, str) else None
        if session is None:
            client.push({"t": "notice", "level": "warning", "text": f"There is no executor named {name!r} to watch."})
            return
        client.watching = name
        message = session.run_message("started", with_bundle=True)
        if message is not None:
            client.push(message)
        client.mark_st(name)
        if session.details:
            client.mark_nd(name, [(index, kind) for index, kinds in session.details.items() for kind in kinds])

    # ================================================================== questions
    async def ask(self, reply: Reply, client: Client, message: dict) -> None:
        kind = message.get("kind")
        session = client.session
        if kind == "parse_literal":
            type_name = message.get("type")
            try:
                value = parse_literal(message.get("text", ""), type_name if isinstance(type_name, str) else None)
            except (ValueError, TypeError) as error:
                reply.no(str(error))
                return
            shown_type = type_name if isinstance(type_name, str) else infer_type(value)
            reply.ok(value=to_wire(shown_type, value), type=shown_type)
            return
        if kind == "executors":
            reply.ok(executors=self.executor_list())
            return
        if session is None:
            reply.no("attach to an executor first")
            return
        if kind == "entry_value":
            entry = session.board.entry(message.get("id")) if isinstance(message.get("id"), str) else None
            if entry is None:
                reply.no(f"no blackboard entry has the id {message.get('id')!r}")
                return
            reply.ok(value=value_to_wire(entry["type"], entry["value"]), type=entry["type"])
            return
        if kind == "log_before":
            await self._log_before(reply, session, message)
            return
        if kind in ("graph", "interface", "sample"):
            arg = {key: value for key, value in message.items() if key not in ("t", "cid", "kind")}
            try:
                result = await session.link.describe(kind, arg)
            except CommandRefused as error:
                reply.no(str(error))
                return
            except LinkDown as error:
                reply.no(f"the executor cannot be reached: {error}")
                return
            if kind == "graph":
                reply.ok(graph=result)
            elif kind == "interface":
                reply.ok(interface=result.get("interface", result) if isinstance(result, dict) else result)
            else:
                reply.ok(**(result if isinstance(result, dict) else {"value": result}))
            return
        reply.no(f"unknown question {kind!r}")

    async def _log_before(self, reply: Reply, session: Session, message: dict) -> None:
        before, count = message.get("before"), message.get("count", 100)
        if isinstance(before, bool) or not isinstance(before, int) or not isinstance(count, int) or count <= 0:
            reply.no("log_before needs the numbers 'before' and 'count'")
            return
        start = max(0, before - min(count, 10_000))
        kept = [(seq, line) for seq, line in session.log if start <= seq < before]
        if kept and kept[0][0] == start:
            reply.ok(first=start, lines=[line for _, line in kept])
            return
        run = session.run
        try:
            records = await session.link.get_log(run.run_id if run is not None else None, start, before - start)
        except LinkDown as error:
            reply.no(f"the executor cannot be reached: {error}")
            return
        records = [record for record in records if record.seq < before]
        reply.ok(first=records[0].seq if records else start, lines=[log_line(record) for record in records])

    # ================================================================== files
    def tree_key(self, name: Any) -> str:
        """The document key of a tree name from a browser (any of the four spellings), confined
        to the trees folder; names in hidden folders (``.recovery``) are refused.

        Raises:
            PathError: the name is refused (G-16).
        """
        if not isinstance(name, str):
            raise PathError("a tree name is text")
        key = document_key(name)
        if any(part.startswith(".") for part in key.split("/")):
            raise PathError(f"the name {name!r} is reserved")
        self.docs.path(key)  # confined: raises PathError for links leading out
        return key

    def root_of(self, key: str) -> Session | None:
        return next((session for session in self.sessions.values() if session.root == key), None)

    async def file_action(self, reply: Reply, client: Client, message: dict) -> None:
        action = message.get("action")
        handler = {
            "list": self._list, "new": self._new_or_load, "load": self._new_or_load, "save": self._save,
            "save_as": self._save_as, "rename": self._rename, "delete": self._delete, "zoom_in": self._zoom_in,
            "go_back": self._go_back, "revert": self._revert, "recover": self._recover,
        }.get(action)
        if handler is None:
            reply.no(f"unknown file action {action!r}")
            return
        try:
            await handler(reply, client, message)
        except PathError as error:
            reply.no(str(error))
        except TreeFileError as error:
            reply.no(str(error))
        except OSError as error:
            reply.no(f"the file cannot be written: {error.strerror or error}")

    async def _list(self, reply: Reply, client: Client, message: dict) -> None:
        trees, dirs = await asyncio.to_thread(self._scan_trees)
        for tree in trees:
            owner = self.root_of(tree["key"])
            tree["open_in"] = owner.name if owner is not None else None
        reply.ok(trees=trees, dirs=dirs)

    def _scan_trees(self) -> tuple[list[dict], list[str]]:
        trees: dict[str, dict] = {}
        dirs: list[str] = []
        for folder, names, files in os.walk(self.trees_dir):
            names[:] = sorted(name for name in names if not name.startswith("."))
            relative = Path(folder).relative_to(self.trees_dir).as_posix()
            if relative != ".":
                dirs.append(relative)
            for file_name in sorted(files):
                if file_name.endswith(BLACKBOARD_SUFFIX) or not file_name.lower().endswith(".json"):
                    continue
                path = Path(folder) / file_name
                if not file_name.endswith(TREE_SUFFIX):
                    try:
                        data = read_json(path)
                    except TreeFileError:
                        continue
                    if not isinstance(data, dict) or data.get("format") != TREE_FORMAT:
                        continue
                try:
                    key = document_key(path.relative_to(self.trees_dir).as_posix())
                    stamp = path.stat().st_mtime
                except (PathError, OSError):
                    continue
                if key not in trees or file_name.endswith(TREE_SUFFIX):
                    folder_parts = key.split("/")[:-1]
                    trees[key] = {"key": key, "dirs": folder_parts, "mtime": stamp, "open_in": None}
        return sorted(trees.values(), key=lambda tree: tree["key"]), dirs

    def _session_docs(self, session: Session) -> list[str]:
        return [key for key in dict.fromkeys([session.root, *sorted(session.docs)]) if key is not None]

    async def _resolve_unsaved(self, reply: Reply, session: Session, choice: Any) -> bool:
        """New and Load ask about every document of the session with unsaved changes; with the
        user's choice they save or discard them. False: answered (a question or an error)."""
        unsaved = [key for key in self._session_docs(session) if (doc := self.docs.get(key)) is not None and doc.unsaved]
        if not unsaved:
            return True
        if choice not in ("save", "discard"):
            reply.no("there are unsaved changes", question={"kind": "unsaved", "docs": unsaved})
            return False
        for key in unsaved:
            document = self.docs.get(key)
            if choice == "save":
                try:
                    await self._save_document(session, document, None)
                except (OSError, PathError) as error:
                    reply.no(f"{key} cannot be saved: {error}")
                    return False
                self.doc_event(key, "saved", document)
            else:
                shown_elsewhere = any(client.session is not session and key in client.shown
                                      for client in self.clients.values()) or (
                    self.root_of(key) not in (None, session))
                if shown_elsewhere:
                    try:
                        reloaded = self.docs.reload(key, session.catalog)
                        self.doc_event(key, "reverted", reloaded)
                    except (PathError, TreeFileError):
                        pass
                else:
                    document.mark_saved()
                await self.docs.drop_recovery(key)
        return True

    async def _new_or_load(self, reply: Reply, client: Client, message: dict) -> None:
        session = client.session
        action = message["action"]
        if session.busy():
            reply.no(f"{'New' if action == 'new' else 'Load'} is refused while the executor {session.name} is not idle")
            return
        key = self.tree_key(message.get("name"))
        owner = self.root_of(key)
        if owner is not None and owner is not session:
            reply.no(f"the tree {key} is open on the executor {owner.name}", executor=owner.name)
            return
        exists = find_tree_file(self.trees_dir, key) is not None
        if action == "new" and exists and message.get("replace") is not True:
            reply.no(f"the tree {key} exists", question={"kind": "replace", "name": key})
            return
        if action == "load" and not exists and self.docs.get(key) is None:
            reply.no(f"there is no tree named {key}")
            return
        if action == "new" and self.docs.get(key) is not None and key != session.root:
            reply.no(f"the tree {key} is open; close it before replacing it")
            return
        if action == "load":
            document, loaded = self.docs.load(key, session.catalog)  # checked completely before anything changes
            board = GatewayBoard(loaded.blackboard)
        else:
            document = TreeDocument(new_tree_data(uuid.uuid4().hex), session.catalog, key=key)
            board = GatewayBoard()
        if not await self._resolve_unsaved(reply, session, message.get("choice")):
            return
        current = self.docs.get(key)
        if current is not None:
            if action == "load" and current.unsaved:
                document = current  # open elsewhere with unsaved changes: they live on (design 9.2)
            else:
                document.rev = current.rev + 1
        if action == "new":
            path = self.docs.path(key)
            await asyncio.to_thread(_write_new_tree, path, document.to_file())
            await asyncio.to_thread(write_json, blackboard_file_path(self.trees_dir, key),
                                    {"format": "behavior_tree_widget_blackboard", "version": 1, "blackboard": []})
        await session.board_writer.flush()
        old_docs = self._session_docs(session)
        self.docs.add(document)
        session.root = key
        session.docs = set()
        session.set_board(board)
        session.findings = []
        if board.problems:
            session.notice("warning", "Blackboard entries were left out: " + "; ".join(board.problems))
        for other in session.clients():
            other.shown = set()
        for old in old_docs:
            self.maybe_close(old)
        self.save_memory()
        log.info("%s of %r on %s by %s", action.capitalize(), key, session.name, client.address)
        reply.ok(doc=key)
        for other in session.clients():
            other.reset_streams()
            other.push(session.snapshot(other))
        if current is not None and current is not document:  # browsers that show it elsewhere follow
            self.doc_event(key, "resync", document)

    async def _save_document(self, session: Session | None, document: TreeDocument, view: dict | None,
                             key: str | None = None) -> list[str]:
        """Write a document; the tree of a session writes its blackboard file too."""
        owner = self.root_of(document.key)
        if owner is not None:
            await owner.board_writer.flush()
            await owner._write_board()
        return await self.docs.save(document, view, key)

    async def _save(self, reply: Reply, client: Client, message: dict) -> None:
        document = self.docs.get(message.get("doc"))
        if document is None:
            reply.no(f"the document {message.get('doc')!r} is not open")
            return
        view = message.get("view") if isinstance(message.get("view"), dict) else None
        warnings = await self._save_document(client.session, document, view)
        log.info("Save of %r by %s", document.key, client.address)
        self.doc_event(document.key, "saved", document, [client])
        reply.ok(warnings=warnings)

    async def _save_as(self, reply: Reply, client: Client, message: dict) -> None:
        old = message.get("doc")
        document = self.docs.get(old)
        if document is None:
            reply.no(f"the document {old!r} is not open")
            return
        new = self.tree_key(message.get("name"))
        view = message.get("view") if isinstance(message.get("view"), dict) else None
        if new == old:
            await self._save(reply, client, {"doc": old, "view": view})
            return
        if self.run_owner(old) is not None:
            reply.no("the tree is executing: Save As is unavailable until the run ends")
            return
        if self.docs.get(new) is not None:
            owner = self.root_of(new)
            reply.no(f"the tree {new} is open{f' on the executor {owner.name}' if owner else ''}")
            return
        if find_tree_file(self.trees_dir, new) is not None and message.get("replace") is not True:
            reply.no(f"the tree {new} exists", question={"kind": "replace", "name": new})
            return
        owner = self.root_of(old)
        warnings = await self.docs.save(document, view, key=new)
        recipients = self.recipients(old)
        self.docs.rekey(old, new)
        document.mark_saved()
        for session in self.sessions.values():
            if old in session.docs:
                session.docs.discard(old)
                session.docs.add(new)
        for other in self.clients.values():
            if old in other.shown:
                other.shown.discard(old)
                other.shown.add(new)
        if owner is not None:
            owner.root = new
            await owner.board_writer.flush()
            await owner._write_board()
            self.save_memory()
        await self.docs.drop_recovery(old)
        log.info("Save As of %r to %r by %s", old, new, client.address)
        for other in {*recipients, client}:
            types = other.session.board.types() if other.session is not None else {}
            other.push({"t": "doc", "doc": old, "event": "renamed", "document": document.to_wire(types)})
        reply.ok(warnings=warnings, doc=new)

    async def _rename(self, reply: Reply, client: Client, message: dict) -> None:
        old = self.tree_key(message.get("name"))
        new = self.tree_key(message.get("new_name"))
        source = find_tree_file(self.trees_dir, old)
        if source is None:
            reply.no(f"there is no tree named {old}")
            return
        if new == old:
            reply.ok()
            return
        if find_tree_file(self.trees_dir, new) is not None or self.docs.get(new) is not None:
            reply.no(f"the tree {new} exists")
            return
        if self.run_owner(old) is not None:
            reply.no("the tree is executing: it cannot be renamed now")
            return
        target = self.docs.path(new) if source.name.endswith(TREE_SUFFIX) else self.docs.path(new).with_name(
            posixpath.basename(new) + ".json")
        old_board, new_board = blackboard_file_path(self.trees_dir, old), blackboard_file_path(self.trees_dir, new)
        owner = self.root_of(old)
        if owner is not None:
            await owner.board_writer.flush()
        await asyncio.to_thread(_move, source, target, old_board, new_board)
        document = self.docs.get(old)
        if document is not None:
            recipients = self.recipients(old)
            self.docs.rekey(old, new)
            if not target.name.endswith(TREE_SUFFIX):
                document.path = target.relative_to(self.trees_dir).as_posix()
            for session in self.sessions.values():
                if old in session.docs:
                    session.docs.discard(old)
                    session.docs.add(new)
                if session.root == old:
                    session.root = new
            for other in self.clients.values():
                if old in other.shown:
                    other.shown.discard(old)
                    other.shown.add(new)
            for other in recipients:
                types = other.session.board.types() if other.session is not None else {}
                other.push({"t": "doc", "doc": old, "event": "renamed", "document": document.to_wire(types)})
            await self.docs.drop_recovery(old)
            self.docs.changed(document)
            self.save_memory()
        log.info("Rename of %r to %r by %s", old, new, client.address)
        reply.ok(doc=new)

    async def _delete(self, reply: Reply, client: Client, message: dict) -> None:
        key = self.tree_key(message.get("name"))
        owner = self.root_of(key)
        if owner is not None:
            reply.no(f"the tree {key} is open on the executor {owner.name}")
            return
        if self.docs.get(key) is not None:
            reply.no(f"the tree {key} is open")
            return
        source = find_tree_file(self.trees_dir, key)
        if source is None:
            reply.no(f"there is no tree named {key}")
            return
        await asyncio.to_thread(_delete_files, source, blackboard_file_path(self.trees_dir, key))
        log.info("Delete of %r by %s", key, client.address)
        reply.ok()

    async def _zoom_in(self, reply: Reply, client: Client, message: dict) -> None:
        session = client.session
        holder = self.docs.get(message.get("doc"))
        node_id = message.get("node")
        if holder is None:
            reply.no(f"the document {message.get('doc')!r} is not open")
            return
        if not isinstance(node_id, str) or not holder.has_node(node_id) or holder.is_placeholder(node_id) \
                or holder.node_type(node_id) not in (SUB_TREE, REMOTE_SUB_TREE):
            reply.no("Zoom In needs a Sub-Tree or Remote Sub-Tree node")
            return
        current = holder.fields(node_id).get("File", "")
        chosen = message.get("file")
        if isinstance(chosen, str) and chosen.strip():
            key = self.tree_key(chosen)
            if find_tree_file(self.trees_dir, key) is None and self.docs.get(key) is None:
                reply.no(f"there is no tree named {key}",
                         question={"kind": "choose_file", "node": node_id, "file": chosen})
                return
            folder = posixpath.dirname(holder.key)
            relative = posixpath.relpath(tree_file_name(key), folder) if folder else tree_file_name(key)
            if relative != current:
                applied = holder.apply({"op": "node.set_field", "id": node_id, "field": "File", "value": relative})
                self.broadcast_op(holder.key, applied.op, applied.rev, client.id, None)
        else:
            try:
                key = subtree_key(holder.key, current)
            except PathError as error:
                reply.no(str(error), question={"kind": "choose_file", "node": node_id, "file": str(current)})
                return
        document = self.docs.get(key)
        if document is None:
            bundle = session.run_bundle if session.own_active() else None
            if bundle is not None and key in bundle.get("trees", {}):
                document = TreeDocument(bundle["trees"][key], session.catalog, key=key)  # what runs (design 9.5)
            else:
                try:
                    document = self.docs.load(key, session.catalog)[0]
                except TreeFileError as error:
                    reply.no(str(error), question={"kind": "choose_file", "node": node_id, "file": str(current)})
                    return
            self.docs.add(document)
        session.docs.add(key)
        client.shown.add(key)
        reply.ok(doc=key, document=document.to_wire(session.board.types()))

    async def _go_back(self, reply: Reply, client: Client, message: dict) -> None:
        key = message.get("doc")
        if isinstance(key, str):
            client.shown.discard(key)
            self.maybe_close(key)
        reply.ok()

    async def _revert(self, reply: Reply, client: Client, message: dict) -> None:
        key = message.get("doc")
        if self.docs.get(key) is None:
            reply.no(f"the document {key!r} is not open")
            return
        if self.run_owner(key) is not None:
            reply.no("the tree is executing: it cannot be reverted now")
            return
        document = self.docs.reload(key, client.session.catalog)
        await self.docs.drop_recovery(key)
        log.info("Revert of %r by %s", key, client.address)
        self.doc_event(key, "reverted", document, [client])
        reply.ok()

    async def _recover(self, reply: Reply, client: Client, message: dict) -> None:
        key = self.tree_key(message.get("doc"))
        if key not in self.docs.recovery_keys:
            reply.no(f"there is nothing to recover for {key}")
            return
        if message.get("accept") is not True:
            current = self.docs.get(key)
            if current is None or not current.unsaved:
                await self.docs.drop_recovery(key)
            reply.ok()
            return
        if self.run_owner(key) is not None:
            reply.no("the tree is executing: it cannot be recovered now")
            return
        session = client.session
        data = self.docs.read_recovery(key)
        path = data.pop("recovery_of", None)
        current = self.docs.get(key)
        document = TreeDocument(data, session.catalog, key=key, path=path if isinstance(path, str) else None,
                                rev=current.rev + 1 if current is not None else 0)
        document.unsaved = True
        self.docs.add(document)
        if session.root != key:
            session.docs.add(key)
            client.shown.add(key)
        self.docs.changed(document)
        self.doc_event(key, "resync", document, [client])
        reply.ok(doc=key, document=document.to_wire(session.board.types()))

    # ================================================================== Promote to Sub-Tree
    async def promote(self, session: Session, reply: Reply, client: Client, document: TreeDocument, op: dict) -> None:
        name = op.get("name")
        if not isinstance(name, str) or not name.strip():
            reply.no("the new tree needs a name")
            return
        folder = posixpath.dirname(document.key)
        new_key = name if "/" in name or not folder else f"{folder}/{name}"
        sizes = op.get("sizes")
        sizes = {key: (float(value[0]), float(value[1])) for key, value in sizes.items()
                 if isinstance(value, list) and len(value) == 2} if isinstance(sizes, dict) else None
        open_keys = {session.root, *client.shown} - {document.key, None}
        if self.docs.get(new_key) is not None:
            reply.no(f"the tree {new_key} is open; choose another name")
            return
        try:
            data, ops = promote(document, op.get("id"), new_key, self.trees_dir, open_keys=open_keys,
                                overwrite=op.get("replace") is True, running=self.run_owner(document.key) is not None,
                                sizes=sizes)
        except FileExistsError:
            reply.no(f"the tree {new_key} exists", question={"kind": "replace", "name": new_key})
            return
        except (OpRefused, PathError) as error:
            reply.no(getattr(error, "reason", str(error)))
            return
        await asyncio.to_thread(_write_new_tree, self.docs.path(new_key), data)
        try:
            applied = document.apply_all(ops, entry_types=session.board.types())
        except OpRefused as error:
            reply.no(error.reason)
            return
        for item in applied:
            self.broadcast_op(document.key, item.op, item.rev, client.id, reply.cid)
        reply.ok(rev=document.rev, doc=new_key)

    # ================================================================== HTTP helpers
    def download_files(self, key: str) -> list[tuple[str, Path]]:
        """The files of a tree to download: ``(name in the zip, path)``.

        Raises:
            PathError, TreeFileError: no such tree.
        """
        key = self.tree_key(key)
        source = find_tree_file(self.trees_dir, key)
        if source is None:
            raise TreeFileError(f"there is no tree named {key}")
        base = posixpath.basename(key)
        files = [(source.name if not source.name.endswith(TREE_SUFFIX) else base + TREE_SUFFIX, source)]
        board = blackboard_file_path(self.trees_dir, key)
        if board.is_file():
            files.append((base + BLACKBOARD_SUFFIX, board))
        return files

    def check_upload(self, folder: str, files: dict[str, bytes]) -> tuple[str, dict[str, bytes]]:
        """Check uploaded files as a tree before anything is stored: one tree file, optionally
        its blackboard file. Returns ``(key, {file name: content})``.

        Raises:
            ValueError: what is wrong, said for the user.
        """
        boards = {name: data for name, data in files.items() if name.upper().endswith("_BB.JSON")}
        trees = {name: data for name, data in files.items() if name not in boards}
        if len(trees) != 1 or len(boards) > 1:
            raise ValueError("upload one tree: its _BT.json file, and its _BB.json file if it has one")
        (tree_file, tree_bytes), = trees.items()
        names = {tree_name(name) for name in files}
        if len(names) != 1:
            raise ValueError("the two files belong to different trees")
        base = names.pop()
        relative = f"{folder.strip('/')}/{base}" if folder and folder.strip("/") else base
        key = self.tree_key(relative)
        try:
            data = loads_json(tree_bytes.decode("utf-8-sig"))
            check_tree_data(data, tree_file)
            TreeDocument(data, builtin_catalog(), key=key)
        except (UnicodeDecodeError, ValueError, TypeError) as error:
            raise ValueError(f"{tree_file} is not a tree file this server reads: {error}") from error
        stored = {key + TREE_SUFFIX: tree_bytes}
        if boards:
            (board_file, board_bytes), = boards.items()
            try:
                items = loads_json(board_bytes.decode("utf-8-sig"))
                if not isinstance(items, dict) or items.get("format") != "behavior_tree_widget_blackboard":
                    raise ValueError("not a blackboard file")
                _, problems = parse_blackboard(items.get("blackboard", []))
            except (UnicodeDecodeError, ValueError, TypeError) as error:
                raise ValueError(f"{board_file} is not a blackboard file: {error}") from error
            if problems:
                raise ValueError(f"{board_file}: {problems[0]}")
            stored[key + BLACKBOARD_SUFFIX] = board_bytes
        elif isinstance(data.get("blackboard"), list):
            _, problems = parse_blackboard(data["blackboard"])
            if problems:
                raise ValueError(f"{tree_file}: {problems[0]}")
        return key, stored

    async def store_upload(self, key: str, stored: dict[str, bytes], replace: bool) -> None:
        """Store checked upload files.

        Raises:
            FileExistsError: the tree exists and ``replace`` is False.
            PermissionError: the tree is open.
        """
        if self.docs.get(key) is not None or self.root_of(key) is not None:
            raise PermissionError(f"the tree {key} is open; close it before replacing it")
        if find_tree_file(self.trees_dir, key) is not None and not replace:
            raise FileExistsError(key)
        def write() -> None:
            for name, content in stored.items():
                path = resolve_in_root(self.trees_dir, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(path, content)

        await asyncio.to_thread(write)

    async def log_text(self, executor: str, run_id: str) -> str:
        """The log of a run as text (the file's lines), read through the link."""
        session = self.sessions.get(executor)
        if session is None:
            raise KeyError(executor)
        lines: list[str] = []
        start = 0
        while True:
            records = await session.link.get_log(run_id, start, 5000)
            if not records:
                break
            lines += [format_line(record.stamp, record.level or 4, record.text) for record in records]
            start = records[-1].seq + 1
        return "\n".join(lines) + ("\n" if lines else "")


def _write_new_tree(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, data)


def _move(source: Path, target: Path, old_board: Path, new_board: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(source, target)
    if old_board.is_file():
        shutil.move(old_board, new_board)


def _delete_files(*paths: Path) -> None:
    for path in paths:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

