#!/usr/bin/env bash
# Install the Behavior Tree Server on this machine (Ubuntu 26.04, ROS 2 Lyrical): system
# packages that are missing, a Python environment, the colcon build, and settings for this
# machine.
#
#   ./install.sh                       development install in this checkout:
#                                        .venv/  build/  install/  config/  .data/
#   ./install.sh --prefix /opt/bt_server --service
#                                      install for the systemd services (setup_system_service.sh
#                                        calls this): <prefix>/venv, <prefix>/build,
#                                        <prefix>/install, <prefix>/bin; no test tools, no config
#
# Options:
#   --prefix DIR      where the environment, the build and the install go (default: this checkout)
#   --service         an install for the services (see above)
#   --namespace NS    executor namespace for the generated settings (default: cell1)
#   --port N          gateway port for the generated settings (default: 8080)
#   --skip-apt        do not install system packages (fail if something is missing)
#   --skip-tests      do not install the test tools (pytest, py_trees, Playwright)
#   --reconfigure     rewrite config/gateway.toml and config/executor.yaml even if they exist
#   -h, --help        this text
#
# What it installs is listed in docs/INSTALLED.md. It is safe to run again: it skips what is
# already there and rebuilds.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="$REPO"
SERVICE=0
NAMESPACE="cell1"
PORT=8080
SKIP_APT=0
SKIP_TESTS=0
RECONFIGURE=0
ROS_DISTRO_NAME="lyrical"
ROS_SETUP="/opt/ros/${ROS_DISTRO_NAME}/setup.bash"
PACKAGES=(bt_server_interfaces bt_server_core bt_server_executor bt_server_gateway bt_server_bringup)

usage() { sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) PREFIX="$(realpath -m "$2")"; shift 2 ;;
        --service) SERVICE=1; shift ;;
        --namespace) NAMESPACE="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --skip-apt) SKIP_APT=1; shift ;;
        --skip-tests) SKIP_TESTS=1; shift ;;
        --reconfigure) RECONFIGURE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "install.sh: unknown option $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ "$SERVICE" = 1 ]; then
    VENV="$PREFIX/venv"
    SKIP_TESTS=1
else
    VENV="$PREFIX/.venv"
fi
[ "$PREFIX" = "$REPO" ] || VENV="$PREFIX/venv"
BUILD="$PREFIX/build"
INSTALL="$PREFIX/install"
CONFIG_DIR="$PREFIX/config"
DATA_DIR="$PREFIX/.data"

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------- checks
[ -r /etc/os-release ] || die "cannot tell which system this is (/etc/os-release missing)"
. /etc/os-release
if [ "${ID:-}" != "ubuntu" ] || [ "${VERSION_ID:-}" != "26.04" ]; then
    echo "install.sh: warning: written for Ubuntu 26.04 (ROS 2 Lyrical); this is ${PRETTY_NAME:-unknown}" >&2
fi
for package in "${PACKAGES[@]}"; do
    [ -f "$REPO/$package/package.xml" ] || die "$REPO/$package/package.xml missing: run this script from the repository"
done

if [ "$(id -u)" = 0 ]; then SUDO=(); else SUDO=(sudo); fi

# --------------------------------------------------------------------------- system packages
is_installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"; }

