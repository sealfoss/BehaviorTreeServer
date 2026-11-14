#!/usr/bin/env bash
# Start the Behavior Tree Server on a Jetson from the container image of ./jetson_install.sh: the
# executor /<namespace>/bt_executor and the gateway, in the foreground, with this checkout's
# settings (config/) and data (.data/). Ctrl+C stops both; the executor first stops a running tree
# as Stop does and waits for its cancels (design 8.2).
#
#   ./jetson_run.sh [--domain N] [--namespace NS] [--image NAME] [--no-names]
#
#   --domain N       ROS_DOMAIN_ID (default: $ROS_DOMAIN_ID, or 0)
#   --namespace NS   executor namespace (default: the one jetson_install.sh configured)
#   --image NAME     the image (default: bt-server:latest)
#   --no-names       do not publish bt.<hostname>.local and bt-<hostname>.local
#
# The container has no Avahi, so while it runs this script publishes the two names from the host,
# for the gateway's address. It refuses to start when an executor of the same name is on the ROS
# network of the domain already: two would both accept the same goals. When Docker needs sudo (a
# user not in the group docker), the script runs itself under sudo; the container still runs as you.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROG="jetson_run.sh"
# shellcheck source=lib/common.sh
source "$REPO/lib/common.sh"

DOMAIN="${ROS_DOMAIN_ID:-0}"
NAMESPACE=""
IMAGE="$DEFAULT_IMAGE"
NAMES=1
CONFIG_DIR="$REPO/config"
DATA_DIR="$REPO/.data"

while [ $# -gt 0 ]; do
    case "$1" in
        --domain) need_value "$1" $#; DOMAIN="$2"; shift 2 ;;
        --namespace) need_value "$1" $#; NAMESPACE="$2"; shift 2 ;;
        --image) need_value "$1" $#; IMAGE="$2"; shift 2 ;;
        --no-names) NAMES=0; shift ;;
        -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) unknown_option "$1" ;;
    esac
done

[ -r "$CONFIG_DIR/gateway.toml" ] || die "$CONFIG_DIR/gateway.toml missing: run ./jetson_install.sh"
[ -r "$CONFIG_DIR/executor.yaml" ] || die "$CONFIG_DIR/executor.yaml missing: run ./jetson_install.sh"
if [ -z "$NAMESPACE" ]; then
    NAMESPACE="$(head -1 "$CONFIG_DIR/namespace" 2>/dev/null || true)"
    [ -n "$NAMESPACE" ] || NAMESPACE="$(default_namespace)"
fi
check_namespace "$NAMESPACE"
check_domain "$DOMAIN"

# A user who may use Docker only through sudo: run this whole script under sudo, once. (A docker run
# started under sudo in the background would take over the terminal, as sudo does with use_pty, the
# default on Ubuntu, and Ctrl+C would no longer reach this script.) sudo drops the environment, so
# the ROS settings that the container gets (ROS_ENV_NAMES) are handed over explicitly.
if [ "$(id -u)" != 0 ] && command -v docker >/dev/null && ! docker info >/dev/null 2>&1; then
    args=(--domain "$DOMAIN" --namespace "$NAMESPACE" --image "$IMAGE")
    [ "$NAMES" = 1 ] || args+=(--no-names)
    keep=()
    for name in "${ROS_ENV_NAMES[@]}"; do
        if [ -n "${!name:-}" ]; then keep+=("$name=${!name}"); fi
    done
    if [ -n "${SUDO_ASKPASS:-}" ]; then sudo_command=(sudo -A); else sudo_command=(sudo); fi
    exec "${sudo_command[@]}" -- env "${keep[@]}" "$REPO/jetson_run.sh" "${args[@]}"
fi

# --------------------------------------------------------------------------- the settings
# The container runs as the user who started this script (also under sudo), who owns .data/.
USER_ID="$(id -u):$(id -g)"
if [ "$(id -u)" = 0 ] && [ -n "${SUDO_UID:-}" ]; then USER_ID="$SUDO_UID:${SUDO_GID:-$SUDO_UID}"; fi
mkdir -p "$DATA_DIR/trees" "$DATA_DIR/log"
if [ "$(id -u)" = 0 ]; then
    chown "$USER_ID" "$DATA_DIR" "$DATA_DIR/trees" "$DATA_DIR/log"
fi

