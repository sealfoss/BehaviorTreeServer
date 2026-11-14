# The part of lib/common.sh that install.sh needs before it has built anything, also in the image's
# dependency layer (container/Dockerfile): kept apart, and small, so that a change to the rest of
# the scripts' shared code does not make Docker download ROS again. lib/common.sh sources it;
# install.sh sources it first and lib/common.sh after the dependencies. The script sets PROG
# (its name, for messages) first.
# shellcheck shell=bash
# shellcheck disable=SC2034  # (variables for the scripts that source this file)

ROS_DISTRO_NAME="lyrical"
ROS_SETUP="/opt/ros/$ROS_DISTRO_NAME/setup.bash"
PACKAGES=(bt_server_interfaces bt_server_core bt_server_executor bt_server_gateway bt_server_bringup)

# ------------------------------------------------------------------------------ messages
say() { printf '\n==> %s\n' "$*"; }
warn() { printf '%s: warning: %s\n' "$PROG" "$*" >&2; }
die() { printf '%s: %s\n' "$PROG" "$*" >&2; exit 1; }
# A message that cannot fail, for what must go on when the terminal is gone (with SIGPIPE ignored).
note() { printf '%s\n' "$*" 2>/dev/null || true; }
# Option parsing: the option $1 needs a value, and $2 arguments are left ($# at the option).
need_value() { [ "$2" -ge 2 ] || { printf '%s: %s needs a value\n' "$PROG" "$1" >&2; exit 2; }; }
unknown_option() { printf '%s: unknown option %s (see --help)\n' "$PROG" "$1" >&2; exit 2; }

# ------------------------------------------------------------------------------ sudo and Docker
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

# setup_sudo, then ask for the password now rather than in the middle of the work.
require_sudo() {
    setup_sudo
    if [ ${#SUDO[@]} -gt 0 ]; then
        "${SUDO[@]}" -v || die "this script needs sudo"
    fi
}

# ------------------------------------------------------------------------------ options
# A ROS name part: a letter, then letters, digits and single underscores.
valid_namespace() { [[ "$1" =~ ^[A-Za-z][A-Za-z0-9_]*$ ]] && [[ "$1" != *__* ]]; }

# A ROS domain as rcl reads it (in decimal: rcl would read "010" as octal).
valid_domain() { [[ "$1" =~ ^(0|[1-9][0-9]{0,2})$ ]] && [ "$1" -le 232 ]; }

valid_port() { [[ "$1" =~ ^[1-9][0-9]{0,4}$ ]] && [ "$1" -le 65535 ]; }

# The lowest port an ordinary user may listen on (1024 unless the system says otherwise).
unprivileged_port_start() { cat /proc/sys/net/ipv4/ip_unprivileged_port_start 2>/dev/null || echo 1024; }

# The options' checks, with the same messages in every script.
check_namespace() { valid_namespace "$1" || die "the namespace must be a ROS name part (letters, digits, single _), not '$1'"; }
check_domain() { valid_domain "$1" || die "--domain must be a number from 0 to 232, not '$1'"; }
check_port() { valid_port "$1" || die "--port must be a port number, not '$1'"; }
