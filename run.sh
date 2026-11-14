#!/usr/bin/env bash
# Start the Behavior Tree Server from this checkout's install (made by ./install.sh): the
# executor /<namespace>/bt_executor and the gateway, in the foreground. Ctrl+C stops both; the
# executor first stops a running tree as Stop does and waits for its cancels (design 8.2).
#
#   ./run.sh [--prefix DIR] [--domain N] [--namespace NS] [--no-checks]
#
#   --prefix DIR     the install to run (default: this checkout)
#   --domain N       ROS_DOMAIN_ID (default: $ROS_DOMAIN_ID, or 0)
#   --namespace NS   executor namespace (default: the one install.sh configured)
#   --no-checks      start without the checks below (the container's entry point: jetson_run.sh
#                    and the service check before they start it)
#
# The settings are <prefix>/config/gateway.toml and <prefix>/config/executor.yaml. It refuses to
# start, with a message saying what to do, when an executor of the same name is on the ROS network
# of the domain already (another machine, or a service of this one: two would both accept the same
# goals; it looks for about 5 s first), when a service of this machine runs that name in that
# domain, or when the gateway's address is no longer this machine's or its port needs root.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROG="run.sh"
# shellcheck source=lib/common.sh
source "$HERE/lib/common.sh"

PREFIX="$HERE"
DOMAIN="${ROS_DOMAIN_ID:-0}"
NAMESPACE=""
CHECKS=1

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) need_value "$1" $#; PREFIX="$(realpath -m "$2")"; shift 2 ;;
        --domain) need_value "$1" $#; DOMAIN="$2"; shift 2 ;;
        --namespace) need_value "$1" $#; NAMESPACE="$2"; shift 2 ;;
        --no-checks) CHECKS=0; shift ;;
        -h|--help) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) unknown_option "$1" ;;
    esac
done

if [ -x "$PREFIX/.venv/bin/python" ]; then VENV="$PREFIX/.venv"; else VENV="$PREFIX/venv"; fi
CONFIG_DIR="$PREFIX/config"

[ -r "$ROS_SETUP" ] || die "ROS 2 Lyrical is not installed ($ROS_SETUP): run ./install.sh"
[ -r "$PREFIX/install/setup.bash" ] || die "nothing built in $PREFIX/install: run ./install.sh"
[ -x "$VENV/bin/python" ] || die "no Python environment in $PREFIX: run ./install.sh"
[ -r "$CONFIG_DIR/gateway.toml" ] || die "$CONFIG_DIR/gateway.toml missing: run ./install.sh"
[ -r "$CONFIG_DIR/executor.yaml" ] || die "$CONFIG_DIR/executor.yaml missing: run ./install.sh"
if [ -z "$NAMESPACE" ]; then
    NAMESPACE="$(head -1 "$CONFIG_DIR/namespace" 2>/dev/null || true)"
    [ -n "$NAMESPACE" ] || NAMESPACE="$(default_namespace)"
fi
check_namespace "$NAMESPACE"
check_domain "$DOMAIN"
HOST="$(gateway_value "$CONFIG_DIR/gateway.toml" http_host)"
PORT="$(gateway_value "$CONFIG_DIR/gateway.toml" http_port)"

if [ "$CHECKS" = 1 ]; then
    check_run_settings "$CONFIG_DIR" install.sh
    if [ "$(id -u)" != 0 ] && [ "${PORT:-80}" -lt "$(unprivileged_port_start)" ]; then
        die "port ${PORT:-80} (http_port in $CONFIG_DIR/gateway.toml) needs root: use a port from $(unprivileged_port_start) (./install.sh --port 8080 --reconfigure), or the services, ./setup_system_service.sh"
    fi
    refuse_running_service "$NAMESPACE" "$DOMAIN"
    require_name_free "$NAMESPACE" "$DOMAIN" ros_nodes_native "$DOMAIN"
fi

set +u
# shellcheck disable=SC1090
source "$ROS_SETUP"
# shellcheck disable=SC1091
source "$PREFIX/install/setup.bash"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
set -u
export ROS_DOMAIN_ID="$DOMAIN"

# Each process runs in a session of its own (setsid), so that Ctrl+C reaches only this script,
# which then stops each process with exactly one SIGTERM: a second signal makes the executor
# exit at once instead of waiting for its cancels (design 8.2). SIGTERM, not SIGINT, because
# a background command of a script starts with SIGINT ignored.
pids=()
stop_all() {
    trap - INT TERM HUP
    for pid in "${pids[@]}"; do kill -TERM "$pid" 2>/dev/null || true; done
    for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done
}
trap 'stop_all; exit 0' INT TERM HUP

setsid python -m bt_server_executor --ros-args -r "__ns:=/$NAMESPACE" -r __node:=bt_executor \
    --params-file "$CONFIG_DIR/executor.yaml" &
pids+=("$!")
setsid python -m bt_server_gateway --config "$CONFIG_DIR/gateway.toml" &
pids+=("$!")

echo "run.sh: executor /$NAMESPACE/bt_executor (ROS domain $DOMAIN) and gateway on http://$HOST:$PORT/ ; Ctrl+C stops both"

# When one process ends by itself, stop the other.
status=0
wait -n "${pids[@]}" || status=$?
stop_all
exit "$status"
