#!/usr/bin/env bash
#
# Run a command as root on the Pi.
#
# Pi-MediaCenter has no passwordless sudo and SSH is key-only, so the sudo
# password is kept as PI_SSH_PW in the repo-root .env, which is gitignored and
# never copied to the Pi. This wraps the one safe way to use it: the password
# goes to sudo's stdin over the encrypted SSH channel and is never placed on a
# command line, where it would show up in ps output and shell history.
#
# Usage:
#   ./scripts/pi-sudo.sh waydroid init -s GAPPS
#   ./scripts/pi-sudo.sh systemctl enable --now waydroid-container

set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ENV_FILE="$REPO_ROOT/.env"
PI_HOST=${PI_SSH_ALIAS:-pi-mediacenter}

if [ $# -eq 0 ]; then
    echo "Usage: $0 <command to run as root on the Pi>" >&2
    exit 2
fi

if [ ! -f "$ENV_FILE" ]; then
    echo "No $ENV_FILE, so there is no sudo password to use." >&2
    exit 1
fi

# Read the value without exporting it or echoing it anywhere.
PW=$(grep -m1 '^PI_SSH_PW=' "$ENV_FILE" | cut -d= -f2- || true)
if [ -z "$PW" ]; then
    echo "PI_SSH_PW is not set in $ENV_FILE." >&2
    exit 1
fi

# -S reads the password from stdin, -p "" suppresses the prompt so it does not
# land in the middle of the command's own output.
printf '%s\n' "$PW" \
    | ssh -o BatchMode=yes "$PI_HOST" "sudo -S -p '' -- $*"
