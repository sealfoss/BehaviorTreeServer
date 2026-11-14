# Shared by the six scripts: install.sh, run.sh and setup_system_service.sh (Ubuntu 26.04), and
# jetson_install.sh, jetson_run.sh and jetson_setup_system_service.sh (a Jetson, in a container).
# Sourced, not run: the script sets PROG (its name, for messages) first. It sources lib/base.sh,
# the part install.sh needs before it has built anything. In the image it is
# /opt/bt_server/lib/common.sh, next to /opt/bt_server/run.sh, as here next to ./run.sh.
# shellcheck shell=bash
# shellcheck disable=SC2034  # (variables for the scripts that source this file)

# shellcheck source=SCRIPTDIR/base.sh
source "$(dirname "${BASH_SOURCE[0]}")/base.sh"

# The services, both kinds: the same user, folders and settings (design 14.2).
SERVICE_USER="btserver"
STATE_DIR="/var/lib/bt_server"
LOG_DIR="/var/log/bt_server"
CONF="/etc/bt_server"
UNIT_DIR="/etc/systemd/system"
EXECUTOR_SERVICE="bt-executor.service"          # setup_system_service.sh: the executor
GATEWAY_SERVICE="bt-gateway.service"            # setup_system_service.sh: the gateway
CONTAINER_SERVICE="bt-server-container.service" # jetson_setup_system_service.sh: both, in the container
NAME_SERVICE="bt-server-name.service"           # both kinds: bt.<hostname>.local and bt-<hostname>.local
TARGET="bt-server.target"

# The container (jetson_*.sh).
DEFAULT_IMAGE="bt-server:latest"
DEFAULT_TEST_IMAGE="bt-server-test:latest"
SERVICE_CONTAINER="bt-server"                   # the container of the service
RUN_CONTAINER="bt-server-run"                   # the container of jetson_run.sh


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

# ------------------------------------------------------------------------------ this machine
# The address of the default route, which browsers on the LAN connect to (empty without one).
lan_address() { ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' || true; }

# The interface of the default route (for the firewall).
lan_interface() { ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p' || true; }

# Whether DHCP gave this address (ip marks it "dynamic"): then the server needs a DHCP reservation,
# or its settings stop matching when the address changes (design 14.3).
address_is_dynamic() {
    local line
    line="$(ip -4 -o addr show 2>/dev/null | grep -F " inet $1/" || true)"
    [[ "$line" == *" dynamic"* ]]
}

# Whether this machine has the address now (after a new DHCP lease it may not).
address_is_mine() {
    local addresses
    addresses="$(ip -4 -o addr show 2>/dev/null || true)"
    [[ "$addresses" == *" inet $1/"* ]]
}

# Whether the gateway listens on every address rather than one.
listens_everywhere() { case "$1" in ""|0.0.0.0|::|"[::]") return 0 ;; esac; return 1; }

# The short host name, as in <hostname>.local, %l in a systemd unit and {hostname} in the
# gateway's public_names.
short_hostname() { hostname -s; }

# The default executor namespace: the host name as a ROS name part (lower case, other characters
# made "_", no "__", a letter first), so that executors of different machines in one ROS domain
# never share a name: orion's /orion/bt_executor, a machine "lab-2"'s /lab_2/bt_executor. An
# installation keeps the namespace it has (config/namespace, or the service's).
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


# ------------------------------------------------------------------------------ the settings
# The two settings files (design 8.1, 9.1), written the same way by all four install and setup
# scripts: gateway_settings WRITER ADDRESS PORT NAMESPACE TREES_DIR and executor_settings WRITER
# LOG_DIR JOURNAL_FILE print them.
gateway_settings() {
    cat <<EOF
# Settings of the gateway (design 9.1), written by $1 for $(short_hostname).
link = "ros"
http_host = "$2"                     # the LAN address, not 0.0.0.0
http_port = $3
public_names = ["bt.{hostname}.local", "bt-{hostname}.local", "{hostname}.local"]
executors = ["/$4/bt_executor"]
allowed_networks = []
trees_dir = "$5"
EOF
}

executor_settings() {
    cat <<EOF
# Parameters of the executor (design 8.1), written by $1 for $(short_hostname).
/**:
  ros__parameters:
    leaf_modules: ["bt_server_core.demo_leaves"]
    log_dir: "$2"
    journal_file: "$3"
EOF
}

# Values read back from those files (what the gateway and the executor will use).
gateway_value() {   # file, key: a string (http_host, trees_dir) or a number (http_port)
    case "$2" in
        http_port) sed -n 's/^http_port *= *\([0-9]*\).*/\1/p' "$1" | head -1 ;;
        *) sed -n "s/^$2 *= *\"\\([^\"]*\\)\".*/\\1/p" "$1" | head -1 ;;
    esac
}
executor_value() {  # file, key (log_dir, journal_file)
    sed -n "s/^ *$2: *\"\\{0,1\\}\\([^\"]*\\)\"\\{0,1\\} *\$/\\1/p" "$1" | head -1
}

