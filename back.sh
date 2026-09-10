#!/usr/bin/env bash
# Close whatever service window is open and fall back to the launcher.
# Bind this to a compositor hotkey (see docs/TECHNICAL.md) so it works from
# inside a kiosk window, where page-level key handlers never fire.
set -euo pipefail
PORT="${PILAUNCHER_PORT:-8800}"
curl -fsS -X POST -H 'Content-Type: application/json' -d '{}' \
  "http://127.0.0.1:${PORT}/close" >/dev/null || {
    # Launcher daemon unreachable. Kill the service windows directly, but
    # leave the launcher's own Chromium alone.
    pkill -f 'user-data-dir=.*/pilauncher/profiles/' || true
  }
