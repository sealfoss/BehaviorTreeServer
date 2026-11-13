# Behavior Tree Server

The features of `behavior-tree-widget` as a web application. One machine on a LAN hosts it;
any browser on that LAN draws, runs and watches behavior trees; the running tree coordinates
work on other machines through ROS 2 (Lyrical).

* Design: [docs/BehaviorTreeServer.md](docs/BehaviorTreeServer.md)
* Decisions the design left open, and the interfaces between the parts:
  [docs/CONTRACT.md](docs/CONTRACT.md)
* Platform facts checked on the server: [docs/VERIFIED.md](docs/VERIFIED.md)
* Software installed on the server, and why: [docs/INSTALLED.md](docs/INSTALLED.md)

## Parts

| Folder | What |
|---|---|
| `bt_server_interfaces/` | The tree API's ROS interfaces (`ExecuteTree` action, messages, services; design Appendix A). |
| `bt_server_core/` | No ROS, no web: the document model (the widget's two JSON files), bundles, the engine (statuses, Parallel, the cancelling lifecycle, event-driven ticks), the blackboard, leaf base classes, the tree log, example leaves. |
| `bt_server_executor/` | The executor: an rclpy node that runs trees and offers them as the tree API; the ROS leaves (Call Action, Call Service, Publish, Remote Sub-Tree), entries fed from topics, the goal journal, `run_tree`. |
| `bt_server_gateway/` | The web server (aiohttp): tree files, shared documents and edit operations, browser sessions over one WebSocket per tab, the links to executors (`RosLink`, or `LocalLink` to run trees in-process without ROS). Holds the built page under `bt_server_gateway/static/`. |
| `bt_server_bringup/` | Launch file, parameter files, systemd unit templates. |
| `web/` | The page: Svelte 5 + TypeScript + Vite. Its build is committed into the gateway, so the server needs no Node.js. |
| `examples/trees/` | Example trees (`hello`, `demo`, `patrol`) copied into a new installation's trees folder. |
| `tests/page/` | Browser tests of the page (Playwright) against a real gateway. |
| `tools/` | `smoke_test.py` (drives a running server like a browser), `make_example_trees.py`. |

## Install, run, and run as a service

On Ubuntu 26.04:

```bash
./install.sh                    # system packages that are missing (ROS 2 Lyrical, aiohttp, avahi-utils, ...),
                                # the Python environment .venv (--system-site-packages), the colcon build,
                                # and settings for this machine in config/
./run.sh                        # executor /cell1/bt_executor and the gateway on http://<LAN address>:8080/
./setup_system_service.sh       # build into /opt/bt_server, user btserver, /etc/bt_server/*, and the units
                                # bt-executor, bt-gateway, bt-server-name (+ bt-server.target), enabled and started
```

With the services running the page is at `http://bt-<hostname>.local/` (Windows, Linux),
`http://bt.<hostname>.local/` (macOS, iOS), or `http://<LAN address>/`. Windows does not look up
three-part `.local` names, so both names are published (see docs/VERIFIED.md).

```bash
systemctl status bt-server.target              # all three units
journalctl -u bt-gateway -u bt-executor -f     # their logs
sudo systemctl restart bt-server.target
python3 tools/smoke_test.py http://<LAN address>/ --tree demo   # a run through the WebSocket
```

Without ROS (for example on Windows), the gateway runs trees in-process: a settings file with
`link = "local"` and `python -m bt_server_gateway --config <file>` (design 3.3, `LocalLink`).

## Tests

```bash
source /opt/ros/lyrical/setup.bash && source install/setup.bash
export ROS_DOMAIN_ID=71 ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
.venv/bin/python -m pytest -q          # every package and the page tests
```

Tests are named after the cases of the design's Appendix B (`test_E_24_...`, `test_X_07_...`,
`test_P_06_...`). Tests that need ROS are marked `ros` and skipped without rclpy; the page tests
are marked `page` and skipped without Playwright's Chromium. The engine is also compared with
py_trees on random trees (a test dependency only).

The page: `cd web && npm ci && npm run check && npm test && npm run build`.
