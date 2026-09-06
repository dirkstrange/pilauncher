# pilauncher

A 10-foot launcher for Raspberry Pi OS. Tiles open streaming services as
fullscreen Chromium kiosk windows, each with its own browser profile.

Tested target: Raspberry Pi 5, Raspberry Pi OS (64-bit), labwc compositor.

## What it is

- `launcher.py` runs a small HTTP server on `127.0.0.1:8800`. It serves the
  tile UI and spawns one Chromium process per service.
- `index.html` is the UI. Arrow keys move, Enter opens, Escape closes.
- `services.json` is the catalog. Edit it; nothing else hardcodes services.
- `back.sh` closes the active service window. Bind it to a compositor hotkey.

Each service gets its own `--user-data-dir`, so logins do not collide and a
crash in one player cannot take the rest down. Profiles live under
`~/.local/share/pilauncher/profiles/`.

## Install

```bash
sudo apt update
sudo apt install -y chromium libwidevinecdm0 curl
mkdir -p ~/pilauncher
# copy launcher.py, index.html, services.json, back.sh into ~/pilauncher
chmod +x ~/pilauncher/launcher.py ~/pilauncher/back.sh
```

`libwidevinecdm0` is the part that makes Netflix, Prime Video, and the rest
load at all. Without it those sites will not present a player.

Verify the CDM registered:

```bash
ls /opt/WidevineCdm/_platform_specific/linux_arm64/
chromium --version
```

Then open `https://bitmovin.com/demos/drm` in Chromium and confirm the
Widevine stream plays. Do this before touching the launcher; if DRM is broken,
nothing downstream will work and you will waste time debugging the wrong layer.

## Run it by hand first

```bash
python3 ~/pilauncher/launcher.py
```

In a second terminal:

```bash
chromium --kiosk \
  --user-data-dir="$HOME/.local/share/pilauncher/shell" \
  http://127.0.0.1:8800
```

Confirm tiles render, arrows move focus, and Enter opens a service.

## Autostart

Two units: the daemon, and the launcher's own Chromium shell.

`~/.config/systemd/user/pilauncher.service`

```ini
[Unit]
Description=pilauncher daemon
After=graphical-session.target
PartOf=graphical-session.target

[Service]
ExecStart=/usr/bin/python3 %h/pilauncher/launcher.py
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
```

`~/.config/systemd/user/pilauncher-shell.service`

```ini
[Unit]
Description=pilauncher display shell
After=pilauncher.service
Requires=pilauncher.service
PartOf=graphical-session.target

[Service]
ExecStartPre=/bin/sleep 3
ExecStart=/usr/bin/chromium --kiosk --noerrdialogs \
  --disable-infobars --disable-session-crashed-bubble \
  --user-data-dir=%h/.local/share/pilauncher/shell \
  http://127.0.0.1:8800
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now pilauncher.service pilauncher-shell.service
loginctl enable-linger "$USER"   # so the units survive without an active login
```

## The back button

Kiosk windows swallow keystrokes, so the page cannot catch a "go home" key
once a service is open. Bind it at the compositor instead.

For labwc the binding goes in `~/.config/labwc/rc.xml` inside `<keyboard>`.
That file usually does not exist yet, and this is the part that catches people:
a user `rc.xml` replaces the system defaults outright rather than layering on
top of them. Writing a file containing only these two keybinds costs you
Alt-Tab, the volume keys, and every other stock binding. Start from the system
copy instead:

```bash
cp /etc/xdg/labwc/rc.xml ~/.config/labwc/rc.xml
```

Then add the keybinds inside the existing `<keyboard>` element. Use an absolute
path; labwc does not expand `~` here, and the user is whoever runs the session,
not necessarily `pi`:

```xml
<keybind key="A-Escape">
  <action name="Execute" command="/home/YOUR_USER/pilauncher/back.sh" />
</keybind>
<keybind key="XF86HomePage">
  <action name="Execute" command="/home/YOUR_USER/pilauncher/back.sh" />
</keybind>
```

Reload with `labwc --reconfigure`. That command reads `LABWC_PID` from its own
session, so it fails over SSH; from a remote shell send the signal directly
with `kill -HUP $(pgrep -x labwc)`. Confirm your compositor first:

```bash
echo "$XDG_SESSION_TYPE"
wlr-randr --version 2>/dev/null || echo "not wlroots"
ps -e | grep -E 'labwc|wayfire|Xorg'
```

Older Pi OS releases used Wayfire, and much older ones X11. The keybind syntax
differs for each. Most kiosk guides online are written for X11 and will not
apply.

## Screen blanking

```bash
sudo apt install -y wlopm
# labwc autostart file: ~/.config/labwc/autostart
wlopm --on '*'
```

Also disable the desktop screensaver in `raspi-config` under Display Options.

## Editing the catalog

```json
{
  "id": "example",
  "name": "Example",
  "url": "https://example.com",
  "color": "#3355FF",
  "ink": "#FFFFFF",
  "note": "Shown at the bottom of the screen when focused.",
  "user_agent": "optional override",
  "extra_flags": ["--optional-chromium-flag"],
  "hidden": false
}
```

Set `hidden` to `true` to park an entry. It stays in the file with its colours
and notes intact but is left out of the tile grid, which is the tidier option
for a service you have not set up yet. Hidden entries can still be launched by
id with a direct POST to `/launch`, which is useful for testing one before
putting it back on screen.

Changes take effect on the next launch; no restart needed, the daemon rereads
the file per request.

### user_agent

Several services gate on the user-agent string and refuse to serve a player to
Linux browsers. Hulu is the usual offender. Setting `user_agent` to a Windows
Chrome string will generally get past it. Doing so is a deliberate choice to
misrepresent your client to the service, which their terms of service prohibit.
The field is left empty in the shipped catalog so that is your call to make,
not a default you inherited.

## Known ceilings

- Widevine on the Pi is software L3 only. There is no TEE on the BCM2712, so
  L1 is unreachable. Netflix runs SD to 720p depending on title.
- The Pi 5 has no hardware H.264 decoder. Browser DRM streams fall to CPU
  decode. At 720p this is fine; there is not much headroom above it.
- The `plex` entry is hidden and still points at a placeholder host.
- Apple TV's web player is built around Safari and is unreliable here.
