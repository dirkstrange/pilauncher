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

# Chromium loads its CDM from the browser profile, not from /opt, so checking
# the package there proves nothing about whether DRM will work. What matters is
# the seed directory: without it a new profile fails DRM once, downloads a CDM,
# and only plays after it reopens.
SEED="$HOME/.local/share/pilauncher/widevine"
if [ -d "$SEED" ] && [ -n "$(ls -A "$SEED" 2>/dev/null)" ]; then
  ok "Widevine seed present, new profiles get a CDM immediately"
else
  warn "no Widevine seed at $SEED"
  warn "  a new service will fail DRM once, then work when reopened"
  warn "  see the Widevine section in README.md to populate it"
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

# The block is delimited so a re-run can replace it wholesale. Checking only
# for presence would leave an older install without keybinds added later.
if grep -q 'pilauncher:end' "$LABWC_DIR/rc.xml" && grep -q 'desktop.sh' "$LABWC_DIR/rc.xml"; then
  ok "keybinds already current"
else
  cp "$LABWC_DIR/rc.xml" "$LABWC_DIR/rc.xml.bak.$(date +%Y%m%d%H%M%S)"
  python3 "$HERE/scripts/labwc_keybinds.py" "$LABWC_DIR/rc.xml" "$HERE" || die "could not add keybinds; the .bak beside rc.xml is your original"
  ok "keybinds added (Alt+Escape, Home, Alt+F4, Ctrl+Alt+D)"
fi

# Stop the display blanking mid-film.
#
# labwc on Raspberry Pi OS runs with --merge-config, so a user autostart is
# read IN ADDITION TO /etc/xdg/labwc/autostart, not instead of it. Seeding this
# file from the system copy therefore starts pcmanfm-pi, wf-panel-pi and kanshi
# a second time, which shows up as two stacked taskbars. Only additions belong
# here.
if grep -qs 'wf-panel-pi\|pcmanfm-pi' "$LABWC_DIR/autostart"; then
  cp "$LABWC_DIR/autostart" "$LABWC_DIR/autostart.bak.$(date +%Y%m%d%H%M%S)"
  rm -f "$LABWC_DIR/autostart"
  warn "user autostart duplicated the system entries; rebuilt it (.bak kept)"
fi

if grep -qs wlopm "$LABWC_DIR/autostart"; then
  ok "screen blanking already handled"
else
  cat >> "$LABWC_DIR/autostart" <<AUTOSTART
# labwc here runs with --merge-config, so this file adds to
# /etc/xdg/labwc/autostart rather than replacing it. Additions only.

# pilauncher: keep the display awake during playback.
wlopm --on '*'
AUTOSTART
  ok "added wlopm to labwc autostart"
fi

if pgrep -x labwc >/dev/null; then
  kill -HUP "$(pgrep -x labwc | head -1)" 2>/dev/null && ok "labwc config reloaded"
fi

# ------------------------------------------------------- desktop shortcut
say "Adding the desktop shortcut"

# Exiting the launcher drops to the Pi desktop, so there has to be something
# there to start it again without a terminal.
APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"
cp "$HERE/pilauncher.desktop" "$APPS/pilauncher.desktop"
ok "menu entry at $APPS/pilauncher.desktop"

DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
if [ -d "$DESKTOP_DIR" ]; then
  cp "$HERE/pilauncher.desktop" "$DESKTOP_DIR/pilauncher.desktop"
  chmod +x "$DESKTOP_DIR/pilauncher.desktop"
  ok "desktop icon in $DESKTOP_DIR"
fi

update-desktop-database "$APPS" >/dev/null 2>&1 || true

# ---------------------------------------------------------------- start up
say "Starting services"

chmod +x "$HERE/launcher.py" "$HERE/back.sh" "$HERE/desktop.sh" 2>/dev/null || true
systemctl --user enable pilauncher.service pilauncher-shell.service

# Restart rather than start. On a re-run after a git pull the units are already
# up, and "start" on a running unit does nothing, so new code in launcher.py
# would not take effect and the script would report success anyway.
systemctl --user restart pilauncher.service pilauncher-shell.service

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
    To leave the launcher: pick "Exit to Desktop" from the last tile, or
    press Ctrl+Alt+D. To come back: the Media Launcher icon on the Pi
    desktop, or Ctrl+Alt+D again.
EOM
