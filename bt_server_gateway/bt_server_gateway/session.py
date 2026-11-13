"""A session: one executor's tree, blackboard and run (design 9.2, 9.3, 9.5, 7.2, 7.3, 4.5).

A session is the server-side counterpart of one widget. It is the :class:`RunListener` of its
executor's link and carries out the operations and commands of the browsers attached to it.

**Runs.** Execute puts the session into *starting* (design 9.5): the structure of the trees
that will run is locked and blackboard edits are held back; the bundle is built at once from
the documents as they are in memory (and sub-trees not open, from disk), validated by the
executor and, for each Remote Sub-Tree node, by the executor that will run it; with findings
Execute is answered ``no`` with them unless the browser confirmed (G-23). The blackboard sent is
kept for Reset after the run. Once the goal is accepted the held edits go to the executor
(K-06); if it is rejected they are applied here.

**The blackboard during a run** is the executor's: the gateway mirrors its updates, writes the
file (at most every 250 ms) and forwards browser edits through ``apply``, applying them itself
when the answer is ``NO_RUN`` (K-05), after the result if that is still on its way. The result's
values become the gateway's copy; later updates of that run are ignored. ``bb.set_source`` waits
for the end of the run.

**Fields during a run** are the executor's too: a ``node.set_field`` or ``node.set_selection``
is sent through ``apply`` to every session whose run holds the tree, and the document changes
only when the value comes back as a ``field`` detail (G-05). Bindings, outputs and cancel
options are read when a leaf starts: they are refused while the tree starts or runs.

**Runs the gateway did not start** are shown read-only from ``RunInfo``; the session's own
tree and blackboard are not touched, and Stop works by run id (4.5).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from bt_server_core.blackboard import BlackboardSnapshot, OpError, new_entry_id
from bt_server_core.bundle import REMOTE_SUB_TREE, build_bundle, sub_bundle
from bt_server_core.document import STRUCTURAL_OPS, OpRefused
from bt_server_core.files import blackboard_file_path
from bt_server_core.values import canonical_type, decode_value, equal_values, from_wire

from .board import FileWriter, GatewayBoard, board_file_data
from .documents import write_json
from .link import (
    APPLIED,
    NO_RUN,
    CommandRefused,
    ExecutorLink,
    GoalRejected,
    LinkDown,
    LinkRecord,
    LinkResult,
    LinkRun,
    LinkStatus,
)
from .wire import log_line, st_body

if TYPE_CHECKING:
    from .client import Client
    from .gateway import Gateway

__all__ = ["LOG_KEEP", "SNAPSHOT_LOG", "Session", "run_nodes"]

log = logging.getLogger("bt_server_gateway")

#: Log lines the gateway keeps per session, and the newest sent in a snapshot (design 9.6).
LOG_KEEP = 10_000
SNAPSHOT_LOG = 500
#: Seconds after an own run's state went IDLE without a result: the mirrored blackboard is then
#: taken as final (a run recognised after a gateway restart has no goal handle).
RESULT_GRACE = 3.0
#: Seconds without a status during a run after which the executor counts as lost (design 16).
LOST_AFTER = 2.0
#: Operations that go to the executor during a run (design 9.3: the executor holds the fields).
FORWARDED_OPS = frozenset(("node.set_field", "node.set_selection"))
#: Operations refused during a run: a leaf reads them when it starts.
BETWEEN_RUNS_OPS = frozenset(("node.set_binding", "node.set_output", "node.set_option"))


def run_nodes(bundle: dict) -> set[tuple[str, str]]:
    """``(tree key, node id)`` of every node a bundle runs on its own executor: those reachable
    from the root tree's Root, through Sub-Tree nodes (the trees of Remote Sub-Tree nodes run
    elsewhere)."""
    trees = bundle.get("trees") or {}
    result: set[tuple[str, str]] = set()
    stack, seen = [bundle.get("root")], set()
    while stack:
        key = stack.pop()
        if key in seen or key not in trees:
            continue
        seen.add(key)
        tree = trees[key]
        nodes = {item.get("id"): item for item in tree.get("nodes", ()) if isinstance(item, dict)}
        children: dict[str, list[str]] = {}
        for link in tree.get("connections", ()):
            if isinstance(link, dict):
                children.setdefault(link.get("parent"), []).append(link.get("child"))
        walk = [node_id for node_id, item in nodes.items() if item.get("type") == "Root"]
        while walk:
            node_id = walk.pop()
            if node_id not in nodes or (key, node_id) in result:
                continue
            result.add((key, node_id))
            item = nodes[node_id]
            if item.get("type") == "Sub-Tree" and isinstance(item.get("tree"), str):
                stack.append(item["tree"])
            walk.extend(children.get(node_id, ()))
    return result


class Reply:
    """Where the answer to one request goes: exactly one ``ok`` or ``no`` per ``cid``."""

    def __init__(self, client: Client, cid: Any) -> None:
        self.client, self.cid, self.done = client, cid, False
        self.deferred = False  # held back: answered later

    def ok(self, **extra: Any) -> None:
        if not self.done:
            self.done = True
            self.client.push({"t": "ok", "cid": self.cid, **extra})

    def no(self, reason: str, **extra: Any) -> None:
        if not self.done:
            self.done = True
            self.client.push({"t": "no", "cid": self.cid, "reason": reason, **extra})


class Session:
    """One executor's session (see the module docs)."""

    def __init__(self, gateway: Gateway, name: str, link: ExecutorLink, catalog: list[dict]) -> None:
        self.gw = gateway
        self.name = name
        self.link = link
        self.online = False
        self.catalog = catalog
        self.root: str | None = None
        self.docs: set[str] = set()  # sub-tree documents opened in this session
        self.board = GatewayBoard()
        self.board_writer = FileWriter(self._write_board, self._board_written)
        self._board_failed = False
        self.restore_copy: BlackboardSnapshot | None = None
        self.findings: list[dict] = []
        # the run
        self.run: LinkRun | None = None
        self.status: LinkStatus | None = None
        self.own_run_id: str | None = None
        self.result_pending = False
        self.run_bundle: dict | None = None
        self.run_keys: set[str] = set()
        self.run_nodes: set[tuple[str, str]] = set()  # (tree, node id) of the leaves the own run executes
        self._bundle_keys: dict[str, str] = {}  # document key -> the key the run's bundle uses
        self._doc_keys: dict[str, str] = {}
        self.finished: set[str] = set()
        self.starting = False
        self.file_busy = 0  # file commands under way that would replace or rename this session's tree
        self._awaiting_goal = False
        self._run_deferred = False
        self._early: list[tuple[Callable[..., None], tuple]] = []
        self._going: str | None = None  # a run not started here that was seen going
        self._ended: set[str] = set()
        self._held: list[tuple[Reply, str, dict, int | None]] = []
        self._after_result: list[tuple[Reply | None, dict]] = []
        self._pending_sources: list[dict] = []
        self._pending_fields: dict[tuple[str, str, str, str], int] = {}
        self._result_timer: asyncio.TimerHandle | None = None
        self.details: dict[int, dict[str, Any]] = {}
        self.log: deque[tuple[int, list]] = deque(maxlen=LOG_KEEP)
        self._log_next = 0
        self._log_buffer: list[LinkRecord] | None = None
        self._bb_seq = 0
        self._last_status = time.monotonic()
        self.lost = False

    # ================================================================== who sees what
    def clients(self) -> list[Client]:
        return [client for client in self.gw.clients.values() if client.session is self]

    def watchers(self) -> list[Client]:
        return [client for client in self.gw.clients.values() if client.watching == self.name]

    def notice(self, level: str, text: str) -> None:
        for client in self.clients():
            client.push({"t": "notice", "level": level, "text": text})

    def busy(self) -> bool:
        """The executor is not idle, or this session is starting or awaiting a result."""
        return self.starting or self.result_pending or (self.status is not None and self.status.state != 0)

    def own_active(self) -> bool:
        """A run this session started is going (its goal was accepted; no result yet)."""
        return self.result_pending and self.own_run_id is not None

    def run_message(self, event: str, with_bundle: bool | None = None) -> dict | None:
        run = self.run
        if run is None or run.run_id is None:
            return None
        own = run.run_id == self.own_run_id
        include = (not own) if with_bundle is None else with_bundle
        message = {"t": "run", "exec": self.name, "event": event, "run_id": run.run_id, "label": run.label,
                   "requested_by": run.requested_by, "node_paths": list(run.node_paths), "own": own,
                   "bundle": run.bundle if include else None}
        if event == "ended" and self.status is not None:
            message["outcome"] = self.status.outcome
            message["message"] = self.status.message
        return message

    def snapshot(self, client: Client) -> dict:
        """The whole session for one browser (contract 5.6)."""
        docs = {}
        types = self.board.types()
        for key in [self.root, *sorted(client.shown)]:
            document = self.gw.docs.get(key)
            if document is not None:
                docs[key] = document.to_wire(types)
        run = self.run_message("started")
        if run is not None:
            del run["t"]
        lines = list(self.log)[-SNAPSHOT_LOG:]
        st = st_body(self.name, self.status, self.starting)
        del st["t"]
        return {
            "t": "snapshot", "exec": self.name, "client": client.id, "root": self.root, "docs": docs,
            "blackboard": self.board.wire_entries(), "catalog": self.catalog, "online": self.online,
            "starting": self.starting, "run": run, "st": st,
            "details": {str(index): dict(kinds) for index, kinds in self.details.items() if kinds},
            "log": {"first": lines[0][0] if lines else self._log_next, "lines": [line for _, line in lines]},
            "recovery": sorted(key for key in self.gw.docs.offered if key == self.root or key in self.docs
                               or key in client.shown or self.gw.docs.get(key) is None),
            "attached": len(self.clients()), "findings": list(self.findings),
        }

    def mark_st(self) -> None:
        for client in self.clients() + self.watchers():
            client.mark_st(self.name)

    # ================================================================== the blackboard
    def set_board(self, board: GatewayBoard) -> None:
        self.board = board
        self.restore_copy = None

    async def _write_board(self) -> None:
        key = self.root
        if key is None:
            return
        path = blackboard_file_path(self.gw.trees_dir, key)
        data = board_file_data(self.board)
        await asyncio.to_thread(write_json, path, data)
        document = self.gw.docs.get(key)
        if document is not None and document.legacy_blackboard is not None and self.root == key:
            document.legacy_blackboard = None

    def _board_written(self, error: Exception | None) -> None:
        if error is None:
            self._board_failed = False
            return
        log.warning("%s: the blackboard file cannot be written: %s", self.name, error)
        if not self._board_failed:
            self._board_failed = True
            self.notice("error", f"The blackboard file cannot be written: {error}. The next change tries again.")

    def board_changed(self, extra: dict | None = None) -> None:
        """Tell the browsers what changed in the blackboard, let documents follow renames, and
        write the file soon (it is never an unsaved change)."""
        changes, renames = self.board.drain()
        changed: list[str] = []
        removed: list[str] = []
        order = False
        for part in (changes, extra):
            if part:
                changed += [entry["id"] if isinstance(entry, dict) else entry for entry in part.get("entries", ())]
                removed += list(part.get("removed", ()))
                order = order or part.get("order") is not None
        if changed or removed or order:
            for client in self.clients():
                client.mark_bb(changed, removed, order)
        for old, new in renames:
            self.gw.rename_entry(self, old, new)
        if changes or extra:
            self.board_writer.request()

    def _bb_from_wire(self, op: dict) -> dict:
        """A ``bb.*`` op with its wire values decoded (contract 3).

        Raises:
            OpError, ValueError: malformed, or a value that does not fit.
        """
        name = op.get("op")
        result = {key: value for key, value in op.items() if key != "cid"}
        if name == "bb.add":
            entry = op.get("entry")
            if not isinstance(entry, dict):
                raise OpError("bb.add needs an entry")
            type_name = canonical_type(entry.get("type"))
            value = entry.get("value")
            result["entry"] = {**entry, "type": type_name, "id": entry.get("id") or new_entry_id(),
                               "value": from_wire(type_name, value) if value is not None else None}
        elif name == "bb.set_value":
            entry = self.board.entry(op.get("id")) if isinstance(op.get("id"), str) else None
            if entry is None:
                raise OpError(f"no blackboard entry has the id {op.get('id')!r}")
            if entry["source"] is not None:
                raise OpError(f"the entry {entry['name']!r} is fed from the topic {entry['source'].get('topic')}; "
                              "only that topic sets its value")
            result["value"] = from_wire(entry["type"], op.get("value"))
        return result

    def _apply_bb_here(self, op: dict) -> None:
        """Apply a decoded ``bb.*`` op to the gateway's copy (raises OpError)."""
        self.board.apply_op(op)
        if not self.result_pending:
            self.restore_copy = None  # editing while idle discards the snapshot, as in the widget
        self.board_changed()

    async def bb_op(self, reply: Reply, op: dict) -> None:
        """A blackboard operation from a browser (contract 5.3)."""
        if self.starting:
            reply.deferred = True
            self._held.append((reply, "@blackboard", op, None))
            return
        try:
            decoded = self._bb_from_wire(op)
        except (OpError, ValueError, KeyError) as error:
            reply.no(str(error))
            return
        if not self.result_pending:
            try:
                self._apply_bb_here(decoded)
            except (OpError, ValueError) as error:
                reply.no(str(error))
                return
            reply.ok()
            return
        if decoded.get("op") == "bb.set_source":
            self._pending_sources.append(decoded)
            reply.ok(pending=True)
            return
        if decoded.get("op") == "bb.add" and decoded["entry"].get("source") is not None:
            self.board.declare(decoded["entry"]["id"], decoded["entry"].get("value"))  # what the file keeps
        try:
            code, message = await self.link.apply([decoded])
        except LinkDown as error:
            reply.no(f"the executor cannot be reached: {error}")
            return
        if code == APPLIED:
            reply.ok()
        elif code == NO_RUN:
            if self.result_pending:
                reply.deferred = True
                self._after_result.append((reply, decoded))  # applied to the result's values
            else:
                self._apply_late(reply, decoded)
        else:
            reply.no(message or "the executor refused the change")

    def _apply_late(self, reply: Reply | None, op: dict) -> None:
        try:
            self._apply_bb_here(op)
        except (OpError, ValueError) as error:
            if reply is not None:
                reply.no(str(error))
            else:
                self.notice("warning", f"A blackboard change could not be applied after the run: {error}")
            return
        if reply is not None:
            reply.ok()

    # ================================================================== document operations
    async def doc_op(self, reply: Reply, client: Client, key: str, rev: Any, op: dict) -> None:
        """An edit operation on a document (contract 5.3)."""
        document = self.gw.docs.get(key)
        if document is None:
            reply.no(f"the document {key!r} is not open")
            return
        name = op.get("op")
        owners = self.gw.run_owners(key)
        if owners:
            if name in STRUCTURAL_OPS:
                reply.no("the tree is executing: nodes cannot be added, removed or connected, nor composites changed")
                return
            if name in BETWEEN_RUNS_OPS:
                reply.no("the tree is executing: bindings, outputs and cancel options are read when a leaf starts; "
                         "change them between runs")
                return
            starting = next((owner for owner in owners if owner.starting), None)
            if starting is not None and (name in FORWARDED_OPS or (name == "doc.set_config" and key == starting.root)):
                reply.deferred = True  # sent once the goal is answered (design 9.5)
                starting._held.append((reply, key, op, rev))
                return
            if name in FORWARDED_OPS:
                running = [owner for owner in owners if owner.own_active() and owner.in_run(key, op.get("id"))]
                if running:
                    await self.gw.forward_edit(reply, document, op, running, self.board.types())
                    return
        if name == "group.promote":
            await self.gw.promote(self, reply, client, document, op)
            return
        try:
            applied = document.apply(op, running=bool(owners), entry_types=self.board.types(),
                                     rev=rev if isinstance(rev, int) else None)
        except OpRefused as error:
            reply.no(error.reason)
            return
        self.gw.broadcast_op(key, applied.op, applied.rev, client.id, reply.cid)
        extra = {"reorder": applied.reorder} if applied.reorder else {}
        reply.ok(rev=applied.rev, **extra)
        if name == "doc.set_config":
            for owner in owners:
                if owner.own_active() and key == owner.root:
                    try:
                        await owner.link.apply([{"op": "doc.set_config", "config": applied.op.get("config", {})}])
                    except LinkDown:
                        pass

    def _set_run_trees(self, bundle: dict) -> None:
        """The trees and leaves of the own run, by document key (as spelt on disk)."""
        self._bundle_keys = {self.gw.canonical(key): key for key in bundle.get("trees", {})}
        self._doc_keys = {key: canonical for canonical, key in self._bundle_keys.items()}
        self.run_keys = set(self._bundle_keys)
        self.run_nodes = {(self._doc_keys.get(key, key), node_id) for key, node_id in run_nodes(bundle)}

    def in_run(self, key: str, node_id: Any) -> bool:
        """Whether the own run executes the leaf ``node_id`` of the tree ``key``."""
        return isinstance(node_id, str) and (key, node_id) in self.run_nodes

    async def send_edit(self, op: dict) -> tuple[int, str]:
        """Send a field or choice edit to the executor; the document follows when it comes back."""
        target = (op["tree"], op["id"], op["field"], "selection" if op["op"] == "node.set_selection" else "value")
        self._pending_fields[target] = self._pending_fields.get(target, 0) + 1
        try:
            code, message = await self.link.apply([dict(op, tree=self._bundle_keys.get(op["tree"], op["tree"]))])
        except LinkDown as error:
            code, message = -1, f"the executor cannot be reached: {error}"
        if code != APPLIED:
            self._release_field(target)
        return code, message

    def _release_field(self, target: tuple[str, str, str, str]) -> bool:
        count = self._pending_fields.get(target, 0)
        if count <= 0:
            return False
        if count == 1:
            del self._pending_fields[target]
        else:
            self._pending_fields[target] = count - 1
        return True

    def _detail_back(self, kind: str, value: Any) -> None:
        """A field or title set during the run, reported on the details: the document follows.
        A browser's edit is an unsaved change; a value written by leaf code is not, and inside a
        sub-tree it changes that run only (design 9.3)."""
        if not isinstance(value, dict):
            return
        tree, node_id = value.get("tree"), value.get("id")
        tree = self._doc_keys.get(tree, tree) if isinstance(tree, str) else tree
        document = self.gw.docs.get(tree) if isinstance(tree, str) else None
        if document is None or not isinstance(node_id, str) or not document.has_node(node_id):
            return
        types = self.board.types()
        if kind == "title":
            if tree != self.root or not isinstance(value.get("title"), str):
                return
            unsaved = document.unsaved
            try:
                applied = document.apply({"op": "node.set_title", "id": node_id, "title": value["title"]})
            except OpRefused:
                return
            document.unsaved = unsaved
            self.gw.broadcast_op(tree, applied.op, applied.rev, "executor", None)
            return
        field = value.get("field")
        if not isinstance(field, str):
            return
        selection = "selection" in value
        by_browser = self._release_field((tree, node_id, field, "selection" if selection else "value"))
        if not by_browser and tree != self.root:
            return
        unsaved = document.unsaved
        try:
            if selection:
                if document.file_item(node_id).get("field_selections", {}).get(field) == value["selection"]:
                    return
                applied = document.apply({"op": "node.set_selection", "id": node_id, "field": field,
                                          "index": value["selection"]}, entry_types=types)
            else:
                if equal_values(document.fields(node_id).get(field), decode_value(value.get("value"))):
                    return
                applied = document.set_field_file(node_id, field, value.get("value"), types)
        except (OpRefused, ValueError):
            return
        document.unsaved = unsaved or by_browser
        self.gw.broadcast_op(tree, applied.op, applied.rev, "executor", None)
        self.gw.docs.changed(document)

    # ================================================================== commands
    async def command(self, reply: Reply, client: Client, name: str, confirm: bool) -> None:
        """``cmd``: execute, pause, resume, stop, reset."""
        try:
            if name == "execute":
                await self.execute(reply, client, confirm)
            elif name in ("pause", "resume"):
                if not self.own_active():
                    reply.no("no run of this tree is going" if self.run is None or self.status is None
                             or self.status.state == 0 else "this run was not started here: only Stop is offered")
                    return
                await self.link.pause(name == "pause")
                reply.ok()
            elif name == "stop":
                await self.stop(reply, client)
            elif name == "reset":
                await self.reset(reply, client)
            else:
                reply.no(f"unknown command {name!r}")
        except CommandRefused as error:
            reply.no(str(error))
        except LinkDown as error:
            reply.no(f"the executor cannot be reached: {error}")

    async def stop(self, reply: Reply, client: Client) -> None:
        run = self.run
        if run is None or run.run_id is None or self.status is None or self.status.state == 0:
            reply.no("no run is going")
            return
        log.info("Stop of run %s (%s) on %s by %s", run.run_id, run.label, self.name, client.address)
        await self.link.stop(run.run_id)
        reply.ok()

    async def reset(self, reply: Reply, client: Client) -> None:
        if self.busy() and not self.own_active():
            reply.no("this run was not started here: only Stop is offered" if not self.starting
                     else "Execute is being started")
            return
        log.info("Reset on %s by %s", self.name, client.address)
        if self.own_active():
            await self.link.reset()
            reply.ok()
            return
        if self.online:
            await self.link.reset()
        root = self.gw.docs.get(self.root)
        if root is not None and root.config.get("restore_blackboard") and self.restore_copy is not None:
            self.board.restore(self.restore_copy)
            self.board_changed()
        reply.ok()

    async def execute(self, reply: Reply, client: Client, confirm: bool) -> None:
        """Execute (design 9.5): starting, bundle, validation, the goal."""
        if not self.online or not self.link.online:
            reply.no(f"the executor {self.name} cannot be reached: Execute is unavailable")
            return
        if self.starting:
            reply.no("Execute is already being started")
            return
        if self.busy():
            who = self.run.requested_by if self.run is not None and self.run.requested_by else "someone"
            reply.no(f"the executor is busy: a run started by {who} is going")
            return
        if self.file_busy:
            reply.no("a file command on this tree is being carried out; try again")
            return
        root = self.gw.docs.get(self.root)
        if root is None:
            reply.no("no tree is open")
            return
        self.starting = True
        self.mark_st()
        accepted = False
        try:
            try:
                bundle = build_bundle(root.key, self.gw.bundle_reader(self, root.key), self.gw.read_blackboard_items)
            except KeyError as error:
                reply.no(str(error.args[0]) if error.args else "the tree cannot be read")
                return
            changing = self.gw.changing_keys() & set(bundle["trees"])
            if changing:
                reply.no(f"a file command on {', '.join(sorted(changing))} is being carried out; try again")
                return
            self._set_run_trees(bundle)
            entries = self.board.to_list()
            findings = await self.validate(bundle, entries)
            self.findings = findings
            if findings and not confirm:
                reply.no(f"the check found {len(findings)} problem{'s' if len(findings) != 1 else ''}",
                         findings=findings)
                return
            kept = self.board.snapshot()
            log.info("Execute of %r on %s by %s", root.key, self.name, client.address)
            self._awaiting_goal = True
            try:
                run_id = await self.link.execute(bundle, entries, root.key, client.address)
            except GoalRejected as error:
                reply.no(str(error) or "the executor rejected the run")
                return
            accepted = True
            self.own_run_id = run_id
            self.result_pending = True
            self.run_bundle = bundle
            self.restore_copy = kept
            self.board.fresh.clear()
            self.board.issues.clear()
            self._bb_seq = 0
            self.gw.save_memory()
            reply.ok(run_id=run_id)
        except LinkDown as error:
            reply.no(f"the executor cannot be reached: {error}")
        finally:
            self.starting = False
            self._awaiting_goal = False
            if not accepted:
                self.run_keys = set()
                self.run_nodes = set()
            if self._run_deferred:
                self._run_deferred = False
                self._announce_run()
            early, self._early = self._early, []
            for handler, args in early:  # updates of the run that came before the goal's answer
                handler(*args)
            self.mark_st()
            await self._release_held()

    async def validate(self, bundle: dict, entries: list[dict]) -> list[dict]:
        """Findings of this executor, merged with those of the executors that run the trees of
        Remote Sub-Tree nodes (design 8.3)."""
        findings = list(await self.link.validate(bundle, entries))
        seen: set[tuple[str, str]] = set()
        for key, tree in bundle["trees"].items():
            for item in tree.get("nodes", ()):
                if item.get("type") != REMOTE_SUB_TREE or not isinstance(item.get("tree"), str):
                    continue
                fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
                executor = str(fields.get("Executor", "")).strip()
                target = (executor, item["tree"])
                if not executor or target in seen:
                    continue
                seen.add(target)
                other = self.gw.sessions.get(executor)
                where = f"tree:{key}/{item.get('id')}"
                if other is None:
                    findings.append({"path": where, "severity": "warning",
                                     "text": f"the executor {executor} is not known here; its tree cannot be checked"})
                    continue
                try:
                    found = await other.link.validate(sub_bundle(bundle, item["tree"]),
                                                      bundle.get("blackboards", {}).get(item["tree"], []))
                except LinkDown:
                    findings.append({"path": where, "severity": "warning",
                                     "text": f"the executor {executor} cannot be reached to check {item['tree']!r}"})
                    continue
                findings += [{**finding, "executor": executor} for finding in found]
        return findings

    async def _release_held(self) -> None:
        """Edits held back while starting: to the executor if the goal was accepted, else here."""
        held, self._held = self._held, []
        for reply, key, op, rev in held:
            if key == "@blackboard":
                await self.bb_op(reply, op)
            else:
                await self.doc_op(reply, reply.client, key, rev, op)

    # ================================================================== the link's reports
    def on_online(self, online: bool) -> None:
        self.online = online
        if online:
            self.gw.spawn(self.sync())
        self.gw.executors_changed()
        for client in self.clients():
            client.mark_st(self.name)

    async def sync(self) -> None:
        """Take the executor's state (after it came back, or the gateway restarted)."""
        try:
            snap = await self.link.snapshot()
        except LinkDown:
            return
        if isinstance(snap.get("run"), LinkRun):
            self.on_run(snap["run"])
        if isinstance(snap.get("status"), LinkStatus):
            self.on_status(snap["status"])
        run = self.run
        if run is None or run.run_id is None:
            return
        if self.own_active() and snap.get("blackboard") is not None:
            self.board_changed(self.board.replace(snap["blackboard"]))
            self._bb_seq = int(snap.get("blackboard_seq", 0))
        self.details = {}
        for item in snap.get("details") or ():
            if isinstance(item, dict) and isinstance(item.get("i"), int) and item.get("value") is not None:
                self.details.setdefault(item["i"], {})[item["kind"]] = item["value"]
        if self.log or snap.get("log_seq", -1) < 0:
            return
        last = int(snap.get("log_seq", -1))
        try:
            records = await self.link.get_log(run.run_id, max(0, last + 1 - SNAPSHOT_LOG * 4), SNAPSHOT_LOG * 4)
        except LinkDown:
            return
        self._log_next = records[0].seq if records else last + 1
        self._append_log(records)

    def on_catalog(self, types: list[dict]) -> None:
        if types and types != self.catalog:
            self.catalog = list(types)
            self.gw.save_catalog(self.name, self.catalog)
            for client in self.clients():
                client.push({"t": "catalog", "exec": self.name, "types": self.catalog})

    def on_run(self, run: LinkRun) -> None:
        if run.run_id is None:
            self._end_foreign()
            if self.result_pending:
                self.notice("error", f"The executor {self.name} restarted: the run ended without a result.")
                self._end_own_run(None)
            self.run = None
            self.details = {}
            self.mark_st()
            return
        if self.run is not None and self.run.run_id == run.run_id:
            self.run = run
            return
        self._end_foreign()
        self.run = run
        self.details = {}
        self.log.clear()
        self._log_next = 0
        self._log_buffer = None
        if self._awaiting_goal:
            self._run_deferred = True  # whose run it is is known once the goal is answered
            return
        self._announce_run()

    def _announce_run(self) -> None:
        run = self.run
        if run is None or run.run_id is None:
            return
        own = run.run_id == self.own_run_id
        if own and run.run_id not in self.finished and not self.result_pending:
            self.result_pending = True  # recognised after a gateway restart
            self.run_bundle = run.bundle
        if own and self.run_bundle is not None:
            self._set_run_trees(self.run_bundle)
        for client in self.clients():
            client.clear_log()
            client.push(self.run_message("started"))
        for client in self.watchers():
            client.push(self.run_message("started", with_bundle=True))
        self.mark_st()

    def on_status(self, status: LinkStatus) -> None:
        self.status = status
        self._last_status = time.monotonic()
        if self.lost:
            self.lost = False
            self.notice("info", f"The executor {self.name} reports again.")
        self.mark_st()
        run = self.run
        if run is not None and run.run_id == status.run_id and run.run_id != self.own_run_id:
            if status.state != 0:
                self._going = run.run_id
            elif self._going == run.run_id:
                self._end_foreign()
        if self.result_pending and status.run_id == self.own_run_id:
            if status.state == 0:
                if self._result_timer is None:
                    loop = asyncio.get_running_loop()
                    self._result_timer = loop.call_later(RESULT_GRACE, self._no_result)
            elif self._result_timer is not None:
                self._result_timer.cancel()
                self._result_timer = None

    def _end_foreign(self) -> None:
        """A run not started here that was seen going has ended (or was replaced): ``run ended``."""
        run = self.run
        if run is None or self._going != run.run_id or run.run_id in self._ended:
            return
        self._going = None
        self._ended.add(run.run_id)
        message = self.run_message("ended", with_bundle=True)
        if message is None:
            return
        for client in self.clients() + self.watchers():
            client.push(message)

    def check_lost(self) -> None:
        """Called every half second: "executor lost" when the status stops during a run."""
        status = self.status
        if self.lost or status is None or status.state == 0:
            return
        if time.monotonic() - self._last_status > LOST_AFTER:
            self.lost = True
            self.notice("error", f"Executor lost: {self.name} has sent no status for {LOST_AFTER:.0f} s.")

    def _no_result(self) -> None:
        """The run went IDLE and no result came: take the mirrored blackboard as final."""
        self._result_timer = None
        if self.result_pending and self.status is not None and self.status.state == 0:
            self._end_own_run(None)

    def on_blackboard(self, run_id: str, seq: int, full: bool, update: dict) -> None:
        if self._awaiting_goal:
            self._early.append((self.on_blackboard, (run_id, seq, full, update)))
            return
        if run_id in self.finished or run_id != self.own_run_id or not self.result_pending:
            return
        if full:
            extra = self.board.replace(update.get("entries") or [])
            for item in update.get("entries") or ():
                if isinstance(item, dict) and "id" in item:
                    self.board.note_reported(item)
            self.board_changed(extra)
        else:
            if self._bb_seq and seq > self._bb_seq + 1:
                self.gw.spawn(self.sync())  # a gap: take a snapshot
            self.board.apply_update(update)
            self.board_changed()
        self._bb_seq = max(self._bb_seq, seq)

    def on_details(self, run_id: str, seq: int, details: list[dict]) -> None:
        if self._awaiting_goal:
            self._early.append((self.on_details, (run_id, seq, details)))
            return
        run = self.run
        own = run_id == self.own_run_id and self.result_pending
        current = run is not None and run.run_id == run_id
        if run_id in self.finished or not (current or own):
            return
        keys = []
        for item in details:
            index, kind = item.get("i"), item.get("kind")
            if not isinstance(index, int) or not isinstance(kind, str):
                continue
            value = item.get("value")
            if own and kind in ("field", "title") and value is not None:
                self._detail_back(kind, value)
            if not current:
                continue
            kinds = self.details.setdefault(index, {})
            if value is None:
                kinds.pop(kind, None)
            else:
                kinds[kind] = value
            keys.append((index, kind))
        for client in self.clients() + self.watchers():
            client.mark_nd(self.name, keys)

    def on_log(self, run_id: str, records: list[LinkRecord]) -> None:
        if self._awaiting_goal:
            self._early.append((self.on_log, (run_id, records)))
            return
        run = self.run
        if run is None or run.run_id != run_id:
            return
        records = sorted(records, key=lambda record: record.seq)
        if self._log_buffer is not None:
            self._log_buffer += records
            return
        if records and records[0].seq > self._log_next:
            self._log_buffer = list(records)
            self.gw.spawn(self._repair_log(run_id, self._log_next, records[0].seq))
            return
        self._append_log(records)

    async def _repair_log(self, run_id: str, start: int, end: int) -> None:
        """Fetch log records missed between ``start`` and ``end`` (a gap in the numbers)."""
        fetched: list[LinkRecord] = []
        try:
            while start + len(fetched) < end:
                part = await self.link.get_log(run_id, start + len(fetched), end - start - len(fetched))
                if not part:
                    break
                fetched += part
        except LinkDown:
            pass
        buffered, self._log_buffer = self._log_buffer or [], None
        if self.run is None or self.run.run_id != run_id:
            return
        if not fetched:
            self._log_next = end  # lost for good: go on after the gap
        self._append_log(fetched + buffered)

    def _append_log(self, records: list[LinkRecord]) -> None:
        lines = []
        for record in sorted(records, key=lambda item: item.seq):
            if record.seq < self._log_next:
                continue
            line = (record.seq, log_line(record))
            self.log.append(line)
            lines.append(line)
            self._log_next = record.seq + 1
        if lines:
            for client in self.clients():
                client.add_log(self.name, lines)

    def on_result(self, result: LinkResult) -> None:
        if result.run_id != self.own_run_id or result.run_id in self.finished:
            return
        self._end_own_run(result)

    def _end_own_run(self, result: LinkResult | None) -> None:
        """The own run ended: its result's blackboard becomes the gateway's copy; edits that
        crossed the end are applied to it (K-05); waiting sources take effect."""
        run_id = self.own_run_id
        if run_id is not None:
            self.finished.add(run_id)
        self.result_pending = False
        self.run_keys = set()
        self.run_nodes = set()
        if self._result_timer is not None:
            self._result_timer.cancel()
            self._result_timer = None
        self._pending_fields.clear()
        self.gw.save_memory()
        if result is not None:
            self.board_changed(self.board.replace(result.blackboard))
            log.info("Run %s of %s on %s ended: outcome %d (%s)", run_id, self.root, self.name, result.outcome,
                     result.message)
        later, self._after_result = self._after_result, []
        for reply, op in later:
            self._apply_late(reply, op)
        sources, self._pending_sources = self._pending_sources, []
        for op in sources:
            self._apply_late(None, op)
        message = self.run_message("ended")
        if run_id is not None:
            self._ended.add(run_id)
        if message is not None:
            if result is not None:
                message["outcome"], message["message"] = result.outcome, result.message
            for client in self.clients():
                client.push(message)
            watched = dict(message, bundle=self.run.bundle if self.run is not None else None)
            for client in self.watchers():
                client.push(watched)
        self.mark_st()
