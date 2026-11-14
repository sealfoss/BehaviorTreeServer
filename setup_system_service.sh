#!/usr/bin/env bash
# Install the Behavior Tree Server as systemd services that start at boot (design 14.2-14.4):
#
#   bt-executor.service     the executor /<namespace>/bt_executor
#   bt-gateway.service      the gateway: the page on port 80 of the LAN address
#   bt-server-name.service  publishes bt.<hostname>.local and bt-<hostname>.local
#   bt-server.target        all three: systemctl status|restart|stop bt-server.target
#
#   ./setup_system_service.sh [options]
#
#   --prefix DIR      where the build goes (default /opt/bt_server)
#   --namespace NS    executor namespace (default cell1)
#   --address IP      LAN address the gateway listens on (default: the address of the default route)
#   --port N          gateway port (default 80)
#   --domain N        ROS_DOMAIN_ID of both services (default 0)
#   --reconfigure     rewrite /etc/bt_server/*.toml|yaml even if they exist
#   --skip-build      use the existing build in the prefix
#   --no-start        install and enable, but do not start now
#
# Run it as a user who may use sudo. It builds as that user into the prefix (design 14.1),
# creates the system user btserver (home /var/lib/bt_server), writes /etc/bt_server/ and the
# units, enables them, starts them, and checks that the page answers.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="/opt/bt_server"
NAMESPACE="cell1"
ADDRESS=""
PORT=80
DOMAIN=0
RECONFIGURE=0
SKIP_BUILD=0
START=1
SERVICE_USER="btserver"
STATE_DIR="/var/lib/bt_server"
LOG_DIR="/var/log/bt_server"
CONF="/etc/bt_server"
ROS_SETUP="/opt/ros/lyrical/setup.bash"
UNIT_DIR="/etc/systemd/system"

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) PREFIX="$(realpath -m "$2")"; shift 2 ;;
        --namespace) NAMESPACE="$2"; shift 2 ;;
        --address) ADDRESS="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --domain) DOMAIN="$2"; shift 2 ;;
        --reconfigure) RECONFIGURE=1; shift ;;
        --skip-build) SKIP_BUILD=1; shift ;;
        --no-start) START=0; shift ;;
        -h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "setup_system_service.sh: unknown option $1" >&2; exit 2 ;;
    esac
done

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'setup_system_service.sh: %s\n' "$*" >&2; exit 1; }

if [ "$(id -u)" = 0 ]; then
    # Run as root (for example with sudo), the build is made by root too.
    SUDO=()
    BUILD_USER="root"
else
    # sudo prompts on the terminal; for an unattended run, point SUDO_ASKPASS at a program that
    # prints the password and it is used instead (sudo -A).
    if [ -n "${SUDO_ASKPASS:-}" ]; then SUDO=(sudo -A); else SUDO=(sudo); fi
    BUILD_USER="$(id -un)"
    "${SUDO[@]}" -v || die "this script needs sudo"
fi
command -v systemctl >/dev/null || die "systemd is required"
[[ "$NAMESPACE" =~ ^[A-Za-z][A-Za-z0-9_]*$ ]] || die "the namespace must be a ROS name part: letters, digits, _"

if [ -z "$ADDRESS" ]; then
    ADDRESS="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p')"
    [ -n "$ADDRESS" ] || die "could not find the LAN address; give it with --address"
fi
HOST_SHORT="$(hostname -s)"

# ---------------------------------------------------------------------------- build
if [ "$SKIP_BUILD" = 0 ]; then
    say "Building into $PREFIX (as $BUILD_USER)"
    "${SUDO[@]}" mkdir -p "$PREFIX"
    "${SUDO[@]}" chown "$BUILD_USER": "$PREFIX"
    "$REPO/install.sh" --prefix "$PREFIX" --service
fi
[ -r "$PREFIX/install/setup.bash" ] || die "no build in $PREFIX/install"
[ -x "$PREFIX/venv/bin/python" ] || die "no Python environment in $PREFIX/venv"
[ -x "$PREFIX/bin/bt-publish-name" ] || die "$PREFIX/bin/bt-publish-name missing"
"${SUDO[@]}" chmod -R a+rX "$PREFIX"

# ---------------------------------------------------------------------------- user and folders
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    say "Creating the system user $SERVICE_USER"
    "${SUDO[@]}" useradd --system --create-home --home-dir "$STATE_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
"${SUDO[@]}" mkdir -p "$STATE_DIR/trees" "$LOG_DIR"
if [ -d "$REPO/examples/trees" ] && [ -z "$("${SUDO[@]}" ls -A "$STATE_DIR/trees")" ]; then
    say "Copying the example trees into $STATE_DIR/trees"
    "${SUDO[@]}" cp -r "$REPO/examples/trees/." "$STATE_DIR/trees/"
fi
"${SUDO[@]}" chown -R "$SERVICE_USER": "$STATE_DIR" "$LOG_DIR"

# ---------------------------------------------------------------------------- settings
"${SUDO[@]}" mkdir -p "$CONF"
"${SUDO[@]}" chmod 755 "$CONF"
if [ ! -f "$CONF/gateway.toml" ] || [ "$RECONFIGURE" = 1 ]; then
    say "Writing $CONF/gateway.toml"
    "${SUDO[@]}" tee "$CONF/gateway.toml" >/dev/null <<EOF
