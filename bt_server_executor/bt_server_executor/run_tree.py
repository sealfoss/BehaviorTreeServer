"""``run_tree``: run a tree file on an executor from a terminal or a launch file (design 3.2).

    ros2 run bt_server_executor run_tree robot_BT.json [--executor /cell1/bt_executor]
                                                       [--trees-dir DIR] [--label NAME] [--log]

It reads the tree and its blackboard file with the core document model, resolves its Sub-Tree
and Remote Sub-Tree nodes into a bundle (design 4.3) as the gateway does, sends the
``ExecuteTree`` goal, prints the run's state changes (and with ``--log`` its log lines) and the
outcome. Ctrl-C cancels the goal (Stop) and waits for the result. The exit code is 0 when the
Root succeeded, 1 for any other outcome, 2 when the tree cannot be read or the goal is
rejected.

Sub-tree files are looked up relative to ``--trees-dir`` (by default the folder of the tree
file), which plays the part of the gateway's ``trees_dir``. Node types are taken from the
executor's catalog (``~/describe``), so its custom leaves are known; without an answer the
built-in types are used.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from typing import Any

import rclpy
from rclpy.action import ActionClient
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.signals import SignalHandlerOptions

from bt_server_core.bundle import build_bundle
from bt_server_core.document import TreeDocument
from bt_server_core.files import PathError, TreeFileError, key_of_file, load_tree
from bt_server_core.node_types import builtin_catalog
from bt_server_core.treelog import LEVEL_NAMES, LogLevel, time_text
from bt_server_core.values import dumps_json, loads_json
from bt_server_interfaces.action import ExecuteTree
from bt_server_interfaces.msg import LogBatch
from bt_server_interfaces.srv import Describe

from .qos import LOG_QOS

__all__ = ["main", "make_bundle"]

STATES = ["IDLE", "RUNNING", "PAUSED", "STOPPING", "RESTARTING"]
OUTCOMES = ["none", "succeeded", "failed", "stopped", "error"]


def make_bundle(tree_file: str, trees_dir: str | None, catalog: list[dict]) -> tuple[dict, list, str]:
    """``(bundle, blackboard entries, document key)`` of a tree file.

    Raises:
        TreeFileError, PathError: the tree cannot be read.
    """
    root = os.path.abspath(trees_dir or os.path.dirname(os.path.abspath(tree_file)))
    key = key_of_file(os.path.abspath(tree_file), root)
    loaded = load_tree(root, key)

    def get_document(name: str) -> TreeDocument:
        found = loaded if name == key else load_tree(root, name)
        return TreeDocument(found.data, catalog, key=name, path=found.path)

    def get_blackboard(name: str) -> list:
        return list(load_tree(root, name).blackboard)

    bundle = build_bundle(key, get_document, get_blackboard)
    return bundle, list(loaded.blackboard), key


def _catalog(node: Node, executor: SingleThreadedExecutor, name: str) -> list[dict]:
    client = node.create_client(Describe, f"{name}/describe")
    try:
        if client.wait_for_service(timeout_sec=5.0):
            future = client.call_async(Describe.Request(kind="node_types", arg_json="{}"))
            executor.spin_until_future_complete(future, timeout_sec=5.0)
            response = future.result() if future.done() else None
            if response is not None and response.ok:
                return loads_json(response.result_json)["types"]
    finally:
        node.destroy_client(client)
    print("run_tree: the executor's node types are not known; using the built-in ones", file=sys.stderr)
    return builtin_catalog()


def main(argv: list[str] | None = None) -> int:
    args_in = sys.argv if argv is None else ["run_tree", *argv]
    ros_args = rclpy.utilities.remove_ros_args(args_in)[1:]
    parser = argparse.ArgumentParser(prog="run_tree", description="Run a tree file on a Behavior Tree Server executor.")
    parser.add_argument("tree", help="the tree file (robot_BT.json, or any of the widget's spellings)")
    parser.add_argument("--executor", default="/bt_executor", help="the executor's name (default /bt_executor)")
    parser.add_argument("--trees-dir", help="the folder sub-tree files are relative to (default: the tree's folder)")
    parser.add_argument("--label", help="the run's label (default: the tree's name)")
    parser.add_argument("--log", action="store_true", help="print the run's log lines")
    options = parser.parse_args(ros_args)
    name = "/" + options.executor.strip("/")

    rclpy.init(args=args_in, signal_handler_options=SignalHandlerOptions.NO)
    node = Node("run_tree")
    executor = SingleThreadedExecutor()
    executor.add_node(node)
    interrupted = [0]
    signal.signal(signal.SIGINT, lambda *_: interrupted.__setitem__(0, interrupted[0] + 1))
    try:
        return _run(node, executor, name, options, interrupted)
    finally:
        executor.shutdown()
        node.destroy_node()
        rclpy.try_shutdown()


def _run(node: Node, executor: SingleThreadedExecutor, name: str, options: Any, interrupted: list[int]) -> int:
    client = ActionClient(node, ExecuteTree, f"{name}/execute")
    if not client.wait_for_server(timeout_sec=10.0):
        print(f"run_tree: the executor {name} is not available", file=sys.stderr)
        return 2
    try:
        bundle, blackboard, key = make_bundle(options.tree, options.trees_dir, _catalog(node, executor, name))
    except (TreeFileError, PathError, KeyError, OSError, ValueError) as error:
        print(f"run_tree: {error}", file=sys.stderr)
        return 2

    run_id = [b""]

    def on_log(batch: LogBatch) -> None:
        if bytes(bytearray(batch.run_id.uuid)) != run_id[0]:
            return
        for record in batch.records:
            stamp = record.stamp.sec + record.stamp.nanosec / 1e9
            print(f"{time_text(stamp)} {LEVEL_NAMES.get(LogLevel(record.level), 'INFO'):<7} {record.text}")

    if options.log:
        node.create_subscription(LogBatch, f"{name}/log", on_log, LOG_QOS)

    def on_feedback(message: Any) -> None:
        feedback = message.feedback
        state = STATES[feedback.state] if feedback.state < len(STATES) else str(feedback.state)
        print(f"run_tree: {state}, loops completed: {feedback.loop_count}")

    goal = ExecuteTree.Goal(bundle_json=dumps_json(bundle, indent=None), blackboard_json=dumps_json(blackboard, indent=None),
                            label=options.label or key.replace("/", "_"), requested_by="command line")
    sent = client.send_goal_async(goal, feedback_callback=on_feedback)
    executor.spin_until_future_complete(sent)
    handle = sent.result()
    if handle is None or not handle.accepted:
        print(f"run_tree: {name} rejected the tree (busy, or the bundle cannot be read)", file=sys.stderr)
        return 2
    run_id[0] = bytes(bytearray(handle.goal_id.uuid))
    print(f"run_tree: run {run_id[0].hex()} of '{key}' started on {name}")
    result_future = handle.get_result_async()
    cancelled = 0
    while not result_future.done():
        executor.spin_once(timeout_sec=0.1)
        if interrupted[0] > cancelled:
            cancelled = interrupted[0]
            if cancelled >= 3:
                print("run_tree: given up waiting; the run may go on", file=sys.stderr)
                return 1
            print("run_tree: stopping the run (Ctrl-C again to stop waiting)")
            handle.cancel_goal_async()
    end = time.monotonic() + 0.3
    while time.monotonic() < end:  # the last log lines
        executor.spin_once(timeout_sec=0.05)
    response = result_future.result()
    result = response.result
    outcome = OUTCOMES[result.outcome] if result.outcome < len(OUTCOMES) else str(result.outcome)
    print(f"run_tree: {outcome}: {result.message} (loops completed: {result.loops_completed}, "
          f"unconfirmed cancels: {result.unconfirmed_cancels})")
    return 0 if result.outcome == ExecuteTree.Result.OUTCOME_SUCCEEDED else 1


if __name__ == "__main__":
    sys.exit(main())