# Whether gateway.toml lists /NS/bt_executor among its executors.
gateway_lists() { grep -Fq "\"/$2/bt_executor\"" "$1"; }

# Warnings for a gateway.toml that was kept (no --reconfigure) and says something else than was asked
# for. kept_gateway_warnings FILE ADDRESS PORT NAMESPACE FIX: an empty value is not compared.
kept_gateway_warnings() {
    local file="$1" address="$2" port="$3" namespace="$4" fix="$5" file_address
    file_address="$(gateway_value "$file" http_host)"
    if [ -n "$address" ] && [ "$file_address" != "$address" ]; then
        warn "$file is kept and has the address $file_address, not $address: $fix"
    fi
    if [ -n "$port" ] && [ "$(gateway_value "$file" http_port)" != "$port" ]; then
        warn "$file is kept and has another http_port than $port: $fix"
    fi
    if [ -n "$namespace" ] && ! gateway_lists "$file" "$namespace"; then
        warn "$file is kept and does not list /$namespace/bt_executor among its executors: $fix"
    fi
    if ! listens_everywhere "$file_address" && [[ "$file_address" != 127.* ]] && ! address_is_mine "$file_address"; then
        warn "$file has the address $file_address, which is not this machine's any more (a new DHCP lease?): $fix"
    fi
}

# The settings of a checkout, for run.sh or jetson_run.sh (install.sh, jetson_install.sh):
# config/gateway.toml, config/executor.yaml, config/namespace, and .data/ for the trees, logs and
# goal journal. Arguments: config dir, data dir, namespace (empty: the one kept, else the
# default), port, whether --port was given, whether --reconfigure was. Under sudo the files
# belong to the user who ran sudo, as whom the run scripts run.
write_checkout_settings() {
    local config="$1" data="$2" namespace="$3" port="$4" port_given="$5" reconfigure="$6"
    local namespace_given=1 address fix="run ./$PROG --reconfigure"
    if [ -z "$namespace" ]; then
        namespace_given=0
        namespace="$(head -1 "$config/namespace" 2>/dev/null || true)"
        [ -n "$namespace" ] || namespace="$(default_namespace)"
    fi
    address="$(lan_address)"
    [ -n "$address" ] || address="127.0.0.1"
    mkdir -p "$config" "$data/trees" "$data/log"
    if [ ! -f "$config/gateway.toml" ] || [ "$reconfigure" = 1 ]; then
        say "Writing $config/gateway.toml"
        gateway_settings "$PROG" "$address" "$port" "$namespace" "$data/trees" > "$config/gateway.toml"
    else
        local asked_port="" asked_namespace=""
        [ "$port_given" = 1 ] && asked_port="$port"
        [ "$namespace_given" = 1 ] && asked_namespace="$namespace"
        kept_gateway_warnings "$config/gateway.toml" "" "$asked_port" "$asked_namespace" "$fix"
    fi
    if [ ! -f "$config/executor.yaml" ] || [ "$reconfigure" = 1 ]; then
        say "Writing $config/executor.yaml"
        executor_settings "$PROG" "$data/log" "$data/goals.jsonl" > "$config/executor.yaml"
    fi
    if [ ! -f "$config/namespace" ] || [ "$namespace_given" = 1 ] || [ "$reconfigure" = 1 ]; then
        echo "$namespace" > "$config/namespace"
    fi
    if address_is_dynamic "$address"; then
        warn "$address came from DHCP: give this machine a DHCP reservation, or $fix when the address changes"
    fi
    if [ -d "$REPO/examples/trees" ] && [ -z "$(ls -A "$data/trees" 2>/dev/null)" ]; then
        cp -r "$REPO/examples/trees/." "$data/trees/"
    fi
    if [ "$(id -u)" = 0 ] && [ -n "${SUDO_UID:-}" ] && [ "$SUDO_UID" != 0 ]; then
        chown -R "$SUDO_UID:${SUDO_GID:-$SUDO_UID}" "$config" "$data"
    fi
}

