# Strange Media: the technical notes

Everything the [README](../README.md) deliberately leaves out. How each piece
works, why it is built the way it is, and the things that cost an evening to
work out the first time.

Read this when something breaks, when you are adapting the project to
different hardware, or when a decision here looks wrong and you want to know
whether it was considered.

## How it fits together

`launcher.py` is a single file with no dependencies outside the Python
standard library. It runs an HTTP server on port 8800, serves the two pages,
and starts a Chromium window when you pick something. Nothing else in the
codebase knows what Netflix is.

`index.html` is the tile page. Arrow keys move a cursor, Enter opens, holding
Enter picks a tile up so the arrows rearrange it instead.

`edit.html` is the settings page, reached from the gear. It reads and writes
the catalog through the API rather than touching files.

`services.json` is the catalog. There are two copies: the one in the checkout
is what a new install starts from, and the live one at
`~/.local/share/pilauncher/services.json` is what the settings page edits. See
"Where the catalog lives" below for why they are separate.

`back.sh` closes whatever service is open and `desktop.sh` switches between
the launcher and the Pi desktop. Both are bound to compositor hotkeys, because
a kiosk window swallows keystrokes before any page can see them.

`scripts/pilauncher-health.py` runs once a minute from
`pilauncher-health.timer` and repairs the audio when a boot with the television
switched off has left it going nowhere. It is the only part of this that runs
without anybody asking it to. See "What a boot with the TV off breaks".

`scripts/show-remote-keys.py` prints what a remote is actually sending, which
is where any argument about a dead button should start.

The server listens on `127.0.0.1` by default, so nothing outside the Pi can
reach it. `PILAUNCHER_BIND=0.0.0.0` opens the settings page to the house
network; the endpoints that drive the TV refuse any address but the Pi itself
whatever it is bound to.

Each service runs with its own `--user-data-dir`, so logins and cookies stay
separate and a player that crashes cannot take the others with it. Those
profiles live under `~/.local/share/pilauncher/profiles/`.

## Installing by hand

`install.sh` does all of this for you and is safe to re-run. These are the
steps it takes, for anyone who wants to know what landed on their machine.

```bash
sudo apt update
sudo apt install -y chromium libwidevinecdm0 curl wlopm
mkdir -p ~/pilauncher
# copy launcher.py, index.html, services.json, back.sh into ~/pilauncher
chmod +x ~/pilauncher/launcher.py ~/pilauncher/back.sh
```

The package is `chromium`, not `chromium-browser`. The older name still
resolves in apt on Raspberry Pi OS, and installing it puts a stale Debian 12
build on the machine alongside the current one. The launcher then starts
whichever of the two comes first on `PATH`, which is not a fight worth having.

If you edit this on Windows and deploy by `git pull`, note that Windows does
not record the executable bit. A script committed from there arrives without
it, and the Pi ends up with a local mode change that blocks the next pull.
`git update-index --chmod=+x <script>` sets the bit in the index so the clone
gets it right.

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

A web service needs a name and an address. "Fetch logo and colors" then reads
the site for the largest icon it advertises, using the lookup in
[scripts/fetch_logos.py](scripts/fetch_logos.py), and reads a brand color off
that image. A guess only fills a choice that is still empty, so a color
already picked survives one. Some sites publish nothing usable, Netflix among
them, and those want an uploaded image instead.

Color is a board of sixteen rather than a hex field, on the grounds that
nobody is typing `#E50914` with a d-pad. The swatch picked is the tile face
itself, and a dropdown decides which sixteen are on offer: Bright for a
service whose identity is one loud color, Dark and Light for the near-blacks
and near-whites most of the catalog uses. An earlier version had you pick a bright
brand color and then derived a near-black face from it, which meant the swatch
you clicked was never the color you got.

Three ways in when none of the sixteen is right: click the logo in the preview
to take the exact pixel under the pointer, choose From logo, which appears in
that dropdown once a logo has given up some colors and holds those rather than
mixing them into the other boards, or press "Pick from screen" where
Chromium offers that API, which not every browser does.

The preview beside the board is drawn the way the launcher draws a tile:
the same 16/9, the same corner radius, the same logo sizing and the same
shadow. It stretches to whatever height the color column comes to, so the
two finish level. Those rules are a hand-kept copy of the ones in
`index.html`, which is the price of previewing one page inside another and
has already caught this preview out twice.

