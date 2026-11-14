#!/bin/bash
# Entry point of the container (container/Dockerfile): starts the executor and the gateway with
# /opt/bt_server/run.sh --prefix /opt/bt_server, which reads the settings in /opt/bt_server/config (mounted by
# jetson_run.sh or the service) and takes the options given after the image name, such as
# --namespace NS and --domain N.
#
# Started as root with BT_USER=<uid>:<gid>, as the service of jetson_setup_system_service.sh
# does, it first becomes that user and keeps only the right to use ports below 1024, for the
# gateway's port 80: what AmbientCapabilities=CAP_NET_BIND_SERVICE does for the services that
# run without a container (design 14.3). The container must have CAP_SETUID and CAP_SETGID for
# that step, CAP_NET_BIND_SERVICE to keep, and CAP_KILL: with --init, docker-init stays root and
# must be able to pass docker stop's SIGTERM on to run.sh, which then belongs to that user.
set -euo pipefail

if [ "$(id -u)" = 0 ] && [ -n "${BT_USER:-}" ]; then
    if ! [[ "$BT_USER" =~ ^[1-9][0-9]*:[0-9]+$ ]]; then
        echo "entrypoint: BT_USER must be <uid>:<gid> of a user other than root, not '$BT_USER'" >&2
        exit 2
    fi
    exec setpriv --reuid="${BT_USER%%:*}" --regid="${BT_USER##*:}" --clear-groups \
        --inh-caps=-all,+net_bind_service --ambient-caps=+net_bind_service -- "$0" "$@"
fi
# Site leaf modules (the settings' leaves/ folder: config/leaves for jetson_run.sh,
# /etc/bt_server/leaves for the service) can be named in executor.yaml's leaf_modules.
if [ -d /opt/bt_server/config/leaves ]; then
    export PYTHONPATH="/opt/bt_server/config/leaves${PYTHONPATH:+:$PYTHONPATH}"
fi
# --no-checks: jetson_run.sh and jetson_setup_system_service.sh look for the executor's name on
# the ROS network, and for the service, before they start the container; from inside it run.sh
# could not see this machine's services and address. --no-names: the container has no Avahi;
# jetson_run.sh and bt-server-name.service publish the names from the host.
exec /opt/bt_server/run.sh --prefix /opt/bt_server --no-checks --no-names "$@"
