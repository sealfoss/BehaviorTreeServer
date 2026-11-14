#!/usr/bin/env bash
# Install the Behavior Tree Server on a Jetson as systemd services that start at boot, with the
# server in the container image of ./jetson_install.sh (design 14.2-14.4, docs/JETSON.md):
#
#   bt-server-container.service  the container: the executor /<namespace>/bt_executor and the
#                                gateway, the page on port 80 of the LAN address
#   bt-server-name.service       publishes bt.<hostname>.local and bt-<hostname>.local from the
#                                host (the container has no Avahi)
#   bt-server.target             both: systemctl status|restart|stop bt-server.target
#
#   ./jetson_setup_system_service.sh [options]
#
#   --namespace NS    executor namespace (default: the service's, else the host name made a ROS name)
#   --address IP      LAN address the gateway listens on (default: the address of the default route)
#   --port N          gateway port (default 80)
#   --domain N        ROS_DOMAIN_ID (default: the service's, else 0)
#   --image NAME      the image (default: the service's, else bt-server:latest)
#   --reconfigure     rewrite /etc/bt_server/*.toml|yaml even if they exist
#   --skip-build      use the existing image; do not run ./jetson_install.sh first
#   --no-start        install and enable, but do not start now
#
# Run it as a user who may use sudo. Unless --skip-build it runs ./jetson_install.sh, which
# builds the image. It creates the system user btserver (home /var/lib/bt_server), as whom the
# container's processes run, writes /etc/bt_server/ and the units, enables them, starts them, and
# checks that the executor is on the ROS network, the page answers and the names resolve.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROG="jetson_setup_system_service.sh"
# shellcheck source=container/common.sh
source "$REPO/container/common.sh"

NAMESPACE=""
ADDRESS=""
PORT=80
DOMAIN=""
IMAGE=""
RECONFIGURE=0
SKIP_BUILD=0
START=1
SERVICE_USER="btserver"
STATE_DIR="/var/lib/bt_server"
LOG_DIR="/var/log/bt_server"
CONF="/etc/bt_server"
PREFIX="/opt/bt_server"     # on the host: bin/bt-publish-name
UNIT_DIR="/etc/systemd/system"
UNITS=("$SERVICE_UNIT" bt-server-name.service bt-server.target)

while [ $# -gt 0 ]; do
    case "$1" in
        --namespace) need_value "$1" $#; NAMESPACE="$2"; shift 2 ;;
        --address) need_value "$1" $#; ADDRESS="$2"; shift 2 ;;
        --port) need_value "$1" $#; PORT="$2"; shift 2 ;;
        --domain) need_value "$1" $#; DOMAIN="$2"; shift 2 ;;
        --image) need_value "$1" $#; IMAGE="$2"; shift 2 ;;
        --reconfigure) RECONFIGURE=1; shift ;;
        --skip-build) SKIP_BUILD=1; shift ;;
        --no-start) START=0; shift ;;
        -h|--help) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "$PROG: unknown option $1" >&2; exit 2 ;;
    esac
done

setup_sudo
if [ "$(id -u)" != 0 ]; then
    "${SUDO[@]}" -v || die "this script needs sudo"
fi
command -v systemctl >/dev/null || die "systemd is required"
# Run again without --namespace or --domain, the service keeps the ones it has.
if [ -z "$NAMESPACE" ] && [ -r "$CONF/namespace" ]; then
    NAMESPACE="$(head -1 "$CONF/namespace")"
fi
[ -n "$NAMESPACE" ] || NAMESPACE="$(default_namespace)"
if [ -z "$DOMAIN" ]; then
    if [ -e "$UNIT_DIR/$SERVICE_UNIT" ]; then
        DOMAIN="$(sed -n 's/^Environment=ROS_DOMAIN_ID=\([0-9]*\)$/\1/p' "$UNIT_DIR/$SERVICE_UNIT" | head -1)"
    fi
    DOMAIN="${DOMAIN:-0}"
fi
if [ -z "$IMAGE" ]; then
    if [ -e "$UNIT_DIR/$SERVICE_UNIT" ]; then
        IMAGE="$(sed -n 's/^Environment=BT_IMAGE=\([^ ]*\)$/\1/p' "$UNIT_DIR/$SERVICE_UNIT" | head -1)"
    fi
    IMAGE="${IMAGE:-$DEFAULT_IMAGE}"
fi
valid_namespace "$NAMESPACE" || die "the namespace must be a ROS name part (letters, digits, single _), not '$NAMESPACE'"
valid_domain "$DOMAIN" || die "--domain must be a number from 0 to 232, not '$DOMAIN'"
valid_port "$PORT" || die "--port must be a port number, not '$PORT'"
if [ -z "$ADDRESS" ]; then
    ADDRESS="$(lan_address)"
    [ -n "$ADDRESS" ] || die "could not find the LAN address; give it with --address"
