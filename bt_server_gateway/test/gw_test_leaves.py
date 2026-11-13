"""Custom leaves for the gateway's tests (a leaf module LocalLink loads through ``leaf_modules``)."""

from __future__ import annotations

from typing import Any, ClassVar

from bt_server_core.leaves import AsyncLeaf, InstantLeaf, LeafContext


class Hold(AsyncLeaf):
    """Succeeds ``Seconds`` after it starts; records the ``Label`` it read when it started."""

    TYPE_NAME = "Hold"
    fields = {"Seconds": 0.5, "Label": ""}
    started: ClassVar[list[str]] = []

    def on_start(self, ctx: LeafContext) -> None:
        Hold.started.append(ctx.field("Label"))
        self._timer = ctx.scheduler.call_at(ctx.now() + float(ctx.field("Seconds")), self.succeed)

    def on_cancel(self, ctx: LeafContext) -> None:
        self._timer.cancel()
        self.settled()

    def on_end(self, ctx: LeafContext) -> None:
        self._timer.cancel()


class BigList(InstantLeaf):
    """Sets the entry ``big`` to a list of ``Count`` integers (K-09)."""

    TYPE_NAME = "BigList"
    fields = {"Count": 100_000}

    def tick(self, ctx: LeafContext) -> Any:
        ctx.set("big", list(range(int(ctx.field("Count")))))
        return True


class Chatter(InstantLeaf):
    """Writes ``Lines`` log lines (G-12)."""

    TYPE_NAME = "Chatter"
    fields = {"Lines": 100}

    def tick(self, ctx: LeafContext) -> Any:
        for number in range(int(ctx.field("Lines"))):
            ctx.log(4, f"chatter {number}")
        return True


class Touch(InstantLeaf):
    """Sets its own field ``Note`` and its title, as leaf code may (design 9.3)."""

    TYPE_NAME = "Touch"
    fields = {"Note": ""}

    def tick(self, ctx: LeafContext) -> Any:
        ctx.set_field("Note", "touched")
        ctx.set_title("Touched")
        return True
