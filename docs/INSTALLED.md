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
