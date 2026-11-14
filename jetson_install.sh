#!/usr/bin/env bash
# Install the Behavior Tree Server on a Jetson with JetPack 6 (Ubuntu 22.04), in a container.
# ROS 2 Lyrical needs Ubuntu 26.04, so the server runs in an Ubuntu 26.04 image that this script
# builds from container/Dockerfile with the project's own install.sh. On the host it installs
# only what the container cannot provide, and it writes settings for this machine (docs/JETSON.md).
#
#   ./jetson_install.sh [options]      then ./jetson_run.sh, or ./jetson_setup_system_service.sh
#
# Options:
#   --image NAME      the image to build (default: bt-server:latest)
#   --namespace NS    executor namespace for the generated settings (default: this machine's host
#                     name made a ROS name, such as orion, so that it differs from other servers')
#   --port N          gateway port for the generated settings (default: 8080)
#   --skip-apt        do not install host packages (fail if something is missing)
#   --no-cache        build the image from scratch, from the newest Ubuntu 26.04 image
#   --test            also build bt-server-test:latest and run the test suite in it
#   --reconfigure     rewrite config/gateway.toml and config/executor.yaml even if they exist
#   -h, --help        this text
#
# Host packages, when missing: Docker (docker.io), avahi-daemon, avahi-utils, iproute2, curl.
# The container has no Avahi, so bt.<hostname>.local and bt-<hostname>.local are published from
# the host: by jetson_run.sh while it runs, and by the service bt-server-name.service (design 14.3;
# the second name because Windows 11 does not look up three-part .local names). The settings for
# ./jetson_run.sh go into config/ and its trees and logs into .data/, as install.sh does; the
# container sees .data/ at the same path. Safe to run again: it skips what is there and rebuilds.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROG="jetson_install.sh"
# shellcheck source=lib/common.sh
source "$REPO/lib/common.sh"

IMAGE="$DEFAULT_IMAGE"
TEST_IMAGE="$DEFAULT_TEST_IMAGE"
NAMESPACE=""
PORT=8080
PORT_GIVEN=0
SKIP_APT=0
NO_CACHE=0
RUN_TESTS=0
RECONFIGURE=0
CONFIG_DIR="$REPO/config"
DATA_DIR="$REPO/.data"

usage() { sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        --image) need_value "$1" $#; IMAGE="$2"; shift 2 ;;
        --namespace) need_value "$1" $#; NAMESPACE="$2"; shift 2 ;;
        --port) need_value "$1" $#; PORT="$2"; PORT_GIVEN=1; shift 2 ;;
        --skip-apt) SKIP_APT=1; shift ;;
        --no-cache) NO_CACHE=1; shift ;;
        --test) RUN_TESTS=1; shift ;;
        --reconfigure) RECONFIGURE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) unknown_option "$1" ;;
    esac
done

# --------------------------------------------------------------------------- checks
[ -r /etc/os-release ] || die "cannot tell which system this is (/etc/os-release missing)"
. /etc/os-release
if [ "${ID:-}" != "ubuntu" ] || [ "${VERSION_ID:-}" != "22.04" ]; then
    warn "written for a Jetson with JetPack 6 (Ubuntu 22.04); this is ${PRETTY_NAME:-unknown}"
fi
if [ -r /etc/nv_tegra_release ]; then
    # "# R36 (release), REVISION: 5.0, ..." is L4T R36.5.0 (JetPack 6.2)
    l4t="$(sed -n 's/^# R\([0-9]*\) (release), REVISION: \([0-9.]*\).*/R\1.\2/p' /etc/nv_tegra_release | head -1)"
    echo "$PROG: Jetson Linux (L4T) ${l4t:-?} on $(uname -m)"
fi
for package in "${PACKAGES[@]}"; do
    [ -f "$REPO/$package/package.xml" ] || die "$REPO/$package/package.xml missing: run this script from the repository"
done
[ -f "$REPO/container/Dockerfile" ] || die "$REPO/container/Dockerfile missing"
check_port "$PORT"
if [ "$PORT" -lt "$(unprivileged_port_start)" ]; then
    die "--port $PORT: jetson_run.sh's container runs as you, without the right to use ports below $(unprivileged_port_start); the service (./jetson_setup_system_service.sh) serves port 80"
fi
if [ -n "$NAMESPACE" ]; then check_namespace "$NAMESPACE"; fi
setup_sudo

# --------------------------------------------------------------------------- host packages
is_installed() { [ "$(dpkg-query -W -f='${Status}' "$1" 2>/dev/null)" = "install ok installed" ]; }