fi
HOST_SHORT="$(short_hostname)"
for unit in bt-executor.service bt-gateway.service; do
    if [ -e "$UNIT_DIR/$unit" ]; then
        die "$UNIT_DIR/$unit is there: the services of setup_system_service.sh (without a container) are installed on this machine; remove them first"
    fi
done

# ---------------------------------------------------------------------------- image
if [ "$SKIP_BUILD" = 0 ]; then
    if [ "$IMAGE" = "$DEFAULT_IMAGE" ]; then
        "$REPO/jetson_install.sh"
    else
        # An image of the site's own (FROM bt-server:latest) is never built over: its base is.
        "$REPO/jetson_install.sh"
        warn "$IMAGE is your own image and was not rebuilt: rebuild it from the new $DEFAULT_IMAGE, then run this script again with --skip-build"
    fi
fi
setup_docker
image_exists "$IMAGE" || die "no image $IMAGE: run ./jetson_install.sh"
if container_running "$RUN_CONTAINER"; then
    die "jetson_run.sh is running (container $RUN_CONTAINER); stop it first"
fi
DOCKER_BIN="$(command -v docker)"

# ---------------------------------------------------------------------------- user and folders
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    say "Creating the system user $SERVICE_USER"
    "${SUDO[@]}" useradd --system --create-home --home-dir "$STATE_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
SERVICE_UID="$(id -u "$SERVICE_USER")"
SERVICE_GID="$(id -g "$SERVICE_USER")"
[ "$SERVICE_UID" != 0 ] || die "the user $SERVICE_USER must not be root"
"${SUDO[@]}" mkdir -p "$STATE_DIR/trees" "$LOG_DIR"
if [ -d "$REPO/examples/trees" ] && [ -z "$("${SUDO[@]}" ls -A "$STATE_DIR/trees")" ]; then
    say "Copying the example trees into $STATE_DIR/trees"
    "${SUDO[@]}" cp -r "$REPO/examples/trees/." "$STATE_DIR/trees/"
fi
"${SUDO[@]}" chown -R "$SERVICE_USER": "$STATE_DIR" "$LOG_DIR"

# ---------------------------------------------------------------------------- settings
# The container sees /etc/bt_server as /opt/bt_server/config, and the two folders at their own paths.
"${SUDO[@]}" mkdir -p "$CONF"
"${SUDO[@]}" chmod 755 "$CONF"
if [ ! -f "$CONF/gateway.toml" ] || [ "$RECONFIGURE" = 1 ]; then
    say "Writing $CONF/gateway.toml"
    "${SUDO[@]}" tee "$CONF/gateway.toml" >/dev/null <<EOF
# Settings of the gateway (design 9.1), written by jetson_setup_system_service.sh.
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
# Parameters of the executor (design 8.1), written by jetson_setup_system_service.sh.
/**:
  ros__parameters:
    leaf_modules: ["bt_server_core.demo_leaves"]
    log_dir: $LOG_DIR
    journal_file: $STATE_DIR/goals.jsonl
EOF
fi
echo "$NAMESPACE" | "${SUDO[@]}" tee "$CONF/namespace" >/dev/null
"${SUDO[@]}" chmod 644 "$CONF/gateway.toml" "$CONF/executor.yaml" "$CONF/namespace"
# The gateway listens on the address and port in gateway.toml: those are what the name and the
# checks use. A kept file may say something else than was asked for.
file_address="$(sed -n 's/^http_host *= *"\([^"]*\)".*/\1/p' "$CONF/gateway.toml")"
file_port="$(sed -n 's/^http_port *= *\([0-9]*\).*/\1/p' "$CONF/gateway.toml")"
if [ "$file_address" != "$ADDRESS" ] || [ "$file_port" != "$PORT" ]; then
    warn "$CONF/gateway.toml is kept (no --reconfigure): the gateway listens on $file_address:$file_port, not $ADDRESS:$PORT"
fi
if ! grep -Fq "\"/$NAMESPACE/bt_executor\"" "$CONF/gateway.toml"; then
    warn "$CONF/gateway.toml is kept (no --reconfigure) and does not list /$NAMESPACE/bt_executor among its executors"
fi
ADDRESS="$file_address"
PORT="$file_port"
[ -n "$PORT" ] || die "$CONF/gateway.toml has no http_port: use --reconfigure"
# A gateway that listens on every address: the names get the address of the default route when the
# unit starts ("auto"), and the checks use today's.
NAME_ADDRESS="$ADDRESS"
CHECK_ADDRESS="$ADDRESS"
if listens_everywhere "$ADDRESS"; then
    NAME_ADDRESS="auto"
    CHECK_ADDRESS="$(lan_address)"
