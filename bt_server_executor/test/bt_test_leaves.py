"""Leaf classes for the executor tests, loaded by the executor processes the tests start
(``leaf_modules: ["bt_test_leaves"]``; the test folder is on their ``PYTHONPATH``).

``TestFib`` sends a ``example_interfaces/action/Fibonacci`` goal through ``ctx.run``'s client
cache and goal journal, the way the real Call Action leaf does, until that exists (X-07, X-09).
"""

from __future__ import annotations

import uuid
from typing import Any

from example_interfaces.action import Fibonacci
from unique_identifier_msgs.msg import UUID

from bt_server_core.leaves import AsyncLeaf, InstantLeaf, LeafContext


class TestSucceed(InstantLeaf):
    """Succeeds at once."""

    def tick(self, ctx: LeafContext) -> Any:
        return True


class TestFail(InstantLeaf):
    """Fails at once."""

    def tick(self, ctx: LeafContext) -> Any:
        return False


class _Broken:
    """A return value the engine cannot read: reading its ``name`` raises, outside the leaf's own
    error handling, so the tick itself fails (outcome *error*)."""

    @property
    def name(self) -> str:
        raise RuntimeError("the tick broke")


class Status(_Broken):
    """Named ``Status`` so that the engine looks at its ``name``."""


class TestBreakTick(InstantLeaf):
    """Makes the tick itself raise."""

    def tick(self, ctx: LeafContext) -> Any:
        return Status()


class TestLogMany(InstantLeaf):
    """Writes ``Count`` log records ("line <i>") and succeeds."""

    fields = {"Count": 100}

    def tick(self, ctx: LeafContext) -> Any:
        for number in range(int(ctx.field("Count"))):
            ctx.log(4, f"line {number}")
        return True


class TestHold(AsyncLeaf):
    """Runs until it is halted; its cancel is confirmed at once."""

    def on_start(self, ctx: LeafContext) -> None:
        ctx.detail("waiting", "holding")


class TestDetails(AsyncLeaf):
    """Sets its ``feedback`` detail to ``{"n": k}`` ``Count`` times, every ``Period`` seconds, then
    holds until halted."""

    fields = {"Count": 10, "Period": 0.15}

    def on_start(self, ctx: LeafContext) -> None:
        self._n = 0
        self._timer = ctx.scheduler.call_at(ctx.now() + float(ctx.field("Period")), self._next)

    def _next(self) -> None:
        ctx = self.ctx
        self._n += 1
        ctx.detail("feedback", {"n": self._n})
        if self._n < int(ctx.field("Count")):
            self._timer = ctx.scheduler.call_at(ctx.now() + float(ctx.field("Period")), self._next)
        else:
            self._timer = None

    def on_end(self, ctx: LeafContext) -> None:
        if self._timer is not None:
            self._timer.cancel()


class TestFib(AsyncLeaf):
    """Sends a Fibonacci goal to ``Action`` and succeeds or fails with its result; a halt cancels
    the goal and settles when the result says so."""

    fields = {"Action": "", "Order": 5}

    def prepare(self, ctx: LeafContext) -> None:
        ctx.run.action_client(Fibonacci, ctx.field("Action"))

    def on_start(self, ctx: LeafContext) -> None:
        self._name = ctx.field("Action")
        self._client = ctx.run.action_client(Fibonacci, self._name)
        self._handle: Any = None
        self._cancel_on_reply = False
        self._waited = 0
        self._goal_id: Any = None
        self._retry: Any = None
        self._send()

    def _send(self) -> None:
        ctx = self.ctx
        if not self._client.server_is_ready():
            self._waited += 1
            if self._waited > 20:
                self.fail("server not available")
            else:
                self._retry = ctx.scheduler.call_at(ctx.now() + 0.1, self._send)
            return
        self._goal_id = uuid.uuid4()
        name = ctx.node.resolve_topic_name(self._name)
        ctx.run.record_goal(name, self._goal_id)
        future = self._client.send_goal_async(Fibonacci.Goal(order=int(ctx.field("Order"))),
                                              goal_uuid=UUID(uuid=list(self._goal_id.bytes)))
        run = ctx.run
        future.add_done_callback(lambda done: self._on_reply(run, done))

    def _on_reply(self, run: Any, future: Any) -> None:
        handle = future.result()
        if not handle.accepted:
            run.goal_done(self._goal_id)
            self.fail("goal rejected")
            return
        self._handle = handle
        handle.get_result_async().add_done_callback(lambda done: self._on_result(run, done))
        if self._cancel_on_reply:
            self._cancel(run)

    def _on_result(self, run: Any, future: Any) -> None:
        run.goal_done(self._goal_id)
        status = future.result().status
        if self._cancel_on_reply:
            self.settled()
        elif status == 4:  # GoalStatus.STATUS_SUCCEEDED
            self.succeed()
        else:
            self.fail(f"the goal ended with status {status}")

    def _cancel(self, run: Any) -> None:
        self._handle.cancel_goal_async()
        run.goal_cancel_sent(self._goal_id)

    def on_cancel(self, ctx: LeafContext) -> None:
        self._cancel_on_reply = True
        if self._goal_id is None:  # still looking for the server: nothing was sent
            if self._retry is not None:
                self._retry.cancel()
            self.settled()
        elif self._handle is not None:
            self._cancel(ctx.run)
