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
- `desktop.sh` switches between the launcher and the Pi desktop, also bound
  to a hotkey.

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

## Widevine and DRM

Netflix, Prime Video, Max and the rest need Widevine to present a player at
all. This is worth understanding before anything else, because when it breaks
the services blame themselves and you will debug the wrong layer for an hour.

Chromium loads a Widevine CDM from the browser profile it was started with,
under `<profile>/WidevineCdm`, found through a hint file that stores an
absolute path. It does not use the system-wide CDM that the `libwidevinecdm0`
package installs at `/opt/WidevineCdm`. That package is still worth having for
other browsers, but Chromium ignores it.

The CDM arrives on its own: Chromium's component updater downloads one per
profile, in the background, on first run. The catch is that it only takes
effect the next time that profile opens, so a brand new service fails DRM once
and then works. `launcher.py` avoids that by seeding each new profile from
`~/.local/share/pilauncher/widevine` if a copy is there. To populate that
directory, copy `WidevineCdm` out of any profile that already has one, and
leave the hint file behind:

```bash
cp -a ~/.local/share/pilauncher/profiles/netflix/WidevineCdm/.       ~/.local/share/pilauncher/widevine/
rm -f ~/.local/share/pilauncher/widevine/latest-component-updated-widevine-cdm
```

Copying a CDM between profiles by hand needs that hint file rewritten to the
new absolute path, or the copy quietly points back at where it came from.

To check whether a profile has a working CDM, ask Chromium rather than the
streaming service:

```bash
chromium --headless=new --enable-logging=stderr --v=1   --user-data-dir=~/.local/share/pilauncher/profiles/netflix   about:blank 2>&1 | grep -i widevine
```

`Registering hinted Widevine 4.10.3057.0` means it is fine. `Widevine enabled
but no library found` means there is no CDM in that profile. Netflix reports
the second case as a message telling you to visit
`chrome://settings/content/protectedContent` and enable protected content.
That advice is a dead end; the setting is not the problem.

For an end-to-end check, open `https://bitmovin.com/demos/drm` in Chromium and
confirm the stream plays.

Avoid `--disable-component-update`. It looks like a reasonable way to stop
Chromium changing the CDM underneath you, and under Chromium 149 it genuinely
fixed E100 playback failures. Under 152 it prevents a fresh profile from ever
getting a CDM, which takes DRM out entirely.

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

## The remote control

The Pi is driven from a G20-style air-mouse remote, the kind with a D-pad and
media keys on one face and a numeric keypad on the other, sold under a rotating
cast of brand names. Ours talks to the Pi over its own 2.4GHz dongle rather than
Bluetooth. That dongle enumerates as USB `4842:0001` and presents two input
devices: a keyboard node carrying the buttons, and a mouse node carrying the
gyro pointer.

Most of the buttons need nothing from us. The arrows, volume, play/pause,
previous and next, page up and down, mute, the number row and the Del key all
emit ordinary evdev codes that Chromium understands, and `index.html` already
walks the tile grid from the arrow keys.

### Why OK arrived dead

The OK button reports HID consumer usage `0x0c/0x41`, which the kernel
translates into `KEY_SELECT`. The xkb evdev keycode table in
`/usr/share/X11/xkb/keycodes/evdev` has an entry for that code, but nothing in
`/usr/share/X11/xkb/symbols/` binds a keysym to it. The keypress therefore
reaches every Wayland client without a name attached. Chromium never raises an
`Enter` DOM event, the handler in `index.html` never matches, and the tile under
the cursor never opens.