The label color follows from how light the face is, and the glow the launcher
draws around a focused tile keeps whatever brand color the entry already had.
A near-black face has no hue to turn up, so deriving that one would trade
Netflix red for grey the first time a tile was saved.

Android tiles work differently, because there is no site to read a logo from
and a package name one character out fails at launch with nothing on screen to
explain why. The app is picked from a list of what Waydroid currently has
installed rather than typed. Anything installed from the Play Store appears in
that list, which is the intended route for adding one: install it in Android,
then add a tile for it here.

Deleting a tile takes its logo with it. Hiding one keeps every setting and only
takes it off the grid.

### A logo with its background baked in

Some sites publish a mark already sitting on a solid rectangle rather than on
transparency. It hides on a tile whose face happens to be the same color and
appears the moment anything makes the face non-uniform, which is how the
Apple TV logo turned into a visible black rectangle when the tiles gained
their gradient.

Uploading a transparent version is the honest fix. Where one is not to be
had, the background can be keyed out, since these are marks on flat color
rather than photographs:

```python
from PIL import Image
im = Image.open("appletv.png").convert("RGBA")
px, (w, h) = im.load(), im.size
CUT = 60.0                      # fully opaque at or above this luminance
for y in range(h):
    for x in range(w):
        r, g, b, _ = px[x, y]
        lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
        # A ramp rather than a hard cut, so antialiased edges survive.
        px[x, y] = (r, g, b, 255 if lum >= CUT else int(lum / CUT * 255))
im.save("appletv.png")
```

Check the brightness distribution first. That file was 88% below luminance 10
and the mark itself above 120, with under 1% in between, so a cut at 60 could
not eat any of the logo. A mark with genuinely dark parts needs a different
approach.

Logos live in `~/.local/share/pilauncher/logos` and are per-machine, so this
is a local repair rather than anything the repository carries.

### Editing from another machine

The daemon listens on `127.0.0.1` alone by default, so the settings page exists
only on the Pi. `PILAUNCHER_BIND=0.0.0.0` in the service unit opens it to the
house network at `http://<pi>:8800/edit`.

What that does not open is control of the TV. `/launch`, `/close` and
`/desktop` check the address a request came from and refuse anything that is
not the Pi itself, whatever the daemon is bound to. Someone on the network can
edit the catalog and cannot start playing something in the living room.

### Where the catalog lives, and how it is written

There are two copies of `services.json`. The one in the checkout is the catalog
the project ships. The live one is at
`~/.local/share/pilauncher/services.json`, seeded from the shipped copy the
first time the daemon runs, and that is the one the settings page edits.

They are separate for a plain reason: git tracks the shipped copy, so an app
that wrote to it would turn every tile you added into a dirty working tree and
a `git pull` that refuses to run. Editing the live copy leaves deployment
alone. `PILAUNCHER_SERVICES` overrides its location.

The shipped copy is still worth changing when a default belongs in the project
rather than on one machine, but it reaches a running Pi only on a fresh
install, since seeding does not overwrite a catalog that already exists.

`services.json` is the entire launcher, so a half-written file costs every tile
at once. Writes land in a temporary file, get an fsync, and are renamed over
the original, which is atomic within one filesystem. The previous version stays
as `services.json.bak`, which is what to restore from when the mistake was in
the content rather than in the writing.

The daemon rereads the catalog on every request, so a saved change shows up on
the next paint without restarting anything.

### Backing it up and restoring it

Settings has "Save a backup" and "Restore from a backup". The backup is one
JSON file holding the catalog, the tile order and every logo, the logos
base64'd inline. Around 900KB for twenty tiles.

One file rather than an archive because the whole project is already JSON and a
backup you can open and read is worth more than one that needs a tool to
inspect. Base64 costs a third in size and buys a single file that copies
anywhere.

```bash
curl -OJ http://pi-mediacenter.local:8800/export
```

`GET /export` sets a `Content-Disposition` filename, so fetching it directly
saves a named file. The settings page goes through a blob instead, only so the
kiosk is not navigated away from the page you are standing in front of.

