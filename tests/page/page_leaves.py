"""Leaves for the page tests: a gateway with ``link = "local"`` loads this module through
``leaf_modules`` (the tests put this folder on the gateway's ``PYTHONPATH``)."""

from __future__ import annotations

from typing import Any

from bt_server_core.leaves import AsyncLeaf, LeafContext


class PageHold(AsyncLeaf):
    """Runs for ``Seconds``, then succeeds; halted, it settles at once."""

    TYPE_NAME = "PageHold"
    title = "Page Hold"
    fields = {"Seconds": 30.0}

    def on_start(self, ctx: LeafContext) -> None:
        self._timer = ctx.scheduler.call_at(ctx.now() + float(ctx.field("Seconds")), self.succeed)

    def on_cancel(self, ctx: LeafContext) -> None:
        self._timer.cancel()
        self.settled()

    def on_end(self, ctx: LeafContext) -> None:
        self._timer.cancel()


class SlowCancel(AsyncLeaf):
    """Runs until halted; a cancel is confirmed only ``CancelSeconds`` later, as an action
    server that cancels slowly (P-06)."""

    TYPE_NAME = "SlowCancel"
    title = "Slow Cancel"
    fields = {"CancelSeconds": 4.0}

    def on_start(self, ctx: LeafContext) -> None:
        self._timer: Any = None

    def on_cancel(self, ctx: LeafContext) -> None:
        self._timer = ctx.scheduler.call_at(ctx.now() + float(ctx.field("CancelSeconds")), self.settled)

    def on_end(self, ctx: LeafContext) -> None:
        if self._timer is not None:
            self._timer.cancel()