fi
if address_is_dynamic "$CHECK_ADDRESS"; then
    warn "$CHECK_ADDRESS came from DHCP: give this machine a DHCP reservation, or run this script again with --reconfigure when the address changes"
fi

# ---------------------------------------------------------------------------- host helper
"${SUDO[@]}" install -D -m 0755 "$REPO/bt_server_bringup/systemd/bt-publish-name" "$PREFIX/bin/bt-publish-name"

# ---------------------------------------------------------------------------- one executor of this name
# When the service is to start now: stop it first if it runs or restarts (it would be the executor
# found; stopping also cancels a pending restart), then look for the name. With --no-start a running
# service is left alone. The checks discover as the service does: the discovery and middleware
# settings of its unit (a drop-in), and none of this shell's, which the service does not get.
service_env=""
for name in "${ROS_ENV_NAMES[@]}"; do unset "$name"; done   # this shell's do not reach the service
if [ -e "$UNIT_DIR/$SERVICE_UNIT" ]; then
    service_env="$(systemctl show --property=Environment --value "$SERVICE_UNIT" 2>/dev/null || true)"
    for word in $service_env; do
        for name in "${ROS_ENV_NAMES[@]}"; do
            if [[ "$word" == "$name="* ]]; then export "$name=${word#*=}"; fi
        done
    done
fi
ros_env_options
if [ "$START" = 0 ] && service_up; then
    running_domain="$(tr ' ' '\n' <<<"$service_env" | sed -n 's/^ROS_DOMAIN_ID=//p')"
    running_namespace="$(tr ' ' '\n' <<<"$service_env" | sed -n 's/^BT_NAMESPACE=//p')"
    if [ "$running_domain" != "$DOMAIN" ] || [ "$running_namespace" != "$NAMESPACE" ]; then
        warn "the running service keeps /$running_namespace/bt_executor in domain $running_domain until it restarts (sudo systemctl restart bt-server.target); the new settings are not checked against the ROS network (--no-start)"
    fi
fi
if [ "$START" = 1 ]; then
    if service_up; then
        say "Stopping the running $SERVICE_UNIT"
    fi
    "${SUDO[@]}" systemctl stop "$SERVICE_UNIT" 2>/dev/null || true
    say "Looking for /$NAMESPACE/bt_executor on the ROS network (domain $DOMAIN)"
    status=0
    node_on_network "$IMAGE" "$DOMAIN" "/$NAMESPACE/bt_executor" "$SERVICE_UID:$SERVICE_GID" || status=$?
    case "$status" in
        0) die "an executor /$NAMESPACE/bt_executor is on the ROS network of domain $DOMAIN already (another machine?), so $SERVICE_UNIT is not started: use --namespace NS or --domain N" ;;
        1) echo "  none: the name is free" ;;
        *) warn "could not look for other executors" ;;
    esac
else
    echo "  (--no-start: the name /$NAMESPACE/bt_executor is not looked for on the ROS network)"
fi
# The discovery and middleware settings the container gets when the unit has them (for example in
# a drop-in from systemctl edit $SERVICE_UNIT): -e NAME passes NAME only when it is set.
ros_env_forward=""
for name in "${ROS_ENV_NAMES[@]}"; do ros_env_forward+="-e $name "; done

# ---------------------------------------------------------------------------- units
say "Writing the systemd units into $UNIT_DIR"
for template in "${UNITS[@]}"; do
    sed -e "s|@PREFIX@|$PREFIX|g" -e "s|@CONF@|$CONF|g" -e "s|@NAMESPACE@|$NAMESPACE|g" \
        -e "s|@DOMAIN@|$DOMAIN|g" -e "s|@ADDRESS@|$NAME_ADDRESS|g" -e "s|@IMAGE@|$IMAGE|g" \
        -e "s|@ROS_ENV@|${ros_env_forward% }|g" \
        -e "s|@CONTAINER@|$SERVICE_CONTAINER|g" -e "s|@DOCKER@|$DOCKER_BIN|g" \
        -e "s|@UID@|$SERVICE_UID|g" -e "s|@GID@|$SERVICE_GID|g" \
        -e "s|@STATE_DIR@|$STATE_DIR|g" -e "s|@LOG_DIR@|$LOG_DIR|g" -e "s|@TIMEZONE@|${TIMEZONE_OPTIONS[*]}|g" \
        "$REPO/container/$template.in" | "${SUDO[@]}" tee "$UNIT_DIR/$template" >/dev/null
    "${SUDO[@]}" chmod 644 "$UNIT_DIR/$template"
done
"${SUDO[@]}" systemctl daemon-reload