HOST_ADDRESS="$(gateway_value "$CONFIG_DIR/gateway.toml" http_host)"
HOST_PORT="$(gateway_value "$CONFIG_DIR/gateway.toml" http_port)"
HOST_PORT="${HOST_PORT:-80}"
HOST_SHORT="$(short_hostname)"
# The container sees only .data/ (at the same path) and the settings: folders elsewhere would be
# inside the container, and lost with it.
for setting in "$(gateway_value "$CONFIG_DIR/gateway.toml" trees_dir)" \
               "$(executor_value "$CONFIG_DIR/executor.yaml" log_dir)" \
               "$(executor_value "$CONFIG_DIR/executor.yaml" journal_file)"; do
    if [ -n "$setting" ] && [[ "$setting" != "$DATA_DIR"/* ]]; then
        die "the settings in $CONFIG_DIR name $setting, which is outside $DATA_DIR (moved checkout?): run ./jetson_install.sh --reconfigure"
    fi
done
if [ "$HOST_PORT" -lt "$(unprivileged_port_start)" ]; then
    die "port $HOST_PORT (http_port in $CONFIG_DIR/gateway.toml) needs privileges this container does not get: use a port from $(unprivileged_port_start) (./jetson_install.sh --port 8080 --reconfigure), or the service, ./jetson_setup_system_service.sh"
fi
# The gateway lists this run's executor first (a page that has not chosen one attaches to it): with
# another namespace than the settings name, it reads a copy of gateway.toml that names this one.
GATEWAY_OPTIONS=()
RUN_GATEWAY="$DATA_DIR/.gateway-run.toml"
rm -f "$RUN_GATEWAY"
if ! grep -Fq "\"/$NAMESPACE/bt_executor\"" "$CONFIG_DIR/gateway.toml"; then
    sed "s|^executors *=.*|executors = [\"/$NAMESPACE/bt_executor\"]|" "$CONFIG_DIR/gateway.toml" > "$RUN_GATEWAY"
    if ! grep -q '^executors *=' "$RUN_GATEWAY"; then
        printf 'executors = ["/%s/bt_executor"]\n' "$NAMESPACE" >> "$RUN_GATEWAY"
    fi
    chmod 644 "$RUN_GATEWAY"
    GATEWAY_OPTIONS=(-v "$RUN_GATEWAY:/opt/bt_server/config/gateway.toml:ro")
    note "$PROG: for this run the gateway's executors are [/$NAMESPACE/bt_executor] ($CONFIG_DIR/gateway.toml names others)"
fi
check_run_settings "$CONFIG_DIR" jetson_install.sh
NAME_ADDRESS="$HOST_ADDRESS"
if listens_everywhere "$HOST_ADDRESS"; then
    NAME_ADDRESS="$(lan_address)"
fi

setup_sudo
setup_docker
ros_env_options
image_exists "$IMAGE" || die "no image $IMAGE: run ./jetson_install.sh"

# A service of this machine that runs (or is restarting) an executor of this name in this domain.
refuse_running_service "$NAMESPACE" "$DOMAIN"
if container_running "$RUN_CONTAINER"; then
    die "jetson_run.sh is running already (container $RUN_CONTAINER)"
fi
"${DOCKER[@]}" rm --force "$RUN_CONTAINER" >/dev/null 2>&1 || true   # one left over, stopped

require_name_free "$NAMESPACE" "$DOMAIN" ros_nodes_container "$DOMAIN" "$IMAGE" "$USER_ID"

# --------------------------------------------------------------------------- cleaning up
# On every way out: stop the container if it still runs (once docker run has ended, it would run
# on unseen), and withdraw the names.
docker_pid=""
names_pid=""
stop_sent=0
# Whether the container runs, asked in a session of its own, so that a Ctrl+C or a hangup cannot
# make the answer "no" (here Docker never goes through sudo: the script runs as root or as a
# member of the group docker). Unknown counts as running.
container_up() {
    local state
    if state="$(setsid -w "${DOCKER[@]}" container inspect --format '{{.State.Running}}' "$RUN_CONTAINER" </dev/null 2>&1)"; then
        [ "$state" = true ]
    else
        [[ "$state" != *"No such"* ]]
    fi
}
# Exactly once: every docker stop sends another SIGTERM, and a second one would make the executor
# exit without waiting for its cancels. In a session of its own (setsid), so that Ctrl+C or a
# hangup cannot cut it short.
stop_container() {
    [ "$stop_sent" = 1 ] && return 0
    stop_sent=1
    note "$PROG: stopping (a running tree is stopped first and its cancels are waited for)"
    if ! setsid -w "${DOCKER[@]}" stop --time 30 "$RUN_CONTAINER" </dev/null >/dev/null 2>&1; then
        note "$PROG: docker stop $RUN_CONTAINER did not succeed; see: sudo docker ps"
    fi
}
cleanup() {
    if [ -n "$docker_pid" ] && [ "$stop_sent" = 0 ] && container_up; then
        stop_container
    fi
    rm -f "$RUN_GATEWAY"
    if [ -n "$names_pid" ]; then
        # the publisher and its avahi-publish processes: a process group of their own (setsid)
        kill -TERM -- "-$names_pid" 2>/dev/null || kill -TERM "$names_pid" 2>/dev/null || true
        wait "$names_pid" 2>/dev/null || true
        names_pid=""
    fi
}
trap cleanup EXIT

# --------------------------------------------------------------------------- the names
if [ "$NAMES" = 1 ]; then
    if [ -z "$NAME_ADDRESS" ] || [[ "$NAME_ADDRESS" == 127.* ]]; then
        warn "the gateway listens on '${HOST_ADDRESS:-?}', not on a LAN address: the names are not published"
    elif ! command -v avahi-publish >/dev/null || ! systemctl is-active --quiet avahi-daemon 2>/dev/null; then
        warn "Avahi is not running or avahi-utils is missing (run ./jetson_install.sh): the names are not published"
    else
        # In a session of its own, so that the terminal's signals (Ctrl+C, hangup) do not reach it:
        # cleanup withdraws the names, whichever way this script ends.
        BT_ADDRESS="$NAME_ADDRESS" setsid "$REPO/bt_server_bringup/systemd/bt-publish-name" \
            "bt.$HOST_SHORT.local" "bt-$HOST_SHORT.local" </dev/null >/dev/null 2>&1 &
        names_pid=$!
        sleep 1
        if ! kill -0 "$names_pid" 2>/dev/null; then
            wait "$names_pid" 2>/dev/null || true
            names_pid=""
            warn "bt.$HOST_SHORT.local and bt-$HOST_SHORT.local could not be published (published by something else already?)"
        fi
    fi
fi

# --------------------------------------------------------------------------- the container
# docker run gets no signals of its own (--sig-proxy=false). Ctrl+C, SIGTERM and SIGHUP only ask
# this script to stop; the loop below then stops the container once, with docker stop, as soon as
# it is up. Its single SIGTERM reaches run.sh, which stops the executor and the gateway with exactly
# one SIGTERM each (a second would make the executor exit at once instead of waiting for its
# cancels). Further signals change nothing, and a message that cannot be written (terminal gone)
# stops nothing.
stop_requested=0
trap 'stop_requested=1' INT TERM HUP
trap '' PIPE

suffix=""
[ "$HOST_PORT" = 80 ] || suffix=":$HOST_PORT"
note "$PROG: executor /$NAMESPACE/bt_executor (ROS domain $DOMAIN) and gateway on http://${NAME_ADDRESS:-$HOST_ADDRESS}$suffix/ ; Ctrl+C stops both"
if [ -n "$names_pid" ]; then
    note "$PROG: also http://bt-${HOST_SHORT,,}.local$suffix/ (Windows, Linux) and http://bt.${HOST_SHORT,,}.local$suffix/ (macOS, iOS)"
fi

"${DOCKER[@]}" run --rm --name "$RUN_CONTAINER" "${CONTAINER_RUN_OPTIONS[@]}" --sig-proxy=false \
    --cap-drop ALL --user "$USER_ID" --stop-timeout 30 \
    -e HOME="$DATA_DIR" "${TIMEZONE_OPTIONS[@]}" "${ROS_ENV_OPTIONS[@]}" \
    -v "$CONFIG_DIR:/opt/bt_server/config:ro" "${GATEWAY_OPTIONS[@]}" -v "$DATA_DIR:$DATA_DIR" \
    "$IMAGE" --namespace "$NAMESPACE" --domain "$DOMAIN" </dev/null &
docker_pid=$!
while kill -0 "$docker_pid" 2>/dev/null; do
    if [ "$stop_requested" = 1 ] && [ "$stop_sent" = 0 ] && container_running "$RUN_CONTAINER"; then
        stop_container    # (not before the container is up: a stop then would do nothing)
    fi
    sleep 0.5 || true   # (Ctrl+C reaches sleep too, which then fails; set -e must not end the script)
done
status=0
wait "$docker_pid" || status=$?
# Stopped on request, it ends as run.sh does then (0); otherwise as the container ended.
if [ "$stop_requested" = 1 ]; then status=0; fi
exit "$status"