# For run.sh and jetson_run.sh: the checkout's settings must still fit this machine.
check_run_settings() {   # config dir, the install script that fixes them
    local address
    address="$(gateway_value "$1/gateway.toml" http_host)"
    if ! listens_everywhere "$address" && [[ "$address" != 127.* ]] && ! address_is_mine "$address"; then
        die "the gateway's address $address (http_host in $1/gateway.toml) is not this machine's any more (a new DHCP lease?): run ./$2 --reconfigure"
    fi
}

# ------------------------------------------------------------------------------ the services' units
# Whether UNIT is up or coming up (also while Restart= waits to start it again).
unit_up() {
    case "$(systemctl is-active "$1" 2>/dev/null || true)" in
        active|activating|reloading|deactivating) return 0 ;;
    esac
    return 1
}

unit_installed() { [ -e "$UNIT_DIR/$1" ]; }

# A variable of UNIT's environment as systemd has it (drop-ins included), empty when not set or
# when systemctl cannot tell (systemd not running: a chroot, a container).
unit_env() {
    { systemctl show --property=Environment --value "$1" 2>/dev/null || true; } | tr ' ' '\n' | sed -n "s/^$2=//p" | tail -1
}

# The executor namespace of an executor unit: BT_NAMESPACE, or for a unit written before it was
# there, the __ns:= of its command.
unit_namespace() {
    local namespace
    namespace="$(unit_env "$1" BT_NAMESPACE)"
    if [ -z "$namespace" ]; then
        namespace="$({ systemctl show --property=ExecStart --value "$1" 2>/dev/null || true; } \
                     | sed -n 's/.*__ns:=\/\([A-Za-z0-9_]*\).*/\1/p' | head -1 || true)"
    fi
    printf '%s\n' "$namespace"
}

# For run.sh and jetson_run.sh: refuse when a service of this machine, of either kind, runs (or is
# restarting) the executor /NS/bt_executor in the domain: two would accept the same goals.
refuse_running_service() {   # namespace, domain
    local unit
    for unit in "$EXECUTOR_SERVICE" "$CONTAINER_SERVICE"; do
        if unit_runs "$unit" "$1" "$2"; then
            if [ "$(systemctl is-active "$unit" 2>/dev/null || true)" = deactivating ]; then
                die "the service $unit is still stopping (up to 30 s); try again in a moment"
            fi
            die "the service $unit runs /$1/bt_executor in ROS domain $2; stop it (sudo systemctl stop $TARGET), or use --namespace NS or --domain N"
        fi
    done
}

# For a setup run again: the namespace and domain the service has, so that it keeps them when no
# option says otherwise. Sets NAMESPACE and DOMAIN where they are empty. Argument: the executor unit.
keep_service_settings() {
    if [ -z "$NAMESPACE" ] && [ -r "$CONF/namespace" ]; then
        NAMESPACE="$(head -1 "$CONF/namespace")"
    fi
    if unit_installed "$1"; then
        [ -n "$NAMESPACE" ] || NAMESPACE="$(unit_namespace "$1")"
        [ -n "$DOMAIN" ] || DOMAIN="$(unit_env "$1" ROS_DOMAIN_ID)"
    fi
    [ -n "$NAMESPACE" ] || NAMESPACE="$(default_namespace)"
    DOMAIN="${DOMAIN:-0}"
}

# One kind of service per machine: they would serve the same port, settings and names.
refuse_other_kind() {   # the other kind's units, then the script that installed them
    local unit script="${*: -1}"
    for unit in "${@:1:$#-1}"; do
        if unit_installed "$unit"; then
            die "$UNIT_DIR/$unit is there: the services of $script are installed on this machine; remove them first"
        fi
    done
}

# The system user btserver, its home (trees, goal journal, the gateway's state) and the log folder.
# Sets SERVICE_UID and SERVICE_GID.
setup_service_user() {
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
}