# ---------------------------------------------------------------------------- firewall
if command -v ufw >/dev/null && [ -n "$("${SUDO[@]}" ufw status 2>/dev/null | sed -n '/Status: active/p')" ]; then
    interface="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p' || true)"
    say "ufw is active: allowing port $PORT/tcp and mDNS (5353/udp) on $interface"
    "${SUDO[@]}" ufw allow in on "$interface" to any port "$PORT" proto tcp
    "${SUDO[@]}" ufw allow in on "$interface" to any port 5353 proto udp
    echo "Note: ROS 2 traffic must be allowed too (design 14.4), for example: sudo ufw allow in on <robot interface>"
fi

# ---------------------------------------------------------------------------- enable and start
say "Enabling ${UNITS[*]}"
# reenable: the links of an earlier version of the units' [Install] go, the current ones come
"${SUDO[@]}" systemctl reenable "${UNITS[@]}"
if [ "$START" = 0 ]; then
    echo "Enabled; not started (--no-start). Start with: sudo systemctl start bt-server.target"
    exit 0
fi
say "Starting the services"
"${SUDO[@]}" systemctl restart "$SERVICE_UNIT" bt-server-name.service
"${SUDO[@]}" systemctl start bt-server.target

# ---------------------------------------------------------------------------- check
say "Checking"
failed=0
for unit in "${UNITS[@]}"; do
    printf '  %-29s enabled=%-8s active=%s\n' "$unit" "$(systemctl is-enabled "$unit" 2>/dev/null || true)" \
        "$(systemctl is-active "$unit" 2>/dev/null || true)"
done
page=""
for _ in $(seq 1 60); do
    if page="$(curl -fsS --max-time 2 "http://$CHECK_ADDRESS:$PORT/" 2>/dev/null)"; then break; fi
    page=""
    sleep 1
done
if [ -n "$page" ] && grep -qi "<html" <<<"$page"; then
    echo "  the page answers on http://$CHECK_ADDRESS:$PORT/"
else
    echo "  the page does not answer on http://$CHECK_ADDRESS:$PORT/ (see: journalctl -u $SERVICE_UNIT)"
    failed=1
fi
for unit in "$SERVICE_UNIT" bt-server-name.service; do
    systemctl is-active --quiet "$unit" || { echo "  $unit is not active (see: journalctl -u $unit)"; failed=1; }
    systemctl is-enabled --quiet "$unit" || { echo "  $unit is not enabled"; failed=1; }
done
container_running "$SERVICE_CONTAINER" || { echo "  the container $SERVICE_CONTAINER is not running"; failed=1; }
status=0
node_on_network "$IMAGE" "$DOMAIN" "/$NAMESPACE/bt_executor" "$SERVICE_UID:$SERVICE_GID" || status=$?
case "$status" in
    0) echo "  the executor /$NAMESPACE/bt_executor is on the ROS network (domain $DOMAIN)" ;;
    1) echo "  the executor /$NAMESPACE/bt_executor is not on the ROS network (see: journalctl -u $SERVICE_UNIT)"; failed=1 ;;
    *) echo "  could not look for the executor on the ROS network"; failed=1 ;;
esac
for name in "bt.$HOST_SHORT.local" "bt-$HOST_SHORT.local"; do
    resolved=""
    for _ in $(seq 1 10); do
        resolved="$(timeout 5 avahi-resolve -4 -n "$name" 2>/dev/null || true)"
        [ -n "$resolved" ] && break
        sleep 1
    done
    if [ -n "$resolved" ]; then
        echo "  $name -> ${resolved##*[[:space:]]}"
        if [ "${resolved##*[[:space:]]}" != "$CHECK_ADDRESS" ]; then
            echo "  $name leads to ${resolved##*[[:space:]]}, not to this gateway's $CHECK_ADDRESS (another machine of the same name?)"
            failed=1
        fi
    else
        echo "  $name is not published (see: journalctl -u bt-server-name)"
        failed=1
    fi
    # The gateway accepts the name (its public_names, with the container's host name filled in).
    if ! curl -fsS --max-time 5 -o /dev/null --resolve "${name,,}:$PORT:$CHECK_ADDRESS" "http://${name,,}:$PORT/" 2>/dev/null; then
        echo "  the gateway does not answer for http://${name,,}:$PORT/ (Host check)"
        failed=1
    fi
done
if [ "$failed" = 0 ]; then
    suffix=""
    [ "$PORT" = 80 ] || suffix=":$PORT"
    echo
    echo "The Behavior Tree Server runs in the container $SERVICE_CONTAINER and starts at boot. Open one of:"
    echo "  http://bt-${HOST_SHORT,,}.local$suffix/   (Windows, Linux)"
    echo "  http://bt.${HOST_SHORT,,}.local$suffix/   (macOS, iOS)"
    echo "  http://$CHECK_ADDRESS$suffix/"
fi
exit "$failed"
