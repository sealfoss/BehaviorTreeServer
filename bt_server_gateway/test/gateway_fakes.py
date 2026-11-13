"""A scripted executor link for the cases LocalLink cannot stage: an executor that is down
(G-22), answers that cross the end of a run (K-05), a run whose goal waits (G-08, K-06)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable

from bt_server_core.node_types import builtin_catalog

from bt_server_gateway.link import (
    APPLIED,
    GoalRejected,
    LinkDown,
    LinkRecord,
    LinkResult,
    LinkRun,
    LinkStatus,
    RunListener,
)
from bt_server_gateway.local_link import LocalLink, LocalLinkFactory


class FakeLink:
    """An executor whose answers the test decides."""

    def __init__(self, name: str, online: bool = True, catalog: list[dict] | None = None) -> None:
        self.name = name
        self.online = online
        self.catalog = catalog if catalog is not None else builtin_catalog(ros=False)
        self.listeners: list[RunListener] = []
        self.apply_answer: tuple[int, str] = (APPLIED, "")
        self.applied: list[list[dict]] = []
        self.executed: list[dict] = []
        self.findings: list[dict] = []
        self.run = LinkRun(None)
        self.state = 0
        self.seq = 0

    def subscribe(self, listener: RunListener) -> None:
        self.listeners.append(listener)

    def emit(self, call: Callable[[RunListener], None]) -> None:
        for listener in self.listeners:
            call(listener)

    def _check(self) -> None:
        if not self.online:
            raise LinkDown(f"{self.name} is down")

    async def start(self) -> None:
        if self.online:
            self.emit(lambda listener: listener.on_online(True))
            self.emit(lambda listener: listener.on_catalog(self.catalog))
            self.emit(lambda listener: listener.on_run(self.run))
            self.status(self.state)

    async def close(self) -> None:
        self.listeners.clear()  # the executor goes on; this gateway no longer listens

    def status(self, state: int, outcome: int = 0, statuses: bytes = b"") -> None:
        self.seq += 1
        status = LinkStatus(self.run.run_id, self.seq, state, 0, 0, 0, outcome, "", statuses)
        self.emit(lambda listener: listener.on_status(status))

    async def describe(self, kind: str, arg: dict) -> dict:
        self._check()
        return {"types": self.catalog} if kind == "node_types" else {}

    async def validate(self, bundle: dict, blackboard: list[dict]) -> list[dict]:
        self._check()
        return list(self.findings)

    async def execute(self, bundle: dict, blackboard: list[dict], label: str, requested_by: str) -> str:
        self._check()
        if self.run.run_id is not None and self.executed and self.executed[-1].get("open"):
            raise GoalRejected("busy")
        run_id = uuid.uuid4().hex
        self.executed.append({"run_id": run_id, "bundle": bundle, "blackboard": blackboard, "open": True})
        self.run = LinkRun(run_id, label, requested_by, bundle, ["x"])
        self.emit(lambda listener: listener.on_run(self.run))
        self.state = 1
        self.status(1, statuses=b"\x01")
        return run_id

    def finish(self, blackboard: list[dict], outcome: int = 1) -> None:
        """End the run: IDLE status, then the result (decoded values, with ids)."""
        self.executed[-1]["open"] = False
        self.state = 0
        self.status(0, outcome, b"\x02")
        result = LinkResult(self.run.run_id or "", outcome, "Succeeded", 1, 0, blackboard, "succeeded")
        self.emit(lambda listener: listener.on_result(result))

    async def stop(self, run_id: str) -> None:
        self._check()

    async def pause(self, paused: bool) -> None:
        self._check()

    async def reset(self) -> None:
        self._check()

    async def apply(self, ops: list[dict]) -> tuple[int, str]:
        self._check()
        self.applied.append(ops)
        return self.apply_answer

    async def snapshot(self) -> dict:
        self._check()
        return {"run": self.run, "status": LinkStatus(self.run.run_id, self.seq, self.state, 0, 0, 0, 0, "", b""),
                "blackboard_seq": 0, "blackboard": None, "details_seq": 0, "details": [], "log_seq": -1}

    async def get_log(self, run_id: str | None, from_seq: int, max_records: int) -> list[LinkRecord]:
        self._check()
        return []


class FakeFactory:
    """Offers the given links."""

    def __init__(self, *links: FakeLink) -> None:
        self.links = {link.name: link for link in links}

    async def start(self, on_executors: Callable[[list[str]], None]) -> None:
        on_executors([name for name, link in self.links.items() if link.online])

    def link(self, name: str) -> FakeLink:
        if name not in self.links:
            self.links[name] = FakeLink(name, online=False)
        return self.links[name]

    async def close(self) -> None:
        pass


class GatedLink(LocalLink):
    """A LocalLink whose validation waits until the test opens the gate: the session stays in
    *starting* meanwhile."""

    def __init__(self, name: str, settings) -> None:  # noqa: ANN001
        super().__init__(name, settings)
        self.gate = asyncio.Event()
        self.gate.set()

    async def validate(self, bundle: dict, blackboard: list[dict]) -> list[dict]:
        await self.gate.wait()
        return await super().validate(bundle, blackboard)


class GatedFactory(LocalLinkFactory):
    def link(self, name: str) -> GatedLink:
        if name not in self._links:
            self._links[name] = GatedLink(name, self._settings)
        return self._links[name]