`POST /import` replaces the catalog, the order and the logos. Neither endpoint
is in `LOCAL_ONLY`: this is catalog editing, which has always been allowed from
a laptop on the house network, and it drives nothing on the television.

Everything is validated before anything is written. Each entry goes through the
same `validate_service()` the settings page uses, ids must be unique, and every
logo is base64-decoded and size-checked up front. A backup with one bad entry
in the middle is refused whole rather than applied as far as the bad entry,
which would leave a catalog that is neither the old one nor the new one. A
refused restore has changed nothing, and an accepted one still leaves the
previous catalog as `services.json.bak`.

Two things are dropped quietly rather than treated as errors. Logos naming a
service the backup does not carry, and order entries naming a service that no
longer exists, which is what a tile deleted after the order was last saved
looks like. Only logos belonging to a service in the catalog are exported, so
the leftovers that accumulate in the logo directory stay out of the file.

`pilauncher_export` is a format version. An unknown one is refused outright,
because importing part of a document whose shape has changed is worse than
importing none of it.

Not included: browser profiles, and therefore not your logins. A restored box
has your tiles, colors and artwork, and asks you to sign in to each service
once.

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

### Turning the panel off on purpose

The setting above exists so nothing blanks the screen during a film. The
launcher then does its own blanking, on its own schedule, because it is the
only thing on the box that knows whether a film is playing.

The sequence on an idle evening is ten minutes to the screensaver, thirty
minutes of pictures, then the panel powers down. Any key brings it back. Both
numbers are `IDLE_MS` and `PANEL_OFF_MS` at the top of the screensaver block in
`index.html`.

Nothing here can fire while something is playing. `checkIdle()` asks the daemon
whether a service is open before it will start the screensaver at all, and the
blanking timer is only armed once the screensaver has actually started, so a
long film cannot reach it.

A web page cannot power down a display, so the page asks the daemon through
`POST /display` and the daemon runs `wlopm`. That route is in `LOCAL_ONLY`
alongside `/launch` and `/close`: blanking the television is TV control, not
catalog editing, and the settings page open on a laptop has no business doing
it.

The failure that matters is a dark screen with nothing left to light it, so
there are four ways back rather than one. Any keypress or pointer movement,
which is the normal one. Opening anything, since `start_service()` wakes the
panel before it does anything else. `/close` and `/desktop`, which cover the
remote's Home and Menu buttons. And loading the page, which covers a shell
restart while the screen was dark, plus daemon startup, which covers the daemon
being restarted while the page that knew about it is gone.

`wlopm` powers down the output only. The compositor keeps running and keeps
delivering input to the page underneath, which is what makes a dark screen
safe: it is still a screen that reacts to the remote.

One trap when testing this from a shell rather than from the sofa. Synthetic
pointer motion, `wlrctl pointer move`, does NOT wake a blanked panel and does
not reach the page's handlers once the output is down, although a real remote
does. So a dark screen that ignores `wlrctl` is not evidence of anything.
Worth knowing twice over, because `wlrctl pointer move` is also relative: a
small move from a pointer already parked in a corner produces no event at all
and looks exactly like a broken feature.

## What a boot with the TV off breaks

A television is not a monitor. It gets switched off, it gets switched to the
console input, and the Pi underneath it carries on regardless. Two things on
this box ask the display a question exactly once, early, and never ask again,
so booting while the TV is asleep leaves them holding a wrong answer for the
rest of the session. Both failures look like something else entirely.

### No sound at all

PipeWire probes each HDMI port as it starts. A TV that is off cannot answer, so
WirePlumber concludes there is no usable output, parks both HDMI cards at
profile `off`, and makes a Dummy Output the default sink. That sink accepts
audio and discards it, which is why everything keeps playing with the picture
intact and nothing comes out of the speakers. Turning the TV on afterwards
changes nothing, because the question is not asked twice.

`wpctl status` is the tell. A healthy box lists `Built-in Audio Digital Stereo
(HDMI)`; a broken one lists `Dummy Output` and nothing else.

`scripts/pilauncher-health.py` repairs it by restarting WirePlumber, and
`pilauncher-health.timer` runs it every minute so the sound comes back a minute
after the TV wakes up rather than at the next reboot. It checks that an HDMI
connector actually reports a display before doing anything, because silence is
the correct behaviour for a Pi with nothing plugged into it, and it holds a ten
minute cooldown so a repair that does not take is visible in the journal
instead of being hammered once a minute.