apt_install() {
    local missing=()
    for package in "$@"; do
        is_installed "$package" || missing+=("$package")
    done
    [ ${#missing[@]} -eq 0 ] && return 0
    [ "$SKIP_APT" = 1 ] && die "missing system packages (and --skip-apt given): ${missing[*]}"
    say "Installing system packages: ${missing[*]}"
    "${SUDO[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}"
}

add_ros_repository() {
    is_installed ros2-apt-source && return 0
    [ "$SKIP_APT" = 1 ] && die "the ROS 2 apt repository is not set up (and --skip-apt given)"
    say "Adding the ROS 2 apt repository (ros2-apt-source)"
    apt_install curl ca-certificates software-properties-common
    if ! grep -rqs "universe" /etc/apt/sources.list /etc/apt/sources.list.d/; then
        "${SUDO[@]}" add-apt-repository -y universe
    fi
    local version codename deb
    version="$(curl -fsSL https://api.github.com/repos/ros-infrastructure/ros-apt-source/releases/latest \
               | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p')"
    [ -n "$version" ] || die "could not find the latest ros-apt-source release"
    codename="${UBUNTU_CODENAME:-${VERSION_CODENAME}}"
    deb="$(mktemp --suffix=.deb)"
    curl -fsSL -o "$deb" \
        "https://github.com/ros-infrastructure/ros-apt-source/releases/download/${version}/ros2-apt-source_${version}.${codename}_all.deb"
    "${SUDO[@]}" dpkg -i "$deb"
    rm -f "$deb"
    "${SUDO[@]}" apt-get update
}

if [ "$SKIP_APT" = 0 ]; then
    say "Checking system packages"
    if ! apt-cache policy python3-aiohttp 2>/dev/null | grep -q Candidate; then
        "${SUDO[@]}" apt-get update
    fi
fi
apt_install curl ca-certificates git python3-venv python3-pip python3-aiohttp avahi-utils build-essential cmake
if [ ! -r "$ROS_SETUP" ] || ! is_installed "ros-${ROS_DISTRO_NAME}-ros-base"; then
    add_ros_repository
    apt_install "ros-${ROS_DISTRO_NAME}-ros-base" ros-dev-tools
fi
apt_install ros-dev-tools
if [ "$SKIP_TESTS" = 0 ]; then
    # Interfaces the tests' scripted action servers and services use.
    apt_install "ros-${ROS_DISTRO_NAME}-example-interfaces" "ros-${ROS_DISTRO_NAME}-nav2-msgs"
fi
[ -r "$ROS_SETUP" ] || die "$ROS_SETUP missing after the installation"

# rosdep resolves the dependencies declared in the package.xml files (design 14.1). Everything
# it would install is already installed above, so a failure here (no network to GitHub) is
# only reported.
if [ "$SKIP_APT" = 0 ]; then
    say "Checking package.xml dependencies with rosdep"
    if [ ! -e /etc/ros/rosdep/sources.list.d/20-default.list ]; then
        "${SUDO[@]}" rosdep init || echo "install.sh: warning: rosdep init failed" >&2
    fi
    if rosdep update --rosdistro "$ROS_DISTRO_NAME" >/dev/null 2>&1; then
        paths=()
        for package in "${PACKAGES[@]}"; do paths+=("$REPO/$package"); done
        rosdep install --from-paths "${paths[@]}" --ignore-src -y --rosdistro "$ROS_DISTRO_NAME" \
            || echo "install.sh: warning: rosdep install reported problems (see above)" >&2
    else
        echo "install.sh: warning: rosdep update failed; dependencies were installed with apt above" >&2
    fi
fi

# --------------------------------------------------------------------------- Python environment
# rclpy is built for Ubuntu's own Python, so the environment uses it and sees its packages
# (--system-site-packages); only tools that apt does not provide go into it (design 14.1).
if [ ! -x "$VENV/bin/python" ]; then
    say "Creating the Python environment $VENV"
    mkdir -p "$(dirname "$VENV")"
    python3 -m venv --system-site-packages "$VENV"
fi
if [ "$SKIP_TESTS" = 0 ]; then
    say "Installing the test tools into $VENV"
    "$VENV/bin/python" -m pip install --quiet pytest pytest-asyncio pytest-aiohttp py_trees playwright pytest-playwright
    "$VENV/bin/python" -m playwright install chromium >/dev/null \
        || echo "install.sh: warning: Playwright's Chromium could not be installed; the page tests will be skipped" >&2
fi

# --------------------------------------------------------------------------- build
say "Building ${PACKAGES[*]}"
mkdir -p "$BUILD" "$INSTALL"
(
    set +u
    # shellcheck disable=SC1090
    source "$ROS_SETUP"
    # shellcheck disable=SC1091
    source "$VENV/bin/activate"
    set -u
    paths=()
    for package in "${PACKAGES[@]}"; do paths+=("$REPO/$package"); done
    cd "$PREFIX"
    colcon --log-base "$PREFIX/log" build --base-paths "${paths[@]}" \
        --build-base "$BUILD" --install-base "$INSTALL" \
        --cmake-args -DBUILD_TESTING=OFF --event-handlers console_cohesion+
)
[ -f "$INSTALL/bt_server_gateway/share/bt_server_gateway/package.xml" ] || die "the build did not install bt_server_gateway"

mkdir -p "$PREFIX/bin"
install -m 0755 "$REPO/bt_server_bringup/systemd/bt-publish-name" "$PREFIX/bin/bt-publish-name"

# --------------------------------------------------------------------------- settings for this machine
lan_address() { ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p'; }

if [ "$SERVICE" = 0 ]; then
    ADDRESS="$(lan_address)"
    [ -n "$ADDRESS" ] || ADDRESS="127.0.0.1"
    mkdir -p "$CONFIG_DIR" "$DATA_DIR/trees" "$DATA_DIR/log"
    if [ ! -f "$CONFIG_DIR/gateway.toml" ] || [ "$RECONFIGURE" = 1 ]; then
        say "Writing $CONFIG_DIR/gateway.toml"
        cat > "$CONFIG_DIR/gateway.toml" <<EOF
# Settings of the gateway (design 9.1), written by install.sh for $(hostname -s).
link = "ros"
http_host = "$ADDRESS"
http_port = $PORT
public_names = ["bt.{hostname}.local", "bt-{hostname}.local", "{hostname}.local"]
executors = ["/$NAMESPACE/bt_executor"]
allowed_networks = []
trees_dir = "$DATA_DIR/trees"
EOF
    fi
    if [ ! -f "$CONFIG_DIR/executor.yaml" ] || [ "$RECONFIGURE" = 1 ]; then
        say "Writing $CONFIG_DIR/executor.yaml"
        cat > "$CONFIG_DIR/executor.yaml" <<EOF
# Parameters of the executor (design 8.1), written by install.sh for $(hostname -s).
/**:
  ros__parameters:
    leaf_modules: ["bt_server_core.demo_leaves"]
    log_dir: "$DATA_DIR/log"
    journal_file: "$DATA_DIR/goals.jsonl"
EOF
    fi
    echo "$NAMESPACE" > "$CONFIG_DIR/namespace"
    if [ -d "$REPO/examples/trees" ] && [ -z "$(ls -A "$DATA_DIR/trees" 2>/dev/null)" ]; then
        cp -r "$REPO/examples/trees/." "$DATA_DIR/trees/"
    fi
fi

say "Done"
echo "  environment: $VENV"
echo "  install:     $INSTALL"
if [ "$SERVICE" = 0 ]; then
    echo "  settings:    $CONFIG_DIR/gateway.toml, $CONFIG_DIR/executor.yaml"
    echo "  trees:       $DATA_DIR/trees"
    echo "Start it with ./run.sh"
fi