APT_UPDATED=0
apt_install() {
    local missing=()
    for package in "$@"; do
        is_installed "$package" || missing+=("$package")
    done
    [ ${#missing[@]} -eq 0 ] && return 0
    [ "$SKIP_APT" = 1 ] && die "missing host packages (and --skip-apt given): ${missing[*]}"
    if [ "$APT_UPDATED" = 0 ]; then
        "${SUDO[@]}" apt-get update
        APT_UPDATED=1
    fi
    say "Installing host packages: ${missing[*]}"
    "${SUDO[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}"
}

say "Checking host packages"
apt_install avahi-daemon avahi-utils iproute2 curl
if ! command -v docker >/dev/null; then
    # JetPack's own Docker (docker-ce, with the NVIDIA runtime) is used when it is there.
    apt_install docker.io
fi
for unit in docker.service avahi-daemon.service; do
    if ! systemctl is-enabled --quiet "$unit" 2>/dev/null || ! systemctl is-active --quiet "$unit" 2>/dev/null; then
        [ "$SKIP_APT" = 1 ] && die "$unit is not enabled and running (and --skip-apt given)"
        say "Enabling and starting $unit"
        "${SUDO[@]}" systemctl enable --now "$unit"
    fi
done
setup_docker
echo "Docker $("${DOCKER[@]}" version --format '{{.Server.Version}}' 2>/dev/null || echo '?'), Avahi $(avahi-daemon --version 2>/dev/null | sed 's/^avahi-daemon //')"

# --------------------------------------------------------------------------- image
# The site's own apt packages for the image (the robots' interfaces, for example), one per line in
# config/apt-packages; a commented example is written the first time.
APT_LIST="$CONFIG_DIR/apt-packages"
if [ ! -e "$APT_LIST" ]; then
    mkdir -p "$CONFIG_DIR"
    cat > "$APT_LIST" <<'EOF'
# Extra apt packages for the image bt-server:latest, one per line, such as the interfaces of the
# robots' actions and services, which the executor needs to call them:
#   ros-lyrical-nav2-msgs
# Then run ./jetson_install.sh again (and sudo systemctl restart bt-server.target for the service).
EOF
fi
EXTRA_APT_PACKAGES="$(sed -e 's/#.*//' "$APT_LIST" | tr -s ' \t\r\n' ' ' | sed -e 's/^ //' -e 's/ $//')"
for package in $EXTRA_APT_PACKAGES; do
    [[ "$package" =~ ^[a-z0-9][a-z0-9+.-]+(=[A-Za-z0-9.+:~-]+)?$ ]] || die "$APT_LIST: '$package' is not an apt package name"
done

build_image() {  # target, image name
    local options=(-f "$REPO/container/Dockerfile" --target "$1" -t "$2")
    [ "$NO_CACHE" = 1 ] && options+=(--no-cache --pull)
    say "Building the image $2 (target $1; the first build downloads Ubuntu 26.04 and ROS 2 Lyrical)"
    if [ "$1" = server ]; then
        options+=(--build-arg "EXTRA_APT_PACKAGES=$EXTRA_APT_PACKAGES")
        [ -z "$EXTRA_APT_PACKAGES" ] || echo "  with the extra packages of $APT_LIST: $EXTRA_APT_PACKAGES"
    fi
    "${DOCKER[@]}" build "${options[@]}" "$REPO"
}
# The build an image name had before, removed once a new one has the name (if nothing uses it):
# that build and no other image.
image_id() { "${DOCKER[@]}" image inspect --format '{{.Id}}' "$1" 2>/dev/null || true; }
remove_replaced() {  # image name, its id before the build
    if [ -n "$2" ] && [ "$(image_id "$1")" != "$2" ]; then
        "${DOCKER[@]}" image rm "$2" >/dev/null 2>&1 || true
    fi
}
previous="$(image_id "$IMAGE")"
build_image server "$IMAGE"
remove_replaced "$IMAGE" "$previous"

say "Checking the image on this machine"
"${DOCKER[@]}" run --rm --init --runtime runc --network none --cap-drop ALL --security-opt no-new-privileges \
    --user 65534:65534 -e HOME=/tmp --entrypoint /bin/bash "$IMAGE" -c "
        source /opt/ros/$ROS_DISTRO_NAME/setup.bash && source /opt/bt_server/install/setup.bash &&
        exec /opt/bt_server/venv/bin/python -c '
import platform, rclpy, bt_server_executor, bt_server_gateway
rclpy.init()
node = rclpy.create_node(\"bt_install_check\")
node.destroy_node()
rclpy.shutdown()
print(\"  ROS 2 \" + \"$ROS_DISTRO_NAME\" + \", Python \" + platform.python_version() + \" on \" + platform.machine() + \": rclpy, the executor and the gateway load\")
'" || die "the image does not work on this machine"

# --------------------------------------------------------------------------- settings for jetson_run.sh
# Written as the user who runs this script (also when it runs under sudo), because jetson_run.sh
# runs the container as that user.
write_checkout_settings "$CONFIG_DIR" "$DATA_DIR" "$NAMESPACE" "$PORT" "$PORT_GIVEN" "$RECONFIGURE"

# --------------------------------------------------------------------------- test suite
if [ "$RUN_TESTS" = 1 ]; then
    previous="$(image_id "$TEST_IMAGE")"
    build_image test "$TEST_IMAGE"
    remove_replaced "$TEST_IMAGE" "$previous"
    say "Running the test suite in $TEST_IMAGE"
    # On Docker's own network, in a ROS domain of its own and with discovery kept to the
    # container, so that the tests neither see nor disturb the ROS programs on the LAN.
    "${DOCKER[@]}" run --rm --init --runtime runc --shm-size 1g \
        -e ROS_DOMAIN_ID=71 -e ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST "$TEST_IMAGE" \
        || die "tests failed (see above)"
fi

say "Done"
echo "  image:     $IMAGE ($("${DOCKER[@]}" image inspect --format '{{.Os}}/{{.Architecture}}' "$IMAGE"))"
echo "  settings:  $CONFIG_DIR/gateway.toml, $CONFIG_DIR/executor.yaml (executor /$(cat "$CONFIG_DIR/namespace")/bt_executor)"
echo "  trees:     $DATA_DIR/trees"
echo "Start it with ./jetson_run.sh, or install it as a service with ./jetson_setup_system_service.sh"
