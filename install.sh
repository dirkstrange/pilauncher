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
  warn "  see the Widevine section in docs/TECHNICAL.md to populate it"
fi

# ------------------------------------------------------------- systemd units
say "Installing systemd user units"

mkdir -p "$UNIT_DIR"
for unit in pilauncher.service pilauncher-shell.service waydroid-session.service \
            pilauncher-health.service pilauncher-health.timer; do
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

# Whether a user rc.xml REPLACES the system one or is read IN ADDITION to it
# depends on how labwc was started, and getting it wrong is visible either way.
#
# With -m (--merge-config), which is how Raspberry Pi OS starts it, both files
# are read. Seeding the system copy into the user file then defines every stock
# keybind twice and autostarts pcmanfm-pi, wf-panel-pi and kanshi twice, which
# shows up as two stacked taskbars.
#
# Without -m the first file found wins, and a user file that holds only
# additions costs the session its stock keybinds, the volume keys included.
#
# The man page describes the second case, so read the running process rather
# than trusting the documentation.
LABWC_PID=$(pgrep -x labwc 2>/dev/null | head -1 || true)
MERGE_CONFIG=unknown
if [ -n "$LABWC_PID" ] && [ -r "/proc/$LABWC_PID/cmdline" ]; then
  if tr '\0' ' ' < "/proc/$LABWC_PID/cmdline" | grep -qE '(^| )(-m|--merge-config)( |$)'; then
    MERGE_CONFIG=yes
  else
    MERGE_CONFIG=no
  fi
fi

if [ ! -f "$LABWC_DIR/rc.xml" ]; then
  if [ "$MERGE_CONFIG" = no ] && [ -f /etc/xdg/labwc/rc.xml ]; then
    cp /etc/xdg/labwc/rc.xml "$LABWC_DIR/rc.xml"
    ok "seeded rc.xml from /etc/xdg/labwc/rc.xml (labwc is not merging configs)"
  else
    cat > "$LABWC_DIR/rc.xml" <<'RCXML'
<?xml version="1.0" encoding="UTF-8"?>
<openbox_config xmlns="http://openbox.org/3.4/rc">
  <!-- labwc here runs with the merge-config option, so this file is read in
       addition to /etc/xdg/labwc/rc.xml. Only pilauncher's own additions
       belong here; copying the system file in defines every stock binding
       twice and gives you two taskbars. -->
  <keyboard>
  </keyboard>
</openbox_config>
RCXML
    if [ "$MERGE_CONFIG" = yes ]; then
      ok "wrote a minimal rc.xml (labwc is merging configs, so this file only adds)"
    else
      warn "could not tell whether labwc merges configs; assumed it does"
      warn "  if the stock keybinds stop working, seed $LABWC_DIR/rc.xml from /etc/xdg/labwc/rc.xml"
    fi
  fi
fi

# The block is delimited so a re-run can replace it wholesale. Checking only
# for presence would leave an older install without keybinds added later.
if grep -q 'pilauncher:end' "$LABWC_DIR/rc.xml" \
  && grep -q 'desktop.sh' "$LABWC_DIR/rc.xml" \
  && grep -q 'windowRule identifier="waydroid' "$LABWC_DIR/rc.xml"; then
  ok "keybinds and window rules already current"
else
  cp "$LABWC_DIR/rc.xml" "$LABWC_DIR/rc.xml.bak.$(date +%Y%m%d%H%M%S)"
  python3 "$HERE/scripts/labwc_keybinds.py" "$LABWC_DIR/rc.xml" "$HERE" || die "could not add keybinds; the .bak beside rc.xml is your original"
  ok "keybinds added (Alt+Escape, Home, Alt+F4, Ctrl+Alt+D) and the waydroid fullscreen rule"
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

# ------------------------------------------------------------------- remote
say "Configuring the remote control"

# The OK button on the G20-style remote emits a key that carries no xkb keysym,
# so it has to be rewritten in evdev before any client sees it. The rule file
# carries the full reasoning.
HWDB_RULE=/etc/udev/hwdb.d/70-pilauncher-remote.hwdb
if sudo install -o root -g root -m 0644 "$HERE/udev/70-pilauncher-remote.hwdb" "$HWDB_RULE"; then
  sudo systemd-hwdb update
  # The compiled database is only consulted when a device is added, so a remote
  # that is already plugged in keeps its old keymap until something re-triggers
  # it. Without this the fix appears to do nothing until the next reboot.
  sudo udevadm trigger --subsystem-match=input --action=change
  ok "installed $HWDB_RULE"