# Settings of the gateway (design 9.1), written by setup_system_service.sh.
link = "ros"
http_host = "$ADDRESS"                     # the LAN address, not 0.0.0.0
http_port = $PORT
public_names = ["bt.{hostname}.local", "bt-{hostname}.local", "{hostname}.local"]
executors = ["/$NAMESPACE/bt_executor"]
allowed_networks = []
trees_dir = "$STATE_DIR/trees"
EOF
fi
if [ ! -f "$CONF/executor.yaml" ] || [ "$RECONFIGURE" = 1 ]; then
    say "Writing $CONF/executor.yaml"
    "${SUDO[@]}" tee "$CONF/executor.yaml" >/dev/null <<EOF
# Parameters of the executor (design 8.1), written by setup_system_service.sh.
/**:
  ros__parameters:
    leaf_modules: ["bt_server_core.demo_leaves"]
    log_dir: $LOG_DIR
    journal_file: $STATE_DIR/goals.jsonl
EOF
fi
"${SUDO[@]}" chmod 644 "$CONF/gateway.toml" "$CONF/executor.yaml"

# ---------------------------------------------------------------------------- units
say "Writing the systemd units into $UNIT_DIR"
for template in bt-executor.service bt-gateway.service bt-server-name.service bt-server.target; do
    sed -e "s|@PREFIX@|$PREFIX|g" -e "s|@CONF@|$CONF|g" -e "s|@NAMESPACE@|$NAMESPACE|g" \
        -e "s|@USER@|$SERVICE_USER|g" -e "s|@DOMAIN@|$DOMAIN|g" -e "s|@ROS_SETUP@|$ROS_SETUP|g" \
        -e "s|@ADDRESS@|$ADDRESS|g" \
        "$REPO/bt_server_bringup/systemd/$template.in" | "${SUDO[@]}" tee "$UNIT_DIR/$template" >/dev/null
    "${SUDO[@]}" chmod 644 "$UNIT_DIR/$template"
done
"${SUDO[@]}" systemctl daemon-reload

# ---------------------------------------------------------------------------- firewall
if command -v ufw >/dev/null && [ -n "$("${SUDO[@]}" ufw status 2>/dev/null | sed -n '/Status: active/p')" ]; then
    interface="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p')"
    say "ufw is active: allowing port $PORT/tcp and mDNS (5353/udp) on $interface"
    "${SUDO[@]}" ufw allow in on "$interface" to any port "$PORT" proto tcp
    "${SUDO[@]}" ufw allow in on "$interface" to any port 5353 proto udp
    echo "Note: ROS 2 traffic must be allowed too (design 14.4), for example: sudo ufw allow in on <robot interface>"
fi

# ---------------------------------------------------------------------------- enable and start
UNITS=(bt-executor.service bt-gateway.service bt-server-name.service bt-server.target)
say "Enabling ${UNITS[*]}"
"${SUDO[@]}" systemctl enable "${UNITS[@]}"
if [ "$START" = 0 ]; then
    echo "Enabled; not started (--no-start). Start with: sudo systemctl start bt-server.target"
    exit 0
fi
say "Starting the services"
"${SUDO[@]}" systemctl restart bt-executor.service bt-gateway.service bt-server-name.service
"${SUDO[@]}" systemctl start bt-server.target

# ---------------------------------------------------------------------------- check
say "Checking"
failed=0
for unit in "${UNITS[@]}"; do
    printf '  %-26s enabled=%-8s active=%s\n' "$unit" "$(systemctl is-enabled "$unit" 2>/dev/null || true)" \
        "$(systemctl is-active "$unit" 2>/dev/null || true)"
done
page=""
for _ in $(seq 1 30); do
    if page="$(curl -fsS --max-time 2 "http://$ADDRESS:$PORT/" 2>/dev/null)"; then break; fi
    page=""
    sleep 1
done
if [ -n "$page" ] && grep -qi "<html" <<<"$page"; then
    echo "  the page answers on http://$ADDRESS:$PORT/"
else
    echo "  the page does not answer on http://$ADDRESS:$PORT/ (see: journalctl -u bt-gateway)"
    failed=1
fi
for unit in bt-executor.service bt-gateway.service bt-server-name.service; do
    systemctl is-active --quiet "$unit" || { echo "  $unit is not active (see: journalctl -u $unit)"; failed=1; }
    systemctl is-enabled --quiet "$unit" || { echo "  $unit is not enabled"; failed=1; }
done
for name in "bt.$HOST_SHORT.local" "bt-$HOST_SHORT.local"; do
    if resolved="$(timeout 5 avahi-resolve -4 -n "$name" 2>/dev/null)" && [ -n "$resolved" ]; then
        echo "  $name -> ${resolved##*[[:space:]]}"
    else
        echo "  $name is not published yet (see: journalctl -u bt-server-name)"
        failed=1
    fi
done
if [ "$failed" = 0 ]; then
    suffix=""
    [ "$PORT" = 80 ] || suffix=":$PORT"
    echo
    echo "The Behavior Tree Server runs and starts at boot. Open one of:"
    echo "  http://bt-${HOST_SHORT,,}.local$suffix/   (Windows, Linux)"
    echo "  http://bt.${HOST_SHORT,,}.local$suffix/   (macOS, iOS)"
    echo "  http://$ADDRESS$suffix/"
fi
exit "$failed"
