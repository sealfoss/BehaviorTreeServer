# Software installed on the server

What was installed on `MOTHER-BRAIN` (Ubuntu 26.04.1 LTS, Python 3.14.4, amd64) to build, test
and run the Behavior Tree Server, and why. `install.sh` installs the same set on a fresh machine
(skipping what is present).

## System packages (apt)

| Package | Why |
|---|---|
| `curl` | Fetch the ROS 2 apt source package (it was not installed). |
| `git` | Version control of the project. |
| `python3-venv` | Create the project's Python environment (`python3 -m venv --system-site-packages`). |
| `python3-pip` | pip inside the virtual environment. |
| `software-properties-common` | `add-apt-repository`, used by the ROS installation guide (universe is already enabled). |
| `ros2-apt-source` 1.3.0 (`.deb` from github.com/ros-infrastructure/ros-apt-source) | Adds the ROS 2 apt repository `packages.ros.org/ros2/ubuntu resolute` and its key. |
| `ros-lyrical-ros-base` | ROS 2 Lyrical Luth: rclpy, the middleware (Fast DDS), `ros2` CLI. 203 `ros-lyrical-*` packages with dependencies. |
| `ros-dev-tools` | `colcon`, `rosdep` and the build tools for the workspace. |
| `python3-aiohttp` (3.13.3) | The gateway's web server (design 9.1). |
| `ros-lyrical-example-interfaces` | Interfaces used by the tests' scripted action servers and services. |
| `ros-lyrical-nav2-msgs` | A real-world interface (`NavigateToPose`) for the interface-description tests. |
| `avahi-utils` | `avahi-publish`, which publishes `bt.<hostname>.local` (design 14.3). |
| `build-essential`, `cmake` | Compile the interface package (`bt_server_interfaces`). |

## Python packages (pip, in the project's virtual environment only)

| Package | Why |
|---|---|
| `pytest` | Test runner. |
| `pytest-asyncio`, `pytest-aiohttp` | Tests of the gateway with aiohttp's test client. |
| `py_trees` | Test dependency only: the engine is compared against py_trees (design 17). |
| `playwright`, `pytest-playwright` | Browser tests of the page (design 17, Appendix B.13). `python -m playwright install chromium` downloads Chrome Headless Shell to `~/.cache/ms-playwright` (no system packages were needed on this machine). |

Nothing is installed into the system Python with pip.

## What the scripts set up on this machine

| Item | By | What |
|---|---|---|
| `~/projects/BehaviorTreeServer/.venv`, `build/`, `install/`, `config/`, `.data/` | `install.sh` | The development environment, its build, settings for this machine (gateway on 192.168.68.77:8080) and its trees, logs and goal journal. |
| `/opt/bt_server/` (`venv/`, `build/`, `install/`, `bin/bt-publish-name`) | `setup_system_service.sh` | The build the services run, owned by the user who ran the script. |
| System user `btserver`, home `/var/lib/bt_server` | `setup_system_service.sh` | Runs both services; holds the trees (`/var/lib/bt_server/trees`), the goal journal and the gateway's state. |
| `/etc/bt_server/gateway.toml`, `executor.yaml`, `namespace` | `setup_system_service.sh` | The services' settings (gateway on 192.168.68.77:80, executor `/cell1/bt_executor`, ROS domain 0). |
| `/etc/systemd/system/bt-executor.service`, `bt-gateway.service`, `bt-server-name.service`, `bt-server.target` | `setup_system_service.sh` | The units, enabled for boot. Tree logs go to `/var/log/bt_server`. |
| rosdep's sources list (`/etc/ros/rosdep/sources.list.d/20-default.list`) | `install.sh` | `rosdep init`, so the package.xml dependencies can be resolved. |
| Chrome Headless Shell in `~/.cache/ms-playwright` | `install.sh` | Browser for the page tests. |

## orion: a Jetson, in a container

`orion` (Jetson Orin, JetPack 6.2 = L4T R36.5.0, Ubuntu 22.04.5, aarch64) runs the server in an
Ubuntu 26.04 container ([JETSON.md](JETSON.md)). The image holds the software listed above, put
there by `install.sh` during the image build; the host got nothing of ROS.

**Host packages:** none were installed. `jetson_install.sh` checks for `docker` (JetPack's
`docker-ce` 29.6 was there), `avahi-daemon`, `avahi-utils`, `iproute2` and `curl`, which were all
present, and that `docker.service` and `avahi-daemon.service` are enabled and running.

**In the image only** (`container/Dockerfile`): the packages of the tables above; for the test
image also `iproute2` and the libraries Playwright's Chromium needs (`playwright install-deps`),
with Chromium in `/opt/ms-playwright`.

| Item | By | What |
|---|---|---|
| Images `bt-server:latest`, `bt-server-test:latest`, and Docker's build cache | `jetson_install.sh` (`--test` for the second) | The server; the test suite. |
| `~/BehaviorTreeServer/config/`, `.data/` | `jetson_install.sh` | Settings for `jetson_run.sh` (gateway on 192.168.68.86:8080, executor `/orion/bt_executor`), `config/apt-packages` (empty), and its trees, logs and goal journal. |
| System user `btserver` (uid 999), home `/var/lib/bt_server` | `jetson_setup_system_service.sh` | The container's processes run as it: trees, goal journal, the gateway's state. |
| `/etc/bt_server/gateway.toml`, `executor.yaml`, `namespace` | `jetson_setup_system_service.sh` | The service's settings (gateway on 192.168.68.86:80, executor `/orion/bt_executor`, ROS domain 0). |
| `/opt/bt_server/bin/bt-publish-name` | `jetson_setup_system_service.sh` | The name publisher, on the host. |
| `/etc/systemd/system/bt-server-container.service`, `bt-server-name.service`, `bt-server.target` | `jetson_setup_system_service.sh` | The units, enabled for boot and for Docker's start. Tree logs go to `/var/log/bt_server`. |
