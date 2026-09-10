#!/usr/bin/env bash
#
# Toggle the launcher shell so you can get to the Pi desktop and back.
#
# Stopping the shell reveals the desktop underneath. Compositor keybinds keep
# working there, so the same key starts it again. The daemon is left running
# either way, since it holds no display state and costs nothing idle.
#
# Bind this to a compositor hotkey (see docs/TECHNICAL.md).
set -euo pipefail

UNIT=pilauncher-shell.service
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if systemctl --user is-active --quiet "$UNIT"; then
  # Close any open service window first. Without this it would be left on
  # screen with no launcher behind it and no obvious way back.
  "$HERE/back.sh" >/dev/null 2>&1 || true
  systemctl --user stop "$UNIT"
else
  systemctl --user start "$UNIT"
fi