# /etc/bt_server/gateway.toml, executor.yaml and namespace. Uses NAMESPACE, ADDRESS, PORT and
# RECONFIGURE; afterwards ADDRESS and PORT are the ones in gateway.toml, which a kept file may have
# other than asked for (a warning says so), NAME_ADDRESS is the address for the names ("auto" for a
# gateway on every address: the default route's when the name unit starts) and CHECK_ADDRESS the
# one the checks use.
write_service_settings() {
    local fix="run ./$PROG again with --reconfigure"
    "${SUDO[@]}" mkdir -p "$CONF"
    "${SUDO[@]}" chmod 755 "$CONF"
    if [ ! -f "$CONF/gateway.toml" ] || [ "$RECONFIGURE" = 1 ]; then
        say "Writing $CONF/gateway.toml"
        gateway_settings "$PROG" "$ADDRESS" "$PORT" "$NAMESPACE" "$STATE_DIR/trees" | "${SUDO[@]}" tee "$CONF/gateway.toml" >/dev/null
    else
        kept_gateway_warnings "$CONF/gateway.toml" "$ADDRESS" "$PORT" "$NAMESPACE" "$fix"
    fi
    if [ ! -f "$CONF/executor.yaml" ] || [ "$RECONFIGURE" = 1 ]; then
        say "Writing $CONF/executor.yaml"
        executor_settings "$PROG" "$LOG_DIR" "$STATE_DIR/goals.jsonl" | "${SUDO[@]}" tee "$CONF/executor.yaml" >/dev/null
    fi
    echo "$NAMESPACE" | "${SUDO[@]}" tee "$CONF/namespace" >/dev/null
    "${SUDO[@]}" chmod 644 "$CONF/gateway.toml" "$CONF/executor.yaml" "$CONF/namespace"
    ADDRESS="$(gateway_value "$CONF/gateway.toml" http_host)"
    PORT="$(gateway_value "$CONF/gateway.toml" http_port)"
    [ -n "$PORT" ] || die "$CONF/gateway.toml has no http_port: use --reconfigure"
    NAME_ADDRESS="$ADDRESS"
    CHECK_ADDRESS="$ADDRESS"
    if listens_everywhere "$ADDRESS"; then
        NAME_ADDRESS="auto"
        CHECK_ADDRESS="$(lan_address)"
    fi
    if address_is_dynamic "$CHECK_ADDRESS"; then
        warn "$CHECK_ADDRESS came from DHCP: give this machine a DHCP reservation, or $fix when the address changes"
    fi
}

# Writes UNIT_DIR/<unit> from a template, each @KEY@ replaced by UNIT_VALUES[KEY].
declare -A UNIT_VALUES=()
install_unit() {   # template file, unit name
    local key value script=()
    for key in "${!UNIT_VALUES[@]}"; do
        value="${UNIT_VALUES[$key]}"
        value="${value//\\/\\\\}"; value="${value//|/\\|}"; value="${value//&/\\&}"
        script+=(-e "s|@$key@|$value|g")
    done
    sed "${script[@]}" "$1" | "${SUDO[@]}" tee "$UNIT_DIR/$2" >/dev/null
    "${SUDO[@]}" chmod 644 "$UNIT_DIR/$2"
    if grep -q '@[A-Z_]\{1,\}@' "$UNIT_DIR/$2"; then
        die "$UNIT_DIR/$2: $(grep -o '@[A-Z_]\{1,\}@' "$UNIT_DIR/$2" | head -1) is not filled in"
    fi
}

# With ufw active, the gateway's port and mDNS are allowed on the LAN interface.
open_firewall() {   # port
    if command -v ufw >/dev/null && [ -n "$("${SUDO[@]}" ufw status 2>/dev/null | sed -n '/Status: active/p')" ]; then
        local interface
        interface="$(lan_interface)"
        say "ufw is active: allowing port $1/tcp and mDNS (5353/udp) on $interface"
        "${SUDO[@]}" ufw allow in on "$interface" to any port "$1" proto tcp
        "${SUDO[@]}" ufw allow in on "$interface" to any port 5353 proto udp
        echo "Note: ROS 2 traffic must be allowed too (design 14.4), for example: sudo ufw allow in on <robot interface>"
    fi
}

# ------------------------------------------------------------------------------ ROS discovery
# Discovery and middleware settings (design 14.4: a discovery server, a Fast DDS profile that keeps
# ROS off the LAN interface, ...). They reach run.sh's processes from the environment, the
# container of jetson_run.sh as docker run options, and a service from its unit (a drop-in).
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

