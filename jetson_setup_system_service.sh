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
# shellcheck source=lib/common.sh
source "$REPO/lib/common.sh"

NAMESPACE=""
ADDRESS=""
PORT=80
DOMAIN=""
IMAGE=""
RECONFIGURE=0
SKIP_BUILD=0
START=1
PREFIX="/opt/bt_server"     # on the host: bin/bt-publish-name
UNITS=("$CONTAINER_SERVICE" "$NAME_SERVICE" "$TARGET")
SERVICES=("$CONTAINER_SERVICE" "$NAME_SERVICE")

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
        *) unknown_option "$1" ;;
    esac
done

require_sudo
command -v systemctl >/dev/null || die "systemd is required"
keep_service_settings "$CONTAINER_SERVICE"   # run again, the service keeps its namespace and domain
if [ -z "$IMAGE" ] && unit_installed "$CONTAINER_SERVICE"; then
    IMAGE="$(unit_env "$CONTAINER_SERVICE" BT_IMAGE)"
fi
IMAGE="${IMAGE:-$DEFAULT_IMAGE}"
check_namespace "$NAMESPACE"
check_domain "$DOMAIN"
check_port "$PORT"
if [ -z "$ADDRESS" ]; then
    ADDRESS="$(lan_address)"
    [ -n "$ADDRESS" ] || die "could not find the LAN address; give it with --address"
fi
refuse_other_kind "$EXECUTOR_SERVICE" "$GATEWAY_SERVICE" setup_system_service.sh

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

# ---------------------------------------------------------------------------- one executor of this name
# Before anything is written (the user btserver aside, as whom the check runs): the checks discover
# as the service does (use_unit_ros_env).
setup_service_user
use_unit_ros_env "$CONTAINER_SERVICE"
ros_env_options
setup_name_check "$CONTAINER_SERVICE" ros_nodes_container "$DOMAIN" "$IMAGE" "$SERVICE_UID:$SERVICE_GID"

# ---------------------------------------------------------------------------- folders, settings
# The container sees /etc/bt_server as /opt/bt_server/config, and the two folders at their own paths.
write_service_settings

# ---------------------------------------------------------------------------- host helper
"${SUDO[@]}" install -D -m 0755 "$REPO/bt_server_bringup/systemd/bt-publish-name" "$PREFIX/bin/bt-publish-name"
# The discovery and middleware settings the container gets when the unit has them (for example in
# a drop-in from systemctl edit $CONTAINER_SERVICE): -e NAME passes NAME only when it is set.
ros_env_forward=""
for name in "${ROS_ENV_NAMES[@]}"; do ros_env_forward+="-e $name "; done

# ---------------------------------------------------------------------------- units
say "Writing the systemd units into $UNIT_DIR"
UNIT_VALUES=([PREFIX]="$PREFIX" [CONF]="$CONF" [NAMESPACE]="$NAMESPACE" [DOMAIN]="$DOMAIN"
             [ADDRESS]="$NAME_ADDRESS" [IMAGE]="$IMAGE" [ROS_ENV]="${ros_env_forward% }"
             [CONTAINER]="$SERVICE_CONTAINER" [DOCKER]="$DOCKER_BIN" [UID]="$SERVICE_UID"
             [GID]="$SERVICE_GID" [STATE_DIR]="$STATE_DIR" [LOG_DIR]="$LOG_DIR"
             [TIMEZONE]="${TIMEZONE_OPTIONS[*]}")
install_unit "$REPO/container/$CONTAINER_SERVICE.in" "$CONTAINER_SERVICE"
install_unit "$REPO/bt_server_bringup/systemd/$NAME_SERVICE.in" "$NAME_SERVICE"
install_unit "$REPO/container/$TARGET.in" "$TARGET"
"${SUDO[@]}" systemctl daemon-reload

open_firewall "$PORT"

# ---------------------------------------------------------------------------- enable and start
say "Enabling ${UNITS[*]}"
# reenable: the links of an earlier version of the units' [Install] go, the current ones come
"${SUDO[@]}" systemctl reenable "${UNITS[@]}"
if [ "$START" = 0 ]; then
    echo "Enabled; not started (--no-start). Start with: sudo systemctl start $TARGET"
    exit 0
fi
say "Starting the services"
"${SUDO[@]}" systemctl restart "${SERVICES[@]}"
"${SUDO[@]}" systemctl start "$TARGET"

# ---------------------------------------------------------------------------- check
say "Checking"
print_unit_states "${UNITS[@]}"
check_page "$CHECK_ADDRESS" "$PORT" "journalctl -u ${CONTAINER_SERVICE%.service}"
check_units_running "${SERVICES[@]}"
container_running "$SERVICE_CONTAINER" || check_failed "the container $SERVICE_CONTAINER is not running"
check_executor "$NAMESPACE" "$DOMAIN" "journalctl -u ${CONTAINER_SERVICE%.service}" \
    ros_nodes_container "$DOMAIN" "$IMAGE" "$SERVICE_UID:$SERVICE_GID"
check_names "$CHECK_ADDRESS" "$PORT"
if [ "$CHECK_FAILED" = 0 ]; then
    echo
    echo "The Behavior Tree Server runs in the container $SERVICE_CONTAINER and starts at boot. Open one of:"
    print_addresses "$CHECK_ADDRESS" "$PORT"
fi
exit "$CHECK_FAILED"
