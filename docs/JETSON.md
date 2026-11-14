# The Behavior Tree Server on a Jetson, in a container

ROS 2 Lyrical needs Ubuntu 26.04, and JetPack 6 is Ubuntu 22.04. On a Jetson the server therefore
runs in an Ubuntu 26.04 container: the image is made by the project's own `install.sh`, holds the
same software as a 26.04 machine ([INSTALLED.md](INSTALLED.md)), reads the same settings and starts
the same two processes with the same `run.sh`. Three scripts do for the container what the usual
three do without one:

| | Ubuntu 26.04 | Jetson (JetPack 6) |
|---|---|---|
| Install | `./install.sh` | `./jetson_install.sh` |
| Run in a terminal | `./run.sh` | `./jetson_run.sh` |
| Run as services that start at boot | `./setup_system_service.sh` | `./jetson_setup_system_service.sh` |

The container's files are in `container/`: `Dockerfile`, the entry point `entrypoint.sh`, and the
unit templates `*.in` (`bt-server-name.service.in`, the same for both kinds, is in
`bt_server_bringup/systemd/`). All six scripts share `lib/common.sh` (with `lib/base.sh`, the part that `install.sh` needs
before it has built anything): the settings they write,
their checks and the steps of a setup, so the two kinds behave the same way (README.md).
`.dockerignore` keeps this machine's `config/`, `.data/`, builds and caches out of the image. Every
script prints its options with `--help`.

## Install

```bash
./jetson_install.sh            # host packages that are missing, the image bt-server:latest,
                               # and settings for ./jetson_run.sh in config/
./jetson_install.sh --test     # the same, then the whole test suite in a container (bt-server-test:latest)
```

* **On the host** it installs only what the container cannot provide, when missing: Docker
  (`docker.io`; JetPack's own `docker-ce` is used when it is there), `avahi-daemon` and
  `avahi-utils` (for the `.local` names, see below), `iproute2` and `curl`. Nothing of ROS: the
  host's own ROS (such as Humble) is not touched.
* **The image** is Ubuntu 26.04 with ROS 2 Lyrical and the server in `/opt/bt_server` (`venv/`,
  `install/`, `bin/`), laid out as for the systemd services. It takes about 2 GB of disk, and
  Docker's build cache about as much again.
* **The first build** takes about 13 minutes on an Orin, most of it downloading Ubuntu's and ROS's
  packages. The dependencies are a layer of their own that only a change to `install.sh`,
  `lib/base.sh` or a `package.xml` rebuilds: after a change to the code (or to the scripts'
  `lib/common.sh`), a rebuild takes about a minute.
* **The test suite** (`--test`) runs on Docker's own network, in ROS domain 71 with discovery kept
  to the container, so it neither sees nor disturbs the ROS programs on the LAN. About 7 minutes.
* **After changing the code**, run `./jetson_install.sh` again, then
  `sudo systemctl restart bt-server.target` (or run `./jetson_setup_system_service.sh` again,
  which does both).
* `--port` must be 1024 or more: `jetson_run.sh`'s container has no right to lower ports (the
  service serves port 80). A kept `config/gateway.toml` is not changed by `--namespace` or `--port`
  alone (a warning says so): add `--reconfigure`.

### Your robots' interfaces and your own leaves

The executor calls only actions and services whose interface packages are installed where it runs,
and that is the image. It holds ROS 2's base packages (`std_msgs`, `geometry_msgs`, `nav_msgs`,
`sensor_msgs`, `std_srvs`, ...) and nothing for particular robots.

* **Released interface packages:** list them in `config/apt-packages`, one per line (the file is
  written with an example the first time), such as `ros-lyrical-nav2-msgs`. `./jetson_install.sh`
  installs them into the image, in a layer that only a change to the list rebuilds; the service
  uses the same image (restart it after the build).
* **Leaf modules of your own:** put the Python modules in the settings' `leaves/` folder,
  `config/leaves/` for `jetson_run.sh` and `/etc/bt_server/leaves/` for the service, and name them in
  `executor.yaml`'s `leaf_modules`. The container puts that folder on Python's path.
