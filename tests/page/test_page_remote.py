"""M-09 (Appendix B.10): Zoom In on a Remote Sub-Tree node during the run shows the remote tree
with its live statuses, read-only. Two executor processes (``/m09a``, ``/m09b``) and a gateway
with ``link = "ros"``, on ROS_DOMAIN_ID 51, discovery limited to this machine."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from page_helpers import child_env, node, root, wait_until, write_tree

pytestmark = [pytest.mark.page, pytest.mark.ros, pytest.mark.slow]

pytest.importorskip("rclpy")
pytest.importorskip("bt_server_interfaces.action")

DOMAIN_ID = "51"
LOCAL = "/m09a/bt_executor"
REMOTE = "/m09b/bt_executor"


def ros_env() -> dict[str, str]:
    env = child_env()
    env["ROS_DOMAIN_ID"] = DOMAIN_ID
    env["ROS_AUTOMATIC_DISCOVERY_RANGE"] = "LOCALHOST"
    return env


class Executor:
    """``python -m bt_server_executor`` in a namespace of its own."""

    def __init__(self, folder: Path, ns: str) -> None:
        self.folder = folder / ns.strip("/")
        self.folder.mkdir(parents=True, exist_ok=True)
        params = self.folder / "params.yaml"
        params.write_text("/**:\n  ros__parameters:\n"
                          f'    log_dir: "{(self.folder / "log").as_posix()}"\n'
                          f'    journal_file: "{(self.folder / "goals.jsonl").as_posix()}"\n', encoding="utf-8")
        self.output = self.folder / "executor.out"
        with open(self.output, "ab") as handle:
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "bt_server_executor", "--ros-args", "-r", f"__ns:={ns}", "-r",
                 "__node:=bt_executor", "--params-file", str(params)],
                env=ros_env(), stdout=handle, stderr=subprocess.STDOUT, cwd=str(self.folder))

    def stop(self) -> None:
        if self.proc.poll() is not None:
            return
        if os.name == "nt":
            self.proc.terminate()
        else:
            self.proc.send_signal(signal.SIGINT)
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)

    def text(self) -> str:
        return self.output.read_text(encoding="utf-8", errors="replace") if self.output.exists() else ""


@pytest.fixture
def executors(tmp_path):
    started = [Executor(tmp_path, "/m09a"), Executor(tmp_path, "/m09b")]
    yield started
    for executor in started:
        executor.stop()


def test_M_09_zoom_in_on_a_remote_sub_tree_during_the_run(executors, start_gateway, open_tab):
    top = root()
    remote_node = node("RemoteSubTree", "Remote", -80, 140,
                       fields={"Executor": REMOTE, "File": "remote_BT.json", "Timeout": 0.0, "Watchdog": 5.0})
    remote_root = root()
    sequence = node("Sequence", "Sequence", -20, 110, memory=True)
    waiting = node("Evaluation", "Wait for hold", -80, 230,
                   fields={"ValueName": "hold", "CompareTo": None, "LiteralValue": True, "Operator": "==",
                           "Wait": True, "WaitTimeout": 0.0})

    def prepare(trees: Path) -> None:
        write_tree(trees, "m09", [top, remote_node], [(top, remote_node)], blackboard=[])
        write_tree(trees, "remote", [remote_root, sequence, waiting], [(remote_root, sequence), (sequence, waiting)],
                   blackboard=[{"name": "hold", "type": "Bool", "value": False, "reset": False}])

    gateway = start_gateway(prepare, env=ros_env(), link="ros", executors=[LOCAL, REMOTE])
    tab = open_tab(gateway, executor=LOCAL, wait_tree=False)
    wait_until(lambda: tab.tid("executor-picker").input_value() == LOCAL, message="not attached to /m09a")
    tab.load("m09")
    execute = tab.tid("btn-execute")
    wait_until(lambda: execute.is_enabled(), timeout=40,
               message=f"Execute stays unavailable ({tab.run_state()!r}):\n{executors[0].text()[-2000:]}")
    execute.click()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and tab.status(remote_node["id"]) != "running":
        if tab.tid("btn-execute-anyway").count():
            tab.tid("btn-execute-anyway").click()
        time.sleep(0.1)
    assert tab.status(remote_node["id"]) == "running", (tab.run_state(), gateway.text()[-3000:])
    status = tab.tid(f"status-{remote_node['id']}")
    wait_until(lambda: REMOTE in (status.get_attribute("title") or ""), timeout=20,
               message=f"no remote run in the details: {status.get_attribute('title')!r}")

    tab.menu(remote_node["id"], "Zoom In")
    tab.tid("read-only-banner").wait_for(timeout=10_000)
    tab.tid(f"node-{waiting['id']}").wait_for(timeout=10_000)
    wait_until(lambda: tab.status(waiting["id"]) == "running", timeout=10,
               message=f"the remote leaf shows {tab.status(waiting['id'])!r}")
    assert tab.status(remote_root["id"]) == "running"
    assert tab.status(sequence["id"]) == "running"
    # Read-only: the editors are disabled and the remote tree is not one of the session's documents.
    assert tab.tid(f"field-{waiting['id']}-Wait").is_disabled()
    assert tab.tid("btn-save").is_disabled()

    tab.tid("btn-go-back").click()
    tab.tid(f"node-{remote_node['id']}").wait_for()
    assert tab.tid("read-only-banner").count() == 0
    tab.tid("btn-stop").click()
    wait_until(lambda: "IDLE" in tab.run_state(), timeout=20, message=tab.run_state())
    assert not tab.errors, tab.errors
