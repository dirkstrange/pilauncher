# pilauncher

Turns a Raspberry Pi into a TV streaming box. You get a grid of tiles for
Netflix, Prime Video, Jellyfin and whatever else you add, navigable with arrow
keys from across the room. Picking one opens that service full screen in
Chromium with no browser chrome, so it behaves like an app rather than a web
page.

The Pi has no official app for most streaming services, so this runs their web
players instead. Read "Known ceilings" at the bottom before you build one.
Netflix tops out around 720p here and Apple TV barely works, and neither of
those is fixable from this end.

## What you need

- A Raspberry Pi 5 running Raspberry Pi OS (64-bit) with the desktop, which
  uses the labwc compositor. Older releases shipped Wayfire or X11 and the
  keyboard setup differs; see "The back button".
- A keyboard, at least for setup. Any remote that sends arrow keys and Enter
  works afterwards, including most HDMI-CEC TV remotes and air mice.
- Accounts for whichever services you plan to use. This launches their web
  players; it does not bypass anything or provide content of its own.

## How it fits together

- `launcher.py` runs a small HTTP server on `127.0.0.1:8800`. It serves the
  tile page and starts a Chromium window when you pick something.
- `index.html` is that page. Arrow keys move, Enter opens, Escape closes.
- `services.json` is the catalog of services. Edit it to add or remove tiles;
  nothing else in the code knows what Netflix is.
- `back.sh` closes whatever service is open. It gets bound to a hotkey.

The server binds to localhost, so nothing outside the Pi can reach it.

Each service runs with its own `--user-data-dir`, which means separate cookies
and logins per service, and a player that crashes cannot take the others with
it. Those profiles live under `~/.local/share/pilauncher/profiles/`.

## Install

```bash
git clone https://github.com/dirkstrange/pilauncher.git ~/pilauncher
cd ~/pilauncher
./install.sh
```

`install.sh` does everything the rest of this document describes by hand. It is
safe to re-run, which is how you apply unit changes after a `git pull`.

The sections below cover what it sets up and why. They matter when something
breaks, or when you are adapting this to another compositor. To install by hand:

```bash
sudo apt update
sudo apt install -y chromium libwidevinecdm0 curl wlopm
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
Environment=WAYLAND_DISPLAY=wayland-0
Environment=XDG_SESSION_TYPE=wayland
ExecStartPre=/bin/sh -c 'for i in $(seq 1 30); do [ -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY" ] && exit 0; sleep 1; done; echo "wayland socket never appeared" >&2; exit 1'
ExecStart=/usr/bin/python3 %h/pilauncher/launcher.py
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
```

The two `Environment` lines are not optional, and leaving them out fails in a
way that points at the launcher instead of the environment.

The compositor imports `WAYLAND_DISPLAY` into the systemd user environment
shortly after it starts, and a unit ordered `After=graphical-session.target`
can still start before that import lands. When it does, the daemon inherits no
display, so every Chromium it spawns picks the X11 backend and exits
immediately with `Missing X server or $DISPLAY`. The HTTP call still returns
`200` with a pid, because the process really was created; it just died a
moment later. Tiles stop opening and nothing in the launcher's own logs
explains why. Setting the variables explicitly takes the timing out of it, and
the `ExecStartPre` waits for the compositor socket rather than assuming it.

`~/.config/systemd/user/pilauncher-shell.service`

```ini
[Unit]
Description=pilauncher display shell
After=pilauncher.service
Requires=pilauncher.service
PartOf=graphical-session.target

[Service]
Environment=WAYLAND_DISPLAY=wayland-0
Environment=XDG_SESSION_TYPE=wayland
ExecStartPre=/bin/sh -c 'for i in $(seq 1 30); do curl -fsS -o /dev/null http://127.0.0.1:8800/status && exit 0; sleep 1; done; echo "daemon never came up" >&2; exit 1'
ExecStart=/usr/bin/chromium --kiosk --noerrdialogs --password-store=basic \
  --disable-infobars --disable-session-crashed-bubble \
  --user-data-dir=%h/.local/share/pilauncher/shell \
  http://127.0.0.1:8800
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
```

`--password-store=basic` mirrors the flag the daemon passes to every service
window. Leave it off and Chromium asks the Secret Service for an encryption
key. With autologin nothing ever unlocks the login keyring, since no password
is typed at login for PAM to hand along, so an "Unlock Keyring" dialog lands on
the TV at every boot. The shell stores no credentials. It only draws tiles.

The shell unit waits for the daemon to answer instead of sleeping a fixed three
seconds. A fixed sleep did work here, but only by accident: it pushed the shell
past the environment import. The daemon, starting at the same instant, is what
broke.

```bash
systemctl --user daemon-reload
systemctl --user enable --now pilauncher.service pilauncher-shell.service
loginctl enable-linger "$USER"   # so the units survive without an active login
```

## The back button

Kiosk windows swallow keystrokes, so the page cannot catch a "go home" key
once a service is open. Bind it at the compositor instead.

For labwc the binding goes in `~/.config/labwc/rc.xml` inside `<keyboard>`.
That file usually does not exist yet. A user `rc.xml` replaces the system
defaults outright rather than layering on top of them, so a file containing
only these two keybinds costs you Alt-Tab, the volume keys, and every other
stock binding. Start from the system copy:

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

Bind a second way out as well. `back.sh` talks to the launcher daemon,
so if that daemon ever wedges there is no keyboard route out of a fullscreen
kiosk window. A plain window close does not depend on it:

```xml
<keybind key="A-F4">
  <action name="Close" />
</keybind>
```

Neither labwc's defaults nor the Raspberry Pi OS config bind a close action, so
without this Alt+Escape is the only exit. Closing the launcher shell by mistake
is harmless, since systemd restarts it within a few seconds.

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
# A user autostart replaces the system one, so seed it before appending.
cp /etc/xdg/labwc/autostart ~/.config/labwc/autostart
echo "wlopm --on '*'" >> ~/.config/labwc/autostart
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
- Everything in `~/.config/labwc/` replaces its system counterpart rather than
  merging with it. That goes for `autostart` as well as `rc.xml`, so seed from
  `/etc/xdg/labwc/` before adding anything.
- Apple TV's web player is built around Safari and is unreliable here.