* **Interface packages that are not released for Lyrical** (a robot's own messages) have to be
  built for Lyrical: in an image of your own, `FROM bt-server:latest`, that builds them with colcon
  and sources the result. Run it with `./jetson_run.sh --image NAME`, and set it up with
  `./jetson_setup_system_service.sh --image NAME --skip-build`. Without `--skip-build` the setup
  rebuilds only `bt-server:latest` (never your image, which it then reminds you to rebuild).
* **Middleware:** the image has Fast DDS (`rmw_fastrtps_cpp`, ROS 2's default). For Cyclone DDS,
  add `ros-lyrical-rmw-cyclonedds-cpp` to `config/apt-packages` and set `RMW_IMPLEMENTATION`; the
  file that `CYCLONEDDS_URI` names is mounted at its own path.

## Run in a terminal

```bash
./jetson_run.sh                           # http://<LAN address>:8080/ and http://bt-<hostname>.local:8080/
./jetson_run.sh --domain 5 --namespace test
```

* Settings come from `config/`, and trees, tree logs, the goal journal and ROS's own logs go to
  `.data/` (in `.data/.ros/log`). The container sees `.data/` at the same path, so the settings
  are the same as `install.sh` writes, and runs as you.
* **Ctrl+C** stops it as `run.sh` does: a running tree is stopped and its cancels are waited for,
  then the script ends with 0. SIGTERM, and the terminal closing (SIGHUP), do the same; a second
  Ctrl+C changes nothing. The names are withdrawn however it ends.
* **It refuses to start**, with a message saying what to do, when:
  * an executor of the same name is on the ROS network of the domain already (another machine, or
    the service below): two executors of one name would both accept the same goals. It looks with
    `ros2 node list` for about 5 s first;
  * the service runs the same executor name in the same domain;
  * `config/gateway.toml`'s address is no longer this machine's (a new DHCP lease), its port is
    below 1024, or a folder in the settings is outside `.data/` (a moved checkout):
    `./jetson_install.sh --reconfigure` fixes these.
* With `--namespace NS` other than the settings', the gateway lists `/NS/bt_executor` for that
  run (a copy of `gateway.toml` in `.data/`, removed afterwards), so that a page attaches to it.
* Without membership of the group `docker`, the script runs itself under `sudo` (one password
  prompt). The container still runs as you, with no capabilities.
* ROS discovery and middleware settings in your environment (`ROS_AUTOMATIC_DISCOVERY_RANGE`,
  `ROS_STATIC_PEERS`, `ROS_DISCOVERY_SERVER`, `ROS_SUPER_CLIENT`, `RMW_IMPLEMENTATION`,
  `FASTDDS_DEFAULT_PROFILES_FILE`, `FASTRTPS_DEFAULT_PROFILES_FILE`, `FASTDDS_BUILTIN_TRANSPORTS`,
  `CYCLONEDDS_URI`) reach the container, as they reach `run.sh`; a profile file is mounted at its
  own path.

## Run as services that start at boot

```bash
./jetson_setup_system_service.sh          # runs ./jetson_install.sh, then sets up and starts the services
systemctl status bt-server.target
journalctl -u bt-server-container -f      # the executor's and the gateway's output
sudo systemctl restart bt-server.target
sudo systemctl stop bt-server.target      # returns at once; the container stops within 30 s
```

| Unit | What |
|---|---|
| `bt-server-container.service` | `docker run ... bt-server:latest`: the executor `/<namespace>/bt_executor` and the gateway on port 80 of the LAN address. Started again 5 s after it ends; stopped with Docker, and started again whenever Docker starts. To keep the server off, disable it (see Removing it). |
| `bt-server-name.service` | On the host: publishes `bt.<hostname>.local` and `bt-<hostname>.local` for the gateway's address. |
| `bt-server.target` | Both. Started at boot. |

| Folder | What |
|---|---|
| `/etc/bt_server/` | The settings (`gateway.toml`, `executor.yaml`, `namespace`); the container sees them, read-only, as `/opt/bt_server/config` and at the same path. `--reconfigure` rewrites them. |
| `/var/lib/bt_server/` | Home of the system user `btserver`: trees, goal journal, the gateway's state, ROS's logs. |
| `/var/log/bt_server/` | Tree logs. |
| `/opt/bt_server/bin/bt-publish-name` | The name publisher (on the host). |

* Options: `--namespace NS`, `--address IP`, `--port N` (80), `--domain N`, `--image NAME`,
  `--reconfigure`, `--skip-build`, `--no-start`. Run again without `--namespace` or `--domain`,
  the service keeps the ones it has.
* Before it writes the settings and the units, it looks for the executor's name on the ROS network,
  as `jetson_run.sh` does, and refuses a name in use. It stops the service first only when the
  service runs this very name in this domain (it would be the executor found); a service with
  another name keeps running, so a refused setup leaves the settings, the units and the running
  service as they were (the image may be new: the service uses it from its next restart). With
  `--no-start` nothing is looked for, and a warning says when the new settings differ from what the
  running service has.
* It refuses to run while `./jetson_run.sh` runs, and on a machine with the services of
  `setup_system_service.sh`.
* At the end it checks the units, the container, the page, the executor on the ROS network, and
  that both names resolve to this gateway and are accepted by it.
* **ROS discovery and middleware settings** for the service go into a drop-in, which a new setup
  keeps; files they name belong in `/etc/bt_server/`:

  ```bash
  sudo systemctl edit bt-server-container.service
  #   [Service]
  #   Environment=FASTDDS_DEFAULT_PROFILES_FILE=/etc/bt_server/fastdds.xml
  sudo systemctl restart bt-server.target
  ```

## How the container runs, and why

| Option | Why |
|---|---|
| `--network host` | ROS 2 finds other machines with multicast, and the gateway listens on the LAN address. The container also has the Jetson's host name, which the gateway's `public_names` use. |
| `--ipc host` | Fast DDS uses shared memory between processes that it takes to be on one machine. Measured on orion: without this, two Lyrical containers on the Jetson see each other but never receive each other's data; with it, all combinations work. Any other ROS container on the Jetson needs it too. The price: the container shares the host's `/dev/shm`. |
| `--runtime runc` | Not the NVIDIA runtime: the server needs no GPU, and that runtime would mount JetPack's Ubuntu 22.04 libraries into the Ubuntu 26.04 image. |
| `--init` | `docker-init` is process 1: it passes Docker's SIGTERM on to `run.sh` and reaps processes. |
| one container | The executor and the gateway run in one container, under `run.sh`, as in a terminal: when one of them ends, `run.sh` stops the other (a running tree is stopped as Stop does) and the service starts both again 5 s later. Without a container they are two services that systemd restarts separately (design 14.2), so that a gateway that fails leaves a run alone; the container service does not have that. |
| users, capabilities | `jetson_run.sh`: you, no capabilities (port 8080). The service: the container starts as root with CAP_SETUID, CAP_SETGID, CAP_NET_BIND_SERVICE and CAP_KILL only; `entrypoint.sh` becomes `btserver` at once and keeps CAP_NET_BIND_SERVICE alone, for port 80, as `AmbientCapabilities` does without a container. `docker-init` stays root and needs CAP_KILL to pass the SIGTERM on to `btserver`'s processes (without it, a stop would end the container without the executor stopping the tree). `no-new-privileges` throughout. |
| stopping | One SIGTERM, from `docker stop -t 30` (the service) or the script (`jetson_run.sh`); `--sig-proxy=false` keeps the Docker client from sending a second one, which would make the executor exit without waiting for its cancels. The executor waits up to the run's longest cancel timeout plus 1 s; Docker kills it after 30 s. |
| `-v /etc/localtime:/etc/localtime:ro` | The host's time zone, so the logs' times are local, as without a container. |
| logs | The service's output goes to the journal; Docker's own copy is limited to 3 × 10 MB. |

## The `.local` names

The container has no Avahi, so the names are published from the host: by `jetson_run.sh` while it
runs, and by `bt-server-name.service` (`bt-publish-name`, that is `avahi-publish -a -R`, design
14.3).

* `bt-<hostname>.local` is for Windows, which does not look up three-part `.local` names;
  `bt.<hostname>.local` for macOS and iOS. Linux with Avahi's usual `mdns4_minimal` does not look
  up three-part names either (design 14.3). `http://<hostname>.local:<port>/` and the address work too.
* The gateway sees the Jetson's host name (host network), so the names it accepts are the Jetson's.
* The names and the gateway use the LAN address found at setup, and the scripts warn when it came
  from DHCP. Give the Jetson a DHCP reservation, or run the setup again with `--reconfigure` after
  a change of address.

## ROS on the LAN

* **The executor's namespace is the host name by default** (`/orion/bt_executor`), as on Ubuntu
  26.04: every executor in a domain needs a name of its own, and MOTHER-BRAIN's server has
  `/cell1/bt_executor` (the namespace it was installed with, which it keeps) in domain 0 on the
  same network. Every run script and setup looks for the name first and refuses a name in use.
* **Every gateway lists every executor in its domain.** The Jetson's page shows MOTHER-BRAIN's
  `/cell1/bt_executor` too, and the other way round; trees run on the executor you attach to. A
  browser that has not chosen one attaches to the first in the list, which is always the gateway's
  own configured executor. `tools/smoke_test.py` attaches to the first one too, unless given
  `--executor`.
* Both gateways' ROS nodes are named `/bt_gateway`, so `ros2 node list` warns about two nodes with
  one name. Nothing depends on that name; the executors' interfaces are under their own.
* **ROS 2 Humble on the host:** measured on orion, `std_msgs/String` and `std_msgs/Int32` pass both
  ways between the host's Humble and the container's Lyrical. Humble prints `sequence size exceeds
  remaining buffer` about Lyrical's discovery data. ROS does not support mixing distributions:
  test the actions and services your trees use.
* The `ros2` command in a container on the host network finds a Humble `ros2 daemon` of the host
  and fails (`unknown tag ...`): use `--no-daemon`, as the scripts do.

## Removing it

```bash
sudo systemctl disable --now bt-server.target bt-server-container.service bt-server-name.service
sudo rm /etc/systemd/system/bt-server-container.service /etc/systemd/system/bt-server-name.service \
        /etc/systemd/system/bt-server.target
sudo rm -r /etc/systemd/system/bt-server-container.service.d     # drop-ins, if any
sudo systemctl daemon-reload
sudo docker image rm bt-server:latest bt-server-test:latest; sudo docker builder prune
sudo rm -r /etc/bt_server /opt/bt_server
sudo rm -r /var/lib/bt_server /var/log/bt_server; sudo userdel btserver    # the trees and logs too
```