# For a setup: the checks discover as the service does, with the discovery and middleware settings
# of its unit (a drop-in) and none of this shell's, which the service does not get.
use_unit_ros_env() {   # unit
    local name value
    for name in "${ROS_ENV_NAMES[@]}"; do
        unset "$name"
        if unit_installed "$1"; then
            value="$(unit_env "$1" "$name")"
            if [ -n "$value" ]; then export "$name=$value"; fi
        fi
    done
}

# The node names on the ROS network of a domain, one per line, found in about 5 s; fails when it
# cannot look. (--no-daemon: a ros2 daemon of another distribution, such as the Jetson's Humble,
# would answer wrongly; --sig-proxy=false: a Ctrl+C during the check ends docker run, and so the
# script, instead of only the check.)
ros_nodes_native() {   # domain
    env ROS_DOMAIN_ID="$1" timeout 60 bash -c 'source "$0" >/dev/null 2>&1 && ros2 node list --no-daemon --spin-time 5' \
        "$ROS_SETUP" 2>/dev/null
}
ros_nodes_container() {   # domain, image, uid:gid to run as (ros_env_options first)
    "${DOCKER[@]}" run --rm --sig-proxy=false "${CONTAINER_RUN_OPTIONS[@]}" --cap-drop ALL --user "$3" -e HOME=/tmp \
        -e ROS_DOMAIN_ID="$1" "${ROS_ENV_OPTIONS[@]}" --entrypoint /bin/bash "$2" \
        -c "source /opt/ros/$ROS_DISTRO_NAME/setup.bash && ros2 node list --no-daemon --spin-time 5" 2>/dev/null
}

# Whether a node of this exact name (such as /orion/bt_executor) is on the ROS network now: 0 yes,
# 1 no, 2 the check itself failed. Arguments: the node name, then the command that lists the nodes.
node_on_network() {
    local node="$1" nodes
    shift
    nodes="$("$@")" || return 2
    if grep -Fqx -- "$node" <<<"$nodes"; then return 0; fi
    return 1
}

# Before an executor starts: refuse when one of its name is on the ROS network of the domain
# already (another machine, or a service): two executors of one name would both accept the same
# goals. Arguments: namespace, domain, then the command that lists the nodes.
require_name_free() {
    local namespace="$1" domain="$2" status=0
    shift 2
    note "$PROG: looking for /$namespace/bt_executor on the ROS network (domain $domain)"
    node_on_network "/$namespace/bt_executor" "$@" || status=$?
    case "$status" in
        0) die "an executor /$namespace/bt_executor is on the ROS network of domain $domain already (another machine, or a service of this one); use --namespace NS or --domain N" ;;
        1) note "  none: the name is free" ;;
        *) warn "could not look for other executors; going on" ;;
    esac
}

# Whether UNIT runs (or is restarting) the executor /NAMESPACE/bt_executor in DOMAIN (a namespace
# that cannot be told counts as the same).
unit_runs() {   # unit, namespace, domain
    unit_up "$1" || return 1
    local domain namespace
    domain="$(unit_env "$1" ROS_DOMAIN_ID)"
    namespace="$(unit_namespace "$1")"
    [ "${domain:-0}" = "$3" ] && [ "${namespace:-$2}" = "$2" ]
}

# A setup, before it writes the settings and the units: the executor's name must be free on the ROS
# network of the domain. With --no-start nothing is looked for, and a warning says when the running
# service keeps other settings until it restarts. Otherwise the service is stopped first when it
# runs this very name (it would be the executor found; stopping also cancels a pending restart), and
# a service that runs another name is left running: after a refusal the settings, the units and
# the running services are as they were. (The build before the check, /opt/bt_server or the image,
# is new: the services run it from their next restart, with their old settings.) Uses NAMESPACE,
# DOMAIN and START; arguments: the executor unit, then the command that lists the nodes.
setup_name_check() {
    local unit="$1"
    shift
    if [ "$START" = 0 ]; then
        if unit_up "$unit" && ! unit_runs "$unit" "$NAMESPACE" "$DOMAIN"; then
            warn "the running service keeps /$(unit_namespace "$unit")/bt_executor in domain $(unit_env "$unit" ROS_DOMAIN_ID) until it restarts (sudo systemctl restart $TARGET); the new settings are not checked against the ROS network (--no-start)"
        fi
        echo "  (--no-start: the name /$NAMESPACE/bt_executor is not looked for on the ROS network)"
        return 0
    fi
    if unit_runs "$unit" "$NAMESPACE" "$DOMAIN"; then
        say "Stopping the running $unit (it has the executor /$NAMESPACE/bt_executor; if the name is in use elsewhere, it stays stopped)"
        "${SUDO[@]}" systemctl stop "$unit" 2>/dev/null || true
    fi
    echo
    require_name_free "$NAMESPACE" "$DOMAIN" "$@"
}

