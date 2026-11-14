# Behavior Tree Server

The features of `behavior-tree-widget` as a web application. One machine on a LAN hosts it;
any browser on that LAN draws, runs and watches behavior trees; the running tree coordinates
work on other machines through ROS 2 (Lyrical).

* Design: [docs/BehaviorTreeServer.md](docs/BehaviorTreeServer.md)
* Decisions the design left open, and the interfaces between the parts:
  [docs/CONTRACT.md](docs/CONTRACT.md)
* Platform facts checked on the server: [docs/VERIFIED.md](docs/VERIFIED.md)
* Software installed on the server, and why: [docs/INSTALLED.md](docs/INSTALLED.md)
* On a Jetson (JetPack 6, Ubuntu 22.04), in a container: [docs/JETSON.md](docs/JETSON.md)

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
| `container/` | The Ubuntu 26.04 image for hosts that cannot run Lyrical, such as a Jetson: `Dockerfile`, entry point, unit templates (docs/JETSON.md). |
| `lib/` | `common.sh` and `base.sh`, shared by the six install, run and setup scripts: the settings they write, their checks and the steps of a setup. |

## Install, run, and run as a service

On Ubuntu 26.04:

```bash
./install.sh                    # system packages that are missing (ROS 2 Lyrical, aiohttp, avahi-utils, ...),
                                # the Python environment .venv (--system-site-packages), the colcon build,
                                # and settings for this machine in config/
./run.sh                        # executor /<hostname>/bt_executor and the gateway on http://<LAN address>:8080/
                                # and http://bt-<hostname>.local:8080/ while it runs
./setup_system_service.sh       # build into /opt/bt_server, user btserver, /etc/bt_server/*, and the units
                                # bt-executor, bt-gateway, bt-server-name (+ bt-server.target), enabled and started
```

With the services running the page is at `http://bt-<hostname>.local/` (Windows, Linux),
`http://bt.<hostname>.local/` (macOS, iOS), or `http://<LAN address>/`. Windows does not look up
three-part `.local` names, so both names are published (see docs/VERIFIED.md).

Both kinds of install, here and on a Jetson, behave the same way:

* **The executor's namespace** is the host name made a ROS name (`/orion/bt_executor`,
  `/lab_2/bt_executor` for `lab-2`), so that servers in one ROS domain never share an executor
  name. An installation keeps the namespace it has (`config/namespace`, or the service's), and a
  setup run again keeps the service's namespace and ROS domain unless `--namespace` or `--domain`
  says otherwise.
* **The run scripts and the setups refuse to start an executor** whose name is on the ROS network
  of the domain already (another machine, or a service of this one), as two would both accept the
  same goals. They look with `ros2 node list` for about 5 s first. The run scripts also refuse
  while a service of this machine runs that name in that domain, when something listens on the
  gateway's port already, and when the settings' address is no longer this machine's (a new DHCP
  lease).
* **The run scripts** (`run.sh`, `jetson_run.sh`) publish `bt.<hostname>.local` and
  `bt-<hostname>.local` while they run (`--no-names`: not; the services have
  `bt-server-name.service`), and with a `--namespace` other than the settings' the gateway lists
  that run's executor, so that a page attaches to it (a copy of `gateway.toml`, removed
  afterwards). Ctrl+C stops a running tree as Stop does and waits for its cancels.
* **Settings that are kept** (no `--reconfigure`) and differ from what was asked for, or name an
  address that is no longer this machine's, get a warning, and so does an address that came from
  DHCP: give the machine a DHCP reservation.
* **A setup checks at the end** the units, the page, the executor on the ROS network, and that both
  names resolve to this gateway and are accepted by it. One machine has one kind of service:
  each setup refuses to run where the other's units are installed.

```bash
systemctl status bt-server.target              # all three units
journalctl -u bt-gateway -u bt-executor -f     # their logs
sudo systemctl restart bt-server.target
python3 tools/smoke_test.py http://<LAN address>/ --tree demo   # a run through the WebSocket
```

On a Jetson with JetPack 6 (Ubuntu 22.04), where Lyrical does not install, the server runs in an
Ubuntu 26.04 container ([docs/JETSON.md](docs/JETSON.md)):

```bash
./jetson_install.sh                    # Docker and Avahi's tools if missing, the image bt-server:latest, config/
./jetson_run.sh                        # executor /<hostname>/bt_executor and the gateway on http://<LAN address>:8080/
                                       # and http://bt-<hostname>.local:8080/ while it runs
./jetson_setup_system_service.sh       # user btserver, /etc/bt_server/*, and the units bt-server-container,
                                       # bt-server-name (+ bt-server.target), enabled and started
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
