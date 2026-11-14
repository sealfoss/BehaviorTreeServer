# Shared by jetson_install.sh, jetson_run.sh and jetson_setup_system_service.sh: sourced, not
# run. The script sets PROG (its name, for messages) first.
# shellcheck shell=bash

DEFAULT_IMAGE="bt-server:latest"
DEFAULT_TEST_IMAGE="bt-server-test:latest"
ROS_DISTRO_NAME="lyrical"
SERVICE_UNIT="bt-server-container.service"   # the container service of jetson_setup_system_service.sh
SERVICE_CONTAINER="bt-server"                # its container
RUN_CONTAINER="bt-server-run"                # the container of jetson_run.sh

say() { printf '\n==> %s\n' "$*"; }
warn() { printf '%s: warning: %s\n' "$PROG" "$*" >&2; }
die() { printf '%s: %s\n' "$PROG" "$*" >&2; exit 1; }
# A message that cannot fail, for what must go on when the terminal is gone (with SIGPIPE ignored).
note() { printf '%s\n' "$*" 2>/dev/null || true; }
# Option parsing: the option $1 needs a value, and $2 arguments are left ($# at the option).
need_value() { [ "$2" -ge 2 ] || { printf '%s: %s needs a value\n' "$PROG" "$1" >&2; exit 2; }; }

# SUDO: nothing for root; sudo prompts on the terminal, and for an unattended run point
# SUDO_ASKPASS at a program that prints the password: it is used instead (sudo -A).
setup_sudo() {
    if [ "$(id -u)" = 0 ]; then
        SUDO=()
    elif [ -n "${SUDO_ASKPASS:-}" ]; then
        SUDO=(sudo -A)
    else
        SUDO=(sudo)
    fi
}

# DOCKER: docker itself when this user may use the Docker daemon (root, or a member of the group
# docker), else through sudo. setup_sudo first.
setup_docker() {
    command -v docker >/dev/null || die "Docker is not installed: run ./jetson_install.sh"
    if docker info >/dev/null 2>&1; then
        DOCKER=(docker)
    else
        if [ ${#SUDO[@]} -gt 0 ]; then
            "${SUDO[@]}" -v || die "Docker needs sudo here, and sudo did not succeed"
        fi
        DOCKER=("${SUDO[@]}" docker)
        "${DOCKER[@]}" info >/dev/null 2>&1 || die "cannot use the Docker daemon (is docker.service running? systemctl status docker)"
    fi
}

image_exists() { "${DOCKER[@]}" image inspect "$1" >/dev/null 2>&1; }

container_running() {
    [ "$("${DOCKER[@]}" container inspect --format '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ]
}

# Whether the service unit is up or coming up (also while Restart= waits to start it again).
service_up() {
    case "$(systemctl is-active "$SERVICE_UNIT" 2>/dev/null || true)" in
        active|activating|reloading|deactivating) return 0 ;;
    esac
    return 1
}

# The address of the default route, which browsers on the LAN connect to (empty without one).
lan_address() { ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' || true; }

# Whether DHCP gave this address (ip marks it "dynamic"): then the server needs a DHCP reservation,
# or its settings stop matching when the address changes (design 14.3).
address_is_dynamic() {
    local line
    line="$(ip -4 -o addr show 2>/dev/null | grep -F " inet $1/" || true)"
    [[ "$line" == *" dynamic"* ]]
}

# Whether the gateway listens on every address rather than one.
listens_everywhere() { case "$1" in ""|0.0.0.0|::|"[::]") return 0 ;; esac; return 1; }

# The short host name, as in <hostname>.local, %l in a systemd unit and {hostname} in the
# gateway's public_names.
short_hostname() { hostname -s; }

# The default executor namespace: the host name as a ROS name part (lower case, other characters
# made "_", no "__", a letter first), so that executors of different machines in one ROS domain
# never share a name: MOTHER-BRAIN's /cell1/bt_executor and orion's /orion/bt_executor.
default_namespace() {
    local name
    name="$(short_hostname | tr '[:upper:]' '[:lower:]' | sed -e 's/[^a-z0-9_]/_/g' -e 's/__*/_/g' -e 's/^_//' -e 's/_$//')"
    case "$name" in
        "") name="cell1" ;;
        [a-z]*) ;;
        *) name="h$name" ;;
    esac
    printf '%s\n' "$name"
}

