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
#   --namespace NS    executor namespace (default: the service's, else the host name made a ROS name)
#   --address IP      LAN address the gateway listens on (default: the address of the default route)
#   --port N          gateway port (default 80)
#   --domain N        ROS_DOMAIN_ID of both services (default: the service's, else 0)
#   --reconfigure     rewrite /etc/bt_server/*.toml|yaml even if they exist
#   --skip-build      use the existing build in the prefix
#   --no-start        install and enable, but do not start now
#
# Run it as a user who may use sudo. It builds as that user into the prefix (design 14.1),
# creates the system user btserver (home /var/lib/bt_server), writes /etc/bt_server/ and the
# units, enables them, starts them, and checks that the executor is on the ROS network, the page
# answers and the names resolve to this gateway. Before it writes the settings and the units, it
# refuses to go on when an executor of the same name is on the ROS network of the domain already
# (it stops its own executor first only when that runs this very name in this domain).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROG="setup_system_service.sh"
# shellcheck source=lib/common.sh
source "$REPO/lib/common.sh"

PREFIX="/opt/bt_server"
NAMESPACE=""
ADDRESS=""
PORT=80
DOMAIN=""
RECONFIGURE=0
SKIP_BUILD=0
START=1
UNITS=("$EXECUTOR_SERVICE" "$GATEWAY_SERVICE" "$NAME_SERVICE" "$TARGET")
SERVICES=("$EXECUTOR_SERVICE" "$GATEWAY_SERVICE" "$NAME_SERVICE")

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) need_value "$1" $#; PREFIX="$(realpath -m "$2")"; shift 2 ;;
        --namespace) need_value "$1" $#; NAMESPACE="$2"; shift 2 ;;
        --address) need_value "$1" $#; ADDRESS="$2"; shift 2 ;;
        --port) need_value "$1" $#; PORT="$2"; shift 2 ;;
        --domain) need_value "$1" $#; DOMAIN="$2"; shift 2 ;;
        --reconfigure) RECONFIGURE=1; shift ;;
        --skip-build) SKIP_BUILD=1; shift ;;
        --no-start) START=0; shift ;;
        -h|--help) sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) unknown_option "$1" ;;
    esac
done

require_sudo
if [ "$(id -u)" = 0 ]; then BUILD_USER="root"; else BUILD_USER="$(id -un)"; fi
command -v systemctl >/dev/null || die "systemd is required"
keep_service_settings "$EXECUTOR_SERVICE"   # run again, the service keeps its namespace and domain
check_namespace "$NAMESPACE"
check_domain "$DOMAIN"
check_port "$PORT"
if [ -z "$ADDRESS" ]; then
    ADDRESS="$(lan_address)"
    [ -n "$ADDRESS" ] || die "could not find the LAN address; give it with --address"
fi
refuse_other_kind "$CONTAINER_SERVICE" jetson_setup_system_service.sh

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

# ---------------------------------------------------------------------------- one executor of this name
# Before anything is written: the checks discover as the executor service does (use_unit_ros_env).
use_unit_ros_env "$EXECUTOR_SERVICE"
setup_name_check "$EXECUTOR_SERVICE" ros_nodes_native "$DOMAIN"

# ---------------------------------------------------------------------------- user, folders, settings
setup_service_user
write_service_settings

# ---------------------------------------------------------------------------- units
say "Writing the systemd units into $UNIT_DIR"
UNIT_VALUES=([PREFIX]="$PREFIX" [CONF]="$CONF" [NAMESPACE]="$NAMESPACE" [USER]="$SERVICE_USER"
             [DOMAIN]="$DOMAIN" [ROS_SETUP]="$ROS_SETUP" [ADDRESS]="$NAME_ADDRESS")
for unit in "${UNITS[@]}"; do
    install_unit "$REPO/bt_server_bringup/systemd/$unit.in" "$unit"
done
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
check_page "$CHECK_ADDRESS" "$PORT" "journalctl -u ${GATEWAY_SERVICE%.service}"
check_units_running "${SERVICES[@]}"
check_executor "$NAMESPACE" "$DOMAIN" "journalctl -u ${EXECUTOR_SERVICE%.service}" ros_nodes_native "$DOMAIN"
check_names "$CHECK_ADDRESS" "$PORT"
if [ "$CHECK_FAILED" = 0 ]; then
    echo
    echo "The Behavior Tree Server runs and starts at boot. Open one of:"
    print_addresses "$CHECK_ADDRESS" "$PORT"
fi
exit "$CHECK_FAILED"