No binding downstream can recover this. By the time labwc or the page sees the
event, the key is already nameless, so the repair has to happen in evdev while
the raw scancode is still in hand. That is what
[udev/70-pilauncher-remote.hwdb](udev/70-pilauncher-remote.hwdb) does. hwdb
matches on the scancode the hardware sent rather than the code the kernel picked
for it, which is why the rule reads `KEYBOARD_KEY_c0041=enter`. The syntax is
documented in [systemd's hwdb man page](https://www.freedesktop.org/software/systemd/man/latest/hwdb.html).

`install.sh` puts the rule in place. By hand it is three commands:

```bash
sudo install -o root -g root -m 0644 \
  udev/70-pilauncher-remote.hwdb /etc/udev/hwdb.d/70-pilauncher-remote.hwdb
sudo systemd-hwdb update
sudo udevadm trigger --subsystem-match=input --action=change
```

The third one is easy to skip and the reason nothing appears to happen without
it. The compiled database is consulted when a device is added, so a remote that
is already plugged in keeps its old keymap until something re-triggers it.

A rule can be checked before it goes anywhere near the system, by compiling it
into a throwaway root and querying it with the remote's real device signature:

```bash
mkdir -p /tmp/hwdbtest/etc/udev/hwdb.d
cp udev/70-pilauncher-remote.hwdb /tmp/hwdbtest/etc/udev/hwdb.d/
systemd-hwdb update --root /tmp/hwdbtest
systemd-hwdb query --root /tmp/hwdbtest "evdev:$(cat /sys/class/input/input4/modalias)"
```

A match prints the properties that would be applied. Silence means the match
line is wrong.

### The rest of the buttons

The back arrow sends `XF86Back`. It is deliberately not bound in labwc, so that
inside a service window Chromium's own history-back still works and the window
survives. `index.html` treats it like Escape, so on the tile page it still
cancels a move or closes.

Home sends `XF86HomePage` and is bound to `back.sh`, which is the one button
that always returns to the tiles.

The Menu button, bottom right, the one that also toggles the backlight when
held, sends the `Menu` keysym and is bound to `desktop.sh`. This matters more
than it looks: the remote has no Ctrl or Alt key, so `A-Escape`, `A-F4` and
`C-A-d` cannot be typed on it at all, and without this there is no way to reach
the Pi desktop from the couch.

The mic button ships sending `XF86VoiceCommand`, which is a perfectly bindable
key, but it is remapped to Back in the same hwdb rule as OK. A second back
button reads like padding until you turn mouse mode on, which is when the
remote's own back arrow stops being a key at all. The next section explains why
that matters more than it sounds.

### Mouse mode changes what several buttons send

The cursor button toggles the gyro pointer. That toggle is handled inside the
remote and sends nothing to the Pi, so it never shows up in a capture. While it
is on, the D-pad drives the pointer rather than the tile cursor, OK sends a left
click instead of a key, and the back arrow sends a right click. Tiles still
open, by being clicked rather than selected, which is what made the OK button
look intermittently broken rather than mode-dependent while it was unmapped.

Neither mode is the right one for everything. The launcher's own tiles are
built for a D-pad and want mouse mode off. Netflix's main page is the opposite:
resting the selection on a tile expands it into a panel of buttons, which then
traps the selection with no keyboard way out other than Escape, so browsing it
in practice needs the pointer. Moving the pointer collapses an expanded tile on
its own, so Escape is only useful in the mode you would not be using anyway.

That split is the reason the mic button carries Back. Mouse mode is where you
end up for browsing, and mouse mode is exactly where the back arrow stops being
a key, so without the remap there is no way back while the pointer is live.
Playback and ordinary pages honour the back arrow normally once mouse mode is
off.

### The power button really does power off

It sends `KEY_POWER` and the Pi acts on it immediately, with no confirmation
step. Worth knowing before testing buttons one at a time.

### Working out what a different remote sends

[scripts/show-remote-keys.py](scripts/show-remote-keys.py) watches every
readable node under `/dev/input` at once and prints the evdev key name and
hardware scancode for each press, naming the device the press came from. Being
in the `input` group is enough to run it, so it needs no sudo:

```bash
id -nG | grep -q input && ./scripts/show-remote-keys.py
```

A button that prints nothing at all is handled in the remote's firmware and
never reaches the host. A button that prints a key name but does nothing in
Chromium is the `KEY_SELECT` case above. To confirm, check whether a keysym
exists for it, remembering that xkb keycodes are the evdev code plus eight:

```bash
grep -rn "<I361>" /usr/share/X11/xkb/symbols/
```

No output means no keysym, and an hwdb remap is the fix.

## Settings

The gear in the top corner opens `/edit`, where tiles are added, changed,
hidden and removed. It is a page in its own right rather than a panel drawn
over the tiles, which is what lets the same editor answer a browser on another
machine.

A web service needs a name and an address. "Fetch logo and colours" then reads
the site for the largest icon it advertises, using the lookup in
[scripts/fetch_logos.py](scripts/fetch_logos.py), and guesses the three tile
colours from that image. Guesses only fill blanks, so a colour typed by hand
survives one. Some sites publish nothing usable, Netflix among them, and those
want an uploaded image instead.

Android tiles work differently, because there is no site to read a logo from
and a package name one character out fails at launch with nothing on screen to
explain why. The app is picked from a list of what Waydroid currently has
installed rather than typed. Anything installed from the Play Store appears in
that list, which is the intended route for adding one: install it in Android,
then add a tile for it here.

Deleting a tile takes its logo with it. Hiding one keeps every setting and only
takes it off the grid.

### Editing from another machine

The daemon listens on `127.0.0.1` alone by default, so the settings page exists
only on the Pi. `PILAUNCHER_BIND=0.0.0.0` in the service unit opens it to the
house network at `http://<pi>:8800/edit`.

What that does not open is control of the TV. `/launch`, `/close` and
`/desktop` check the address a request came from and refuse anything that is
not the Pi itself, whatever the daemon is bound to. Someone on the network can
edit the catalog and cannot start playing something in the living room.

### How the catalog is written

`services.json` is the entire launcher, so a half-written file costs every tile
at once. Writes land in a temporary file, get an fsync, and are renamed over
the original, which is atomic within one filesystem. The previous version stays
as `services.json.bak`, which is what to restore from when the mistake was in
the content rather than in the writing.

The daemon rereads the catalog on every request, so a saved change shows up on
the next paint without restarting anything.

## Leaving the launcher

The launcher covers the desktop and restarts itself if you close its window, so
there are two deliberate ways out and both come back the same way.

From the couch, arrow up to the gear, open it, and choose "Exit to the Pi
desktop". It posts to `/desktop`, which closes any open service and stops the
shell unit. The Pi desktop is underneath. `Ctrl+Alt+D` does the same from
anywhere, including from inside a service window, and so does the Menu button
on the remote.

To get back, use the Media Launcher icon on the Pi desktop, or press
`Ctrl+Alt+D` again. Compositor keybinds keep working at the desktop, so the one
key covers both directions.

The daemon keeps running the whole time. Only the Chromium window that draws
the tiles stops, which is why returning is close to instant and why the desktop
shortcut only has to start one unit:

```bash
systemctl --user start pilauncher-shell.service
```

That is also the command to run over SSH if you ever end up somewhere the
keybinds do not reach.

## Screen blanking

```bash
sudo apt install -y wlopm
echo "wlopm --on '*'" >> ~/.config/labwc/autostart
```

Raspberry Pi OS starts labwc with `--merge-config`, which makes a user
`autostart` run in addition to `/etc/xdg/labwc/autostart` rather than instead
of it. Put only your own additions in the user file. Copying the system entries
into it starts `pcmanfm-pi`, `wf-panel-pi` and `kanshi` a second time, and two
stacked taskbars on the desktop is what that looks like.

Also disable the desktop screensaver in `raspi-config` under Display Options.

## Editing the catalog

The gear in the corner covers all of this now, and validates as it goes. What
follows is the file it writes, for anyone editing `services.json` directly or
reading a diff of it.

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
- Whether files in `~/.config/labwc/` merge with the system copies or replace
  them depends on how labwc was started. Raspberry Pi OS passes
  `--merge-config`, so they merge and a user file should hold additions only.
  Without that flag the first file found wins and the user file needs the
  system contents seeded into it. Check which you have with
  `tr '\0' ' ' < /proc/$(pgrep -x labwc)/cmdline`.
- Apple TV's web player is built around Safari and is unreliable here.