# A ROS name part: a letter, then letters, digits and single underscores.
valid_namespace() { [[ "$1" =~ ^[A-Za-z][A-Za-z0-9_]*$ ]] && [[ "$1" != *__* ]]; }

# A ROS domain as rcl reads it (in decimal: rcl would read "010" as octal).
valid_domain() { [[ "$1" =~ ^(0|[1-9][0-9]{0,2})$ ]] && [ "$1" -le 232 ]; }

valid_port() { [[ "$1" =~ ^[1-9][0-9]{0,4}$ ]] && [ "$1" -le 65535 ]; }

# The lowest port an ordinary user may listen on (1024 unless the system says otherwise).
unprivileged_port_start() { cat /proc/sys/net/ipv4/ip_unprivileged_port_start 2>/dev/null || echo 1024; }

# The options of docker run that both jetson_run.sh and the service use. The container shares
# the host's network (ROS finds other machines with multicast, and the gateway listens on the LAN
# address) and IPC (Fast DDS's shared memory with ROS programs on this machine), and runs on
# runc, not the NVIDIA runtime, which would mount JetPack's libraries into Ubuntu 26.04.
CONTAINER_RUN_OPTIONS=(--init --runtime runc --network host --ipc host --security-opt no-new-privileges)

# The host's time zone (the image has none: UTC), so that the times in the logs are this machine's.
TIMEZONE_OPTIONS=()
if [ -e /etc/localtime ]; then TIMEZONE_OPTIONS=(-v /etc/localtime:/etc/localtime:ro); fi

# Discovery and middleware settings that reach the container's ROS when they are set, as they
# reach run.sh's without a container (design 14.4: a discovery server, a Fast DDS profile that
# keeps ROS off the LAN interface, ...).
ROS_ENV_NAMES=(ROS_AUTOMATIC_DISCOVERY_RANGE ROS_STATIC_PEERS ROS_DISCOVERY_SERVER ROS_SUPER_CLIENT
               RMW_IMPLEMENTATION FASTDDS_DEFAULT_PROFILES_FILE FASTRTPS_DEFAULT_PROFILES_FILE
               FASTDDS_BUILTIN_TRANSPORTS CYCLONEDDS_URI)
# ROS_ENV_OPTIONS: the docker run options for those set in this environment; a profile file is
# mounted, read-only, at its own path.
ROS_ENV_OPTIONS=()
ros_env_options() {
    ROS_ENV_OPTIONS=()
    local name value
    for name in "${ROS_ENV_NAMES[@]}"; do
        value="${!name:-}"
        [ -n "$value" ] || continue
        case "$name" in
            FASTDDS_DEFAULT_PROFILES_FILE|FASTRTPS_DEFAULT_PROFILES_FILE)
                value="$(realpath -m -- "$value")"   # (the container has another working directory)
                if [ -f "$value" ]; then ROS_ENV_OPTIONS+=(-v "$value:$value:ro"); fi ;;
            CYCLONEDDS_URI)   # a file (path or file://), not XML in the variable itself
                local file="${value#file://}"
                if [[ "$file" != *"<"* ]] && [[ "$file" != *,* ]] && [ -f "$file" ]; then
                    file="$(realpath -- "$file")"
                    ROS_ENV_OPTIONS+=(-v "$file:$file:ro")
                    value="file://$file"
                fi ;;
        esac
        ROS_ENV_OPTIONS+=(-e "$name=$value")
    done
}

# Whether a node of this exact name (such as /orion/bt_executor) is on the ROS network of the
# domain now: 0 yes, 1 no, 2 the check itself failed. Two executors of one name in one domain
# would both accept the same goals. Arguments: image, domain, node name, uid:gid to run as.
# ros_env_options first, so that the check discovers as the executor will. (--sig-proxy=false: a
# Ctrl+C during the check ends docker run, and so the script, instead of only the check.)
node_on_network() {
    local nodes
    nodes="$("${DOCKER[@]}" run --rm --sig-proxy=false "${CONTAINER_RUN_OPTIONS[@]}" --cap-drop ALL --user "$4" -e HOME=/tmp \
        -e ROS_DOMAIN_ID="$2" "${ROS_ENV_OPTIONS[@]}" --entrypoint /bin/bash "$1" \
        -c "source /opt/ros/$ROS_DISTRO_NAME/setup.bash && ros2 node list --no-daemon --spin-time 5" 2>/dev/null)" || return 2
    if grep -Fqx -- "$3" <<<"$nodes"; then return 0; fi
    return 1
}
