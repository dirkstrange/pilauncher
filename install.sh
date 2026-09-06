#!/usr/bin/env bash
#
# Provision pilauncher on a fresh Raspberry Pi OS install.
#
# Idempotent: safe to re-run to pick up changes or repair a half-finished
# install. Run as the desktop user, not root, since everything lives in that
# user's systemd and compositor config.
#
#   ./install.sh
#
set -euo pipefail

PORT="${PILAUNCHER_PORT:-8800}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
LABWC_DIR="$HOME/.config/labwc"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '    ok: %s\n' "$*"; }
warn() { printf '    warning: %s\n' "$*" >&2; }
die()  { printf '\nerror: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- preflight
say "Checking the environment"

[ "$(id -u)" -ne 0 ] || die "run as your desktop user, not root or sudo"
command -v systemctl >/dev/null || die "systemd not found; this targets Raspberry Pi OS"
[ -f "$HERE/launcher.py" ] || die "launcher.py not next to install.sh; run this from the repo"

if ! systemctl --user show-environment >/dev/null 2>&1; then
  die "no systemd user session. Log in to the desktop once, then re-run."
fi
ok "running as $USER, repo at $HERE"

if [ "$XDG_SESSION_TYPE" = "tty" ] && ! pgrep -x labwc >/dev/null; then
  warn "labwc is not running. Installing anyway; the units start with the desktop session."
fi

# ------------------------------------------------------------------ packages
say "Installing packages"

NEEDED=()
for pkg in chromium libwidevinecdm0 curl wlopm; do
  dpkg -s "$pkg" >/dev/null 2>&1 || NEEDED+=("$pkg")
done

if [ ${#NEEDED[@]} -gt 0 ]; then
  echo "    installing: ${NEEDED[*]}"
  sudo apt-get update -qq
  sudo apt-get install -y "${NEEDED[@]}"
else
  ok "all packages already present"
fi

# The binary is chromium on Debian 13. chromium-browser still resolves in apt
# but installs an older deb12 build alongside it.
command -v chromium >/dev/null || die "chromium not on PATH after install"
ok "chromium $(chromium --version 2>/dev/null | awk '{print $2}')"

# The CDM ships at /opt/WidevineCdm, not under ~/.config/chromium.
if [ -f /opt/WidevineCdm/_platform_specific/linux_arm64/libwidevinecdm.so ]; then
  ok "Widevine CDM present at /opt/WidevineCdm"
else
  warn "Widevine CDM missing. DRM services will not present a player."
fi

# ------------------------------------------------------------- systemd units
say "Installing systemd user units"

mkdir -p "$UNIT_DIR"
for unit in pilauncher.service pilauncher-shell.service; do
  sed -e "s|__PILAUNCHER_DIR__|$HERE|g" \
      -e "s|__PILAUNCHER_PORT__|$PORT|g" \
      "$HERE/systemd/$unit" > "$UNIT_DIR/$unit"
  ok "$unit"
done

systemctl --user daemon-reload

# Units must survive without an active login, or they stop when the session
# ends and never come back on a headless boot.
loginctl enable-linger "$USER" >/dev/null 2>&1 || warn "could not enable linger"
ok "linger: $(loginctl show-user "$USER" --property=Linger --value 2>/dev/null || echo unknown)"

# -------------------------------------------------------------- compositor
say "Configuring labwc"

mkdir -p "$LABWC_DIR"

# A user rc.xml REPLACES the system defaults rather than extending them, so
# start from the system copy or the session loses Alt-Tab and the volume keys.
if [ ! -f "$LABWC_DIR/rc.xml" ]; then
  if [ -f /etc/xdg/labwc/rc.xml ]; then
    cp /etc/xdg/labwc/rc.xml "$LABWC_DIR/rc.xml"
    ok "seeded rc.xml from /etc/xdg/labwc/rc.xml"
  else
    printf '<?xml version="1.0"?>\n<labwc_config>\n  <keyboard>\n  </keyboard>\n</labwc_config>\n' > "$LABWC_DIR/rc.xml"
    warn "no system rc.xml found; wrote a minimal one"
  fi
fi

if grep -q 'pilauncher' "$LABWC_DIR/rc.xml"; then
  ok "keybinds already present"
else
  cp "$LABWC_DIR/rc.xml" "$LABWC_DIR/rc.xml.bak.$(date +%Y%m%d%H%M%S)"
  python3 - "$LABWC_DIR/rc.xml" "$HERE" <<'PY'
import io, sys
path, here = sys.argv[1], sys.argv[2]
text = io.open(path, encoding='utf-8').read()
# labwc does not expand ~ in Execute commands, so the path is absolute.
block = f'''
    <!-- pilauncher: close the active service window and return to the tiles.
         Kiosk windows swallow keystrokes, so this is bound at the compositor
         rather than in the page. A-F4 is a backstop that does not depend on
         the launcher daemon being healthy. -->
    <keybind key="A-Escape">
      <action name="Execute" command="{here}/back.sh" />
    </keybind>
    <keybind key="XF86HomePage">
      <action name="Execute" command="{here}/back.sh" />
    </keybind>
    <keybind key="A-F4">
      <action name="Close" />
    </keybind>
'''
marker = '<keyboard>\n'
if marker not in text:
    sys.exit('no <keyboard> section in rc.xml; add the keybinds by hand')
idx = text.index(marker) + len(marker)
io.open(path, 'w', encoding='utf-8', newline='\n').write(text[:idx] + block + text[idx:])
PY
  python3 -c "import xml.etree.ElementTree as ET,sys; ET.parse(sys.argv[1])" "$LABWC_DIR/rc.xml" \
    || die "rc.xml is not well-formed after edit; restore the .bak beside it"
  ok "keybinds added (Alt+Escape, Home, Alt+F4)"
fi

# Stop the display blanking mid-film. autostart follows the same XDG lookup as
# rc.xml: the first file found wins, so a user autostart REPLACES the system
# one rather than adding to it. Writing a bare wlopm line here would drop
# pcmanfm-pi, wf-panel-pi and kanshi from the session. Seed from the system
# copy first, exactly as with rc.xml above.
if [ -f "$LABWC_DIR/autostart" ] && grep -q wlopm "$LABWC_DIR/autostart"; then
  ok "screen blanking already handled"
else
  if [ ! -f "$LABWC_DIR/autostart" ] && [ -f /etc/xdg/labwc/autostart ]; then
    cp /etc/xdg/labwc/autostart "$LABWC_DIR/autostart"
    ok "seeded autostart from /etc/xdg/labwc/autostart"
  fi
  cat >> "$LABWC_DIR/autostart" <<AUTOSTART

# pilauncher: keep the display awake during playback.
wlopm --on '*'
AUTOSTART
  ok "added wlopm to labwc autostart"
fi

if pgrep -x labwc >/dev/null; then
  kill -HUP "$(pgrep -x labwc | head -1)" 2>/dev/null && ok "labwc config reloaded"
fi

# ---------------------------------------------------------------- start up
say "Starting services"

chmod +x "$HERE/launcher.py" "$HERE/back.sh" 2>/dev/null || true
systemctl --user enable --now pilauncher.service pilauncher-shell.service

for i in $(seq 1 30); do
  if curl -fsS -o /dev/null "http://127.0.0.1:$PORT/status" 2>/dev/null; then
    ok "daemon answering on port $PORT"
    break
  fi
  [ "$i" -eq 30 ] && die "daemon never came up. Check: journalctl --user -u pilauncher.service"
  sleep 1
done

COUNT=$(curl -fsS "http://127.0.0.1:$PORT/services.json" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')
ok "$COUNT tiles being served"

say "Done"
cat <<EOM
    Verify Widevine before trusting the streaming tiles: open
    https://bitmovin.com/demos/drm on this Pi and confirm the stream plays.

    Point Jellyfin and Plex at your own servers by editing services.json.
    No restart needed; the daemon rereads it per request.

    Alt+Escape returns to the tiles from inside a service window.
EOM