else
  warn "could not install the remote keymap; the OK button will not select"
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

chmod +x "$HERE/launcher.py" "$HERE/back.sh" "$HERE/desktop.sh" \
         "$HERE/scripts/pilauncher-health.py" 2>/dev/null || true
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

# ------------------------------------------------------------------- health
# The Pi asks each HDMI port what it can do once, while PipeWire starts. Boot
# with the TV off or on another input and it decides there is no sound card at
# all, then never asks again, so the box comes up silent and stays silent. The
# timer notices and repairs it a minute after the TV wakes up.
say "Enabling the health check"
systemctl --user enable pilauncher-health.timer
systemctl --user restart pilauncher-health.timer
if systemctl --user is-active --quiet pilauncher-health.timer; then
  ok "health check running every minute"
else
  warn "health check timer did not start; check: systemctl --user status pilauncher-health.timer"
fi

# ------------------------------------------------------------------- waydroid
# Android is part two of the build rather than an extra, but it cannot be done
# from here: it needs boot-configuration decisions and a Google sign-in, and an
# installer making those silently would be the wrong kind of helpful. The unit
# file is written either way above and only ENABLED once waydroid is actually
# installed and initialised, so a box that has not got there yet carries an
# inert unit rather than one that fails on every boot.
if command -v waydroid >/dev/null 2>&1 && [ -f /var/lib/waydroid/waydroid.cfg ]; then
  say "Enabling the Android session"

  # Stopping an Android app needs "waydroid shell", which is root only. Rather
  # than give the daemon blanket sudo, install one small root-owned helper and
  # allow exactly that path. Kept out of the checkout deliberately: a NOPASSWD
  # rule pointing into a user-writable directory is a rule to run anything.
  HELPER=/usr/local/sbin/pilauncher-waydroid-stop
  sudo install -o root -g root -m 0755 "$HERE/scripts/pilauncher-waydroid-stop" "$HELPER"
  ok "installed $HELPER"

  SUDOERS=/etc/sudoers.d/pilauncher
  # Write via a temp file and check it before installing. A malformed file in
  # sudoers.d breaks sudo for everything, including the sudo needed to fix it.
  TMP_SUDOERS=$(mktemp)
  printf '%s ALL=(root) NOPASSWD: %s\n' "$USER" "$HELPER" > "$TMP_SUDOERS"
  if sudo visudo -cf "$TMP_SUDOERS" >/dev/null 2>&1; then
    sudo install -o root -g root -m 0440 "$TMP_SUDOERS" "$SUDOERS"
    ok "sudoers rule for $HELPER"
  else
    warn "generated sudoers rule failed validation; not installing it"
    warn "  Android tiles will launch but will not be able to close"
  fi
  rm -f "$TMP_SUDOERS"

  systemctl --user enable waydroid-session.service
  systemctl --user restart waydroid-session.service
  # First boot of Android takes a while; later starts are quicker.
  for i in $(seq 1 40); do
    if waydroid status 2>/dev/null | grep -q "Session:.*RUNNING"; then
      ok "Android session running"
      break
    fi
    [ "$i" -eq 40 ] && warn "Android session did not report RUNNING; check: journalctl --user -u waydroid-session.service"
    sleep 3
  done
else
  # Deliberately a warning rather than a "say". This used to be an ordinary
  # progress line and it scrolled past in a long install, which is how a
  # from-scratch build got signed off with every Android tile dead. If the
  # catalog has Android tiles, this is the reason none of them open.
  warn "Waydroid is NOT installed. Part two of the build is not done yet, so"
  warn "  no Android tile will open. The web tiles work in the meantime."
  warn "  Steps: docs/TECHNICAL.md, \"Android apps through Waydroid\"."
  warn "  Re-run this script afterwards to enable the Android session."
fi

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

    On the remote: OK opens a tile, Home returns to the tiles, the back
    arrow and the mic button both go back a page, and Menu toggles the Pi
    desktop. Mouse mode turns the D-pad into a pointer, OK into a left
    click and the back arrow into a right click, which is why the mic
    button carries Back as well: it is the only one that works in both.
EOM
