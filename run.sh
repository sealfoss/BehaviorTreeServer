#!/usr/bin/env bash
# Start the Behavior Tree Server from this checkout's install (made by ./install.sh): the
# executor /<namespace>/bt_executor and the gateway, in the foreground. Ctrl+C stops both; the
# executor first stops a running tree as Stop does and waits for its cancels (design 8.2).
#
#   ./run.sh [--prefix DIR] [--domain N] [--namespace NS]
#
#   --prefix DIR     the install to run (default: this checkout)
#   --domain N       ROS_DOMAIN_ID (default: $ROS_DOMAIN_ID, or 0)
#   --namespace NS   executor namespace (default: the one install.sh configured, or cell1)
#
# The settings are <prefix>/config/gateway.toml and <prefix>/config/executor.yaml.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="$REPO"
DOMAIN="${ROS_DOMAIN_ID:-0}"
NAMESPACE=""
ROS_SETUP="/opt/ros/lyrical/setup.bash"

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) PREFIX="$(realpath -m "$2")"; shift 2 ;;
        --domain) DOMAIN="$2"; shift 2 ;;
        --namespace) NAMESPACE="$2"; shift 2 ;;
        -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "run.sh: unknown option $1" >&2; exit 2 ;;
    esac
done

if [ -x "$PREFIX/.venv/bin/python" ]; then VENV="$PREFIX/.venv"; else VENV="$PREFIX/venv"; fi
CONFIG_DIR="$PREFIX/config"
die() { printf 'run.sh: %s\n' "$*" >&2; exit 1; }

[ -r "$ROS_SETUP" ] || die "ROS 2 Lyrical is not installed ($ROS_SETUP): run ./install.sh"
[ -r "$PREFIX/install/setup.bash" ] || die "nothing built in $PREFIX/install: run ./install.sh"
[ -x "$VENV/bin/python" ] || die "no Python environment in $PREFIX: run ./install.sh"
[ -r "$CONFIG_DIR/gateway.toml" ] || die "$CONFIG_DIR/gateway.toml missing: run ./install.sh"
[ -r "$CONFIG_DIR/executor.yaml" ] || die "$CONFIG_DIR/executor.yaml missing: run ./install.sh"
if [ -z "$NAMESPACE" ]; then
    NAMESPACE="$(cat "$CONFIG_DIR/namespace" 2>/dev/null || echo cell1)"
fi

# The systemd services use domain 0 and the same executor name: two executors of one name in
# one domain would answer the same goals.
if [ "$DOMAIN" = 0 ] && systemctl is-active --quiet bt-executor.service 2>/dev/null; then
    die "the bt-executor service is running in ROS domain 0; stop it (sudo systemctl stop bt-server.target) or use --domain N"
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

pids=()
stop_all() {
    trap - INT TERM
    for pid in "${pids[@]}"; do kill -INT "$pid" 2>/dev/null || true; done
    for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done
}
trap 'stop_all; exit 0' INT TERM

python -m bt_server_executor --ros-args -r "__ns:=/$NAMESPACE" -r __node:=bt_executor \
    --params-file "$CONFIG_DIR/executor.yaml" &
pids+=("$!")
python -m bt_server_gateway --config "$CONFIG_DIR/gateway.toml" &
pids+=("$!")

host="$(sed -n 's/^http_host *= *"\(.*\)"/\1/p' "$CONFIG_DIR/gateway.toml")"
port="$(sed -n 's/^http_port *= *\([0-9]*\)/\1/p' "$CONFIG_DIR/gateway.toml")"
echo "run.sh: executor /$NAMESPACE/bt_executor (ROS domain $DOMAIN) and gateway on http://$host:$port/ ; Ctrl+C stops both"

# When one process ends by itself, stop the other.
status=0
wait -n "${pids[@]}" || status=$?
stop_all
exit "$status"
