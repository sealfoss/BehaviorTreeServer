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
#   --namespace NS    executor namespace for the generated settings (default: the one configured,
#                     else this machine's host name made a ROS name, so that it differs from other
#                     servers' in the same ROS domain)
#   --port N          gateway port for the generated settings (default: 8080; at least 1024, as
#                     run.sh runs as you: the services serve port 80)
#   --skip-apt        do not install system packages (fail if something is missing)
#   --skip-tests      do not install the test tools (pytest, py_trees, Playwright)
#   --reconfigure     rewrite config/gateway.toml and config/executor.yaml even if they exist
#   --deps-only       stop after the system packages and the Python environment (no build)
#   -h, --help        this text
#
# What it installs is listed in docs/INSTALLED.md. It is safe to run again: it skips what is
# already there and rebuilds. A kept config/gateway.toml is not changed by --namespace or --port
# alone (a warning says so): add --reconfigure.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROG="install.sh"
# lib/base.sh now; lib/common.sh after the dependencies (the image's dependency layer has only
# lib/base.sh, see container/Dockerfile)
# shellcheck source=lib/base.sh
source "$REPO/lib/base.sh"

PREFIX="$REPO"
SERVICE=0
NAMESPACE=""
PORT=8080
PORT_GIVEN=0
SKIP_APT=0
SKIP_TESTS=0
RECONFIGURE=0
DEPS_ONLY=0

usage() { sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) need_value "$1" $#; PREFIX="$(realpath -m "$2")"; shift 2 ;;
        --service) SERVICE=1; shift ;;
        --namespace) need_value "$1" $#; NAMESPACE="$2"; shift 2 ;;
        --port) need_value "$1" $#; PORT="$2"; PORT_GIVEN=1; shift 2 ;;
        --skip-apt) SKIP_APT=1; shift ;;
        --skip-tests) SKIP_TESTS=1; shift ;;
        --reconfigure) RECONFIGURE=1; shift ;;
        --deps-only) DEPS_ONLY=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) unknown_option "$1" ;;
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

# --------------------------------------------------------------------------- checks
[ -r /etc/os-release ] || die "cannot tell which system this is (/etc/os-release missing)"
. /etc/os-release
if [ "${ID:-}" != "ubuntu" ] || [ "${VERSION_ID:-}" != "26.04" ]; then
    warn "written for Ubuntu 26.04 (ROS 2 Lyrical); this is ${PRETTY_NAME:-unknown}"
fi
for package in "${PACKAGES[@]}"; do
    [ -f "$REPO/$package/package.xml" ] || die "$REPO/$package/package.xml missing: run this script from the repository"
done
if [ "$SERVICE" = 0 ]; then
    check_port "$PORT"
    if [ "$PORT" -lt "$(unprivileged_port_start)" ]; then
        die "--port $PORT: run.sh runs as you, without the right to use ports below $(unprivileged_port_start); the services (./setup_system_service.sh) serve port 80"
    fi
    if [ -n "$NAMESPACE" ]; then check_namespace "$NAMESPACE"; fi
fi
setup_sudo

# --------------------------------------------------------------------------- system packages
is_installed() { [ "$(dpkg-query -W -f='${Status}' "$1" 2>/dev/null)" = "install ok installed" ]; }

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
    # (no "grep -q" in a pipeline: with pipefail, apt-cache's SIGPIPE would count as a failure)
    if [ -z "$(apt-cache policy python3-aiohttp 2>/dev/null | sed -n '/Candidate/p')" ]; then
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

# rosdep resolves the dependencies declared in the package.xml files (design 14.1) to apt
# packages, which are installed like the ones above (rosdep's own "install" calls sudo itself,
# which an unattended run cannot answer). Everything known is already installed above, so a
# failure here (no network to GitHub) is only reported.
if [ "$SKIP_APT" = 0 ]; then
    say "Checking package.xml dependencies with rosdep"
    if [ ! -e /etc/ros/rosdep/sources.list.d/20-default.list ]; then
        "${SUDO[@]}" rosdep init || warn "rosdep init failed"
    fi
    if rosdep update --rosdistro "$ROS_DISTRO_NAME" >/dev/null 2>&1; then
        paths=()
        for package in "${PACKAGES[@]}"; do paths+=("$REPO/$package"); done
        needed=()
        unresolved=()
        for key in $(rosdep keys --from-paths "${paths[@]}" --ignore-src --rosdistro "$ROS_DISTRO_NAME" 2>/dev/null); do
            if ! resolved="$(rosdep resolve "$key" --rosdistro "$ROS_DISTRO_NAME" 2>/dev/null)"; then
                unresolved+=("$key")
                continue
            fi
            installer=""
            while read -r line; do
                case "$line" in
                    "#"*) installer="${line#\#}" ;;
                    *) [ "$installer" = "apt" ] && for name in $line; do needed+=("$name"); done ;;
                esac
            done <<<"$resolved"
        done
        [ ${#needed[@]} -gt 0 ] && apt_install "${needed[@]}"
        if [ ${#unresolved[@]} -gt 0 ]; then
            warn "rosdep cannot resolve: ${unresolved[*]}"
        fi
    else
        warn "rosdep update failed; dependencies were installed with apt above"
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
        || warn "Playwright's Chromium could not be installed; the page tests will be skipped"
fi
if [ "$DEPS_ONLY" = 1 ]; then
    # (container/Dockerfile installs the dependencies in a layer of their own, which a change
    # to the code does not invalidate)
    say "Done (dependencies only, --deps-only)"
    exit 0
fi
# shellcheck source=lib/common.sh
source "$REPO/lib/common.sh"

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
if [ "$SERVICE" = 0 ]; then
    write_checkout_settings "$CONFIG_DIR" "$DATA_DIR" "$NAMESPACE" "$PORT" "$PORT_GIVEN" "$RECONFIGURE"
fi

say "Done"
echo "  environment: $VENV"
echo "  install:     $INSTALL"
if [ "$SERVICE" = 0 ]; then
    echo "  settings:    $CONFIG_DIR/gateway.toml, $CONFIG_DIR/executor.yaml (executor /$(cat "$CONFIG_DIR/namespace")/bt_executor)"
    echo "  trees:       $DATA_DIR/trees"
    echo "Start it with ./run.sh"
fi