# ------------------------------------------------------------------------------ the container
# The options of docker run that both jetson_run.sh and the service use. The container shares
# the host's network (ROS finds other machines with multicast, and the gateway listens on the LAN
# address) and IPC (Fast DDS's shared memory with ROS programs on this machine), and runs on
# runc, not the NVIDIA runtime, which would mount JetPack's libraries into Ubuntu 26.04.
CONTAINER_RUN_OPTIONS=(--init --runtime runc --network host --ipc host --security-opt no-new-privileges)

# The host's time zone (the image has none: UTC), so that the times in the logs are this machine's.
TIMEZONE_OPTIONS=()
if [ -e /etc/localtime ]; then TIMEZONE_OPTIONS=(-v /etc/localtime:/etc/localtime:ro); fi

# ------------------------------------------------------------------------------ a setup's checks
# After a setup has started the services: each check prints a line, and one that fails sets
# CHECK_FAILED.
CHECK_FAILED=0
check_failed() { echo "  $*"; CHECK_FAILED=1; }

print_unit_states() {   # units
    local unit
    for unit in "$@"; do
        printf '  %-29s enabled=%-8s active=%s\n' "$unit" "$(systemctl is-enabled "$unit" 2>/dev/null || true)" \
            "$(systemctl is-active "$unit" 2>/dev/null || true)"
    done
}

check_units_running() {   # units (not the target)
    local unit
    for unit in "$@"; do
        systemctl is-active --quiet "$unit" || check_failed "$unit is not active (see: journalctl -u $unit)"
        systemctl is-enabled --quiet "$unit" || check_failed "$unit is not enabled"
    done
}

check_page() {   # address, port, where its log is
    local page="" _
    for _ in $(seq 1 60); do
        if page="$(curl -fsS --max-time 2 "http://$1:$2/" 2>/dev/null)"; then break; fi
        page=""
        sleep 1
    done
    if [ -n "$page" ] && grep -qi "<html" <<<"$page"; then
        echo "  the page answers on http://$1:$2/"
    else
        check_failed "the page does not answer on http://$1:$2/ (see: $3)"
    fi
}

check_executor() {   # namespace, domain, where its log is, then the command that lists the nodes
    local namespace="$1" domain="$2" log="$3" status=0
    shift 3
    node_on_network "/$namespace/bt_executor" "$@" || status=$?
    case "$status" in
        0) echo "  the executor /$namespace/bt_executor is on the ROS network (domain $domain)" ;;
        1) check_failed "the executor /$namespace/bt_executor is not on the ROS network (see: $log)" ;;
        *) check_failed "could not look for the executor on the ROS network" ;;
    esac
}

# Both names resolve, to this gateway (not to another machine of the same name), and the gateway
# accepts them (its public_names, with the host name filled in: the Host check, design 13).
check_names() {   # address, port
    local name resolved _
    for name in "bt.$(short_hostname).local" "bt-$(short_hostname).local"; do
        resolved=""
        for _ in $(seq 1 10); do
            resolved="$(timeout 5 avahi-resolve -4 -n "$name" 2>/dev/null || true)"
            [ -n "$resolved" ] && break
            sleep 1
        done
        if [ -z "$resolved" ]; then
            check_failed "$name is not published (see: journalctl -u ${NAME_SERVICE%.service})"
        elif [ "${resolved##*[[:space:]]}" != "$1" ]; then
            check_failed "$name leads to ${resolved##*[[:space:]]}, not to this gateway's $1 (another machine of the same name?)"
        else
            echo "  $name -> $1"
        fi
        if ! curl -fsS --max-time 5 -o /dev/null --resolve "${name,,}:$2:$1" "http://${name,,}:$2/" 2>/dev/null; then
            check_failed "the gateway does not answer for http://${name,,}:$2/ (Host check)"
        fi
    done
}

print_addresses() {   # address, port
    local suffix="" host
    host="$(short_hostname)"
    [ "$2" = 80 ] || suffix=":$2"
    echo "  http://bt-${host,,}.local$suffix/   (Windows, Linux)"
    echo "  http://bt.${host,,}.local$suffix/   (macOS, iOS)"
    echo "  http://$1$suffix/"
}