Three things bite when investigating this by hand:

`aplay -D hw:1,0` fails with "Sample format non available" on a perfectly
healthy card. vc4-hdmi only accepts `IEC958_SUBFRAME_LE` and ALSA converts
through the plug layer, so `plughw:1,0` is the device to test with. The raw
failure is not evidence of a passthrough mode or a fault.

The ELD of a DISCONNECTED HDMI port still reports the last monitor it saw, so
both ports can name the same television while only one is live.
`/sys/class/drm/card*-HDMI-A-*/status` is the honest answer.

A card parked at `off` does not merely have its stereo profile deselected: the
profile is not in the list at all, leaving only `off` and `pro-audio`. That
looks like a driver or hardware problem and is not one. Restarting WirePlumber
with the TV awake rebuilds the list.

### The wrong resolution, which then breaks other things

`~/.config/kanshi/config` forces 1920x1080@60, because the TV offers 4K only at
30Hz and nothing here renders above 1080p anyway. kanshi matches a profile by
output name and silently ignores one whose outputs are not all connected, so a
profile naming `HDMI-A-1` stops applying the moment the cable moves to the
other port. Nothing warns. The desktop simply comes up at the TV's preferred
mode.

That single change cascades. Half the refresh rate. Waydroid draws Android app
windows at 1080p in the middle of a 4K screen, since the Android session sizes
itself once at startup. And the mode change is enough to make the HDMI audio
probe above fail as well, so it presents as three unrelated faults at once.

The config therefore carries one profile per port, `tv` and `tv2`, both forcing
the same mode. An unmatched profile is inert rather than an error, so naming
both costs nothing and means the cable can move.

## When Android will not open anything

Waydroid reaches a state where `waydroid status` reports Session RUNNING and
Container RUNNING, `waydroid app list` returns the full list, `waydroid prop
get` and `set` both work, and every single `waydroid app launch` fails. It
fails in the least useful way available: one line of `Sending reply failed` on
stderr, exit status 0, and no window. Web tiles keep working, so from the sofa
it reads as the OK button having stopped working on some tiles but not others.

That message comes from `tools/interfaces/IPlatform.py` and means the gbinder
transaction failed in transport. It is not "no such app".

The repair is `systemctl --user restart waydroid-session.service`, then waiting
for Android to finish booting, which takes around 25 seconds. `waydroid status`
says RUNNING almost immediately and is useless as a readiness test; the
property `sys.boot_completed` reading `1` is the real signal.

`start_android()` in `launcher.py` does this automatically now. It reads what
waydroid printed, and on that specific string it restarts the session, waits
for `sys.boot_completed`, and tries the launch once more before giving up and
returning an error the tile page can show.

This is repaired at the launch rather than from the health timer on purpose.
There is no way to test for the condition without launching an app, and a check
that threw a window over whatever was playing every minute would be worse than
the fault it was looking for. The launch is the one moment the answer matters
and the one moment a window is wanted anyway.

Logcat shows nothing during any of this, so it is not worth reading.

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
  "note": "Catalog documentation. Not shown on screen.",
  "tile_bg": "#101014",
  "logo_shadow": false,
  "logo_scale": 1.2,
  "user_agent": "optional override",
  "extra_flags": ["--optional-chromium-flag"],
  "hidden": false
}
```

`tile_bg` is the face of the tile and `color` the brand color the launcher
glows around it when focused; `ink` is the label, used only where there is no
logo to cover it.

`logo_shadow` and `logo_scale` both exist because artwork does not arrive in a
consistent state. A drop shadow lifts a flat mark off a saturated face and
does the opposite to one that already carries its own edge, so it is per tile
and defaults to on. `logo_scale` multiplies the size every logo is drawn at,
which is worth having because each image bakes a different amount of padding
into its own bounding box: two logos capped identically can still look nothing
like the same size. It defaults to 1 and both are left out of the file when
they are at their default.

The strip below the grid shows the focused service's name, not its `note`.
Notes are documentation for whoever reads the catalog.

Set `hidden` to `true` to park an entry. It stays in the file with its colors
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
