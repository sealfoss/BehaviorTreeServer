#!/usr/bin/env bash
# Start the Behavior Tree Server from this checkout's install (made by ./install.sh): the
# executor /<namespace>/bt_executor and the gateway, in the foreground, and while they run the
# names bt.<hostname>.local and bt-<hostname>.local for the gateway's address. Ctrl+C stops both;
# the executor first stops a running tree as Stop does and waits for its cancels (design 8.2).
#
#   ./run.sh [--prefix DIR] [--domain N] [--namespace NS] [--no-names] [--no-checks]
#
#   --prefix DIR     the install to run (default: this checkout)
#   --domain N       ROS_DOMAIN_ID (default: $ROS_DOMAIN_ID, or 0)
#   --namespace NS   executor namespace (default: the one install.sh configured)
#   --no-names       do not publish bt.<hostname>.local and bt-<hostname>.local
#   --no-checks      start without the checks below (the container's entry point: jetson_run.sh
#                    and the service check before they start it)
#
# The settings are <prefix>/config/gateway.toml and <prefix>/config/executor.yaml; with another
# namespace than they name, the gateway lists this run's executor (a copy of gateway.toml beside
# it, removed afterwards). It refuses to start, with a message saying what to do, when an executor
# of the same name is on the ROS network of the domain already (another machine, or a service of
# this one: two would both accept the same goals; it looks for about 5 s first), when a service of
# this machine runs that name in that domain, or when the gateway's address is no longer this
# machine's, its port needs root or something listens on that port already.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROG="run.sh"
# shellcheck source=lib/common.sh
source "$HERE/lib/common.sh"

PREFIX="$HERE"
DOMAIN="${ROS_DOMAIN_ID:-0}"
NAMESPACE=""
NAMES=1
CHECKS=1

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) need_value "$1" $#; PREFIX="$(realpath -m "$2")"; shift 2 ;;
        --domain) need_value "$1" $#; DOMAIN="$2"; shift 2 ;;
        --namespace) need_value "$1" $#; NAMESPACE="$2"; shift 2 ;;
        --no-names) NAMES=0; shift ;;
        --no-checks) CHECKS=0; shift ;;
        -h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
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
PORT="${PORT:-80}"

if [ "$CHECKS" = 1 ]; then
    check_run_settings "$CONFIG_DIR" install.sh
    if [ "$(id -u)" != 0 ] && [ "$PORT" -lt "$(unprivileged_port_start)" ]; then
        die "port $PORT (http_port in $CONFIG_DIR/gateway.toml) needs root: use a port from $(unprivileged_port_start) (./install.sh --port 8080 --reconfigure), or the services, ./setup_system_service.sh"
    fi
    # A service of this machine that runs (or is restarting) an executor of this name in this domain.
    refuse_running_service "$NAMESPACE" "$DOMAIN"
    refuse_port_in_use "$HOST" "$PORT"
    require_name_free "$NAMESPACE" "$DOMAIN" ros_nodes_native "$DOMAIN"
fi

# --------------------------------------------------------------------------- cleaning up
# On every way out: withdraw the names and remove the gateway's copy of the settings.
RUN_GATEWAY=""
cleanup() {
    stop_run_names
    if [ -n "$RUN_GATEWAY" ]; then rm -f "$RUN_GATEWAY"; fi
}
trap cleanup EXIT

# --------------------------------------------------------------------------- the settings, the names
GATEWAY_CONFIG="$CONFIG_DIR/gateway.toml"
if ! gateway_lists "$GATEWAY_CONFIG" "$NAMESPACE"; then
    if RUN_GATEWAY="$(mktemp --suffix .toml "$CONFIG_DIR/.gateway-run.XXXXXX" 2>/dev/null)"; then
        write_run_gateway "$CONFIG_DIR/gateway.toml" "$NAMESPACE" "$RUN_GATEWAY"
        GATEWAY_CONFIG="$RUN_GATEWAY"
    else
        RUN_GATEWAY=""
        warn "$CONFIG_DIR is not writable: the gateway lists the executors of its gateway.toml first, not /$NAMESPACE/bt_executor"
    fi
fi
PAGE_ADDRESS="$(page_address "$HOST")"
if [ "$NAMES" = 1 ]; then
    start_run_names "$PAGE_ADDRESS" "$HOST" "$PREFIX/bin/bt-publish-name" install.sh
fi

# --------------------------------------------------------------------------- the two processes
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
setsid python -m bt_server_gateway --config "$GATEWAY_CONFIG" &
pids+=("$!")

# (In the container, under jetson_run.sh or the service, it is they that stop it.)
if [ "$CHECKS" = 1 ]; then stop_hint="Ctrl+C stops both"; else stop_hint=""; fi
run_banner "$NAMESPACE" "$DOMAIN" "${PAGE_ADDRESS:-$HOST}" "$PORT" "$stop_hint"

# When one process ends by itself, stop the other.
status=0
wait -n "${pids[@]}" || status=$?
stop_all
exit "$status"
