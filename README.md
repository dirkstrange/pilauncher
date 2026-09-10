# Strange Media

A launcher that turns a Raspberry Pi into a TV streaming box.

![The launcher, a grid of service tiles on one screen](screenshots/Launcher.png)

## What it is

Every service you use on one screen, driven from a remote. Pick one and the service
opens full screen with no browser furniture around it, so it behaves like an
app rather than a web page you happen to be looking at on a television.

Most streaming services have no Raspberry Pi app. They do all have web
players, and a Pi 5 can run them. This wraps those players in something you
can operate from a sofa: one Chromium kiosk window per service, each with its
own profile and its own login, launched from a grid you can rearrange by
holding the OK button.

Android apps work as well, through Waydroid. Anything installed from the Play
Store appears in settings ready to become a tile, which is how YouTube and
Netflix run here as real Android TV apps instead of web players.

## Why it exists

The Pi is a capable little media machine with nothing on it that behaves like
a TV interface. Plug one into a television and you get a desktop, a mouse
pointer and a browser, which is a poor way to start watching something from
eight feet away.

A Roku or a Fire Stick solves it, and charges you for the fix in home screen
real estate. Rows you did not ask for, and adverts between you and the thing
you sat down to watch.

So this is the other trade. The catalog is a JSON file you own. Nothing
phones home, nothing recommends anything, and the only things on the screen
are the services you put there.

## Screenshots

Settings lists every tile, the Android apps that could become tiles, and the
way back to the Pi desktop.

![The settings page](screenshots/Settings.png)

Editing a tile shows it drawn exactly as the launcher draws it, both focused
and dimmed, because a color that works lit is not always the one that works
when it is not selected.

![Editing a tile](screenshots/Tile_Edit.png)

Install something from the Play Store and it turns up here with its package
name filled in, one press away from being a tile.

![Adding an Android app](screenshots/Add_Android_App.png)

## What it is built on

Python 3 from the standard library, and nothing else. `launcher.py` is a
single file running `http.server`, with no packages to install and no
virtualenv.

Chromium provides the players, one kiosk window per service, with Widevine
through the `libwidevinecdm0` package for the services that need DRM.

[labwc](https://labwc.github.io/) is the Wayland compositor Raspberry Pi OS
ships, and it handles the hotkeys a kiosk window would otherwise swallow.
[Waydroid](https://waydro.id/) runs the Android side. systemd user units
start the daemon and the display shell at login.

There is no framework and no build step. Editing `index.html` and reloading
the page is the entire development loop.

## Getting it running on a new Pi

You need a Raspberry Pi 5 running 64-bit Raspberry Pi OS with the desktop,
which is the release that uses labwc. A keyboard is needed for setup; after
that any remote sending arrow keys and Enter will do, including most HDMI-CEC
TV remotes and air mice. Bring your own accounts for the services, since this
launches their players and provides no content of its own.

```bash
git clone https://github.com/dirkstrange/pilauncher.git ~/pilauncher
cd ~/pilauncher
./install.sh
```

`install.sh` installs the packages, writes the systemd user units, adds the
compositor keybinds, installs the remote's key mapping and starts everything.
It is safe to re-run, which is also how you apply changes after a `git pull`:

```bash
cd ~/pilauncher && git pull --ff-only && ./install.sh
```

Reboot and the launcher comes up on its own. Before trusting the streaming
tiles, open <https://bitmovin.com/demos/drm> on the Pi and confirm the stream
plays, which is what tells you Widevine is working. Then press the gear in
the corner and start adding your own services.

## Things worth knowing before you build one

Netflix caps at about 720p here and Apple TV barely works. Both are decisions
those services make about Widevine on Linux, and neither is fixable from this
end. Protected content will not play at all under Waydroid, which has no
Widevine of its own. If any of that matters to you, read "Known ceilings" in
the technical notes before spending an evening on this.

## The details

[docs/TECHNICAL.md](docs/TECHNICAL.md) has everything this page leaves out.
How Widevine actually resolves, and why checking `/opt/WidevineCdm` tells you
nothing. Why the remote's OK button arrives dead and has to be remapped in
evdev. What the compositor does with a kiosk window's keystrokes. How the
catalog gets written without ever losing it, and where it lives. The
screensaver, the Android session, and the ceilings mentioned above.

`design/` holds the brand kit: the marks, the palette, and the sheet they are
documented on. Both pages inline those marks rather than fetching them, so
that folder is a source of truth rather than a runtime dependency.
