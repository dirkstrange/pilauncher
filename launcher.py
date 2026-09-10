#!/usr/bin/env python3
# Strange Media, a launcher for streaming services on a Raspberry Pi.
# Copyright (C) 2026 DJ Strange
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for
# more details.
#
# You should have received a copy of the GNU General Public License along
# with this program. If not, see <https://www.gnu.org/licenses/>.
#
# The Strange Media name and the marks in design/ are not covered by this
# license. See the README.
"""
pilauncher: a 10-foot launcher for streaming web apps on Raspberry Pi OS.

Serves the tile UI on 127.0.0.1 and spawns one Chromium kiosk window per
service, each with its own profile directory so sessions stay independent.

Environment overrides:
    PILAUNCHER_BROWSER    browser binary          (default: chromium)
    PILAUNCHER_PROFILES   profile root directory
    PILAUNCHER_PORT       listen port             (default: 8800)
    PILAUNCHER_SHELL_UNIT systemd unit for the launcher's own window
    PILAUNCHER_WLOPM      tool that powers the panel down  (default: wlopm)
    PILAUNCHER_WAYDROID   waydroid binary         (default: waydroid)
    PILAUNCHER_WAYDROID_STOP  root helper that stops an Android app
    PILAUNCHER_CDM_SEED   Widevine CDM copied into each new profile
    PILAUNCHER_ORDER      saved tile order (default: alongside the profiles)
    PILAUNCHER_WALLPAPERS screensaver image folder
    PILAUNCHER_NASA_KEY   api.nasa.gov key, to lift the shared DEMO_KEY limit
    PILAUNCHER_WALLPAPER_FEEDS  bing,apod,nasa-library,epic (default bing,apod)
    PILAUNCHER_NASA_QUERY search terms for the nasa-library feed
    PILAUNCHER_LOGOS      directory of service logo images
    PILAUNCHER_SERVICES   the live catalog (default is beside the profiles,
                          seeded from the copy in the checkout)
    PILAUNCHER_BIND       listen address (default 127.0.0.1; 0.0.0.0 opens
                          the settings page to the LAN, never TV control)
"""

from __future__ import annotations

import base64
import json
import os
import random
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
BROWSER = os.environ.get("PILAUNCHER_BROWSER", "chromium")
PROFILE_ROOT = Path(
    os.environ.get(
        "PILAUNCHER_PROFILES", Path.home() / ".local" / "share" / "pilauncher" / "profiles"
    )
)
HOST = "127.0.0.1"
PORT = int(os.environ.get("PILAUNCHER_PORT", "8800"))
# The catalog in the checkout is the one the project ships. The live one sits
# with the rest of this machine's state, because the settings page writes to it
# and a file that git tracks cannot be edited by the app without turning every
# tile someone adds into a dirty working tree and a failed pull. The shipped
# copy is seeded into place the first time the daemon runs.
DEFAULT_SERVICES = BASE / "services.json"
SERVICES_FILE = Path(
    os.environ.get("PILAUNCHER_SERVICES", PROFILE_ROOT.parent / "services.json")
)
# Editing from another machine is opt-in: set PILAUNCHER_BIND=0.0.0.0 to open
# the settings page to a browser on the LAN. The endpoints below stay refused
# to every address but this one regardless, so opening that door lets the
# network edit the catalog without handing it a remote control for the TV.
BIND = os.environ.get("PILAUNCHER_BIND", HOST)
LOCAL_ONLY = frozenset({"/launch", "/close", "/desktop", "/display"})
# Enough for a generous PNG, small enough that a mistyped upload cannot
# fill the card.
LOGO_MAX_BYTES = 2 * 1024 * 1024
SHELL_UNIT = os.environ.get("PILAUNCHER_SHELL_UNIT", "pilauncher-shell.service")
# install.sh already puts wlopm in the labwc autostart, where it runs once to
# turn the panel ON and stop the desktop blanking a film. This is the other
# direction, for the screensaver.
WLOPM = os.environ.get("PILAUNCHER_WLOPM", "wlopm")
# Android services are launched through waydroid. Launching works as the
# desktop user, but stopping an app needs "waydroid shell", which insists on
# root, so that one step goes through a root-owned helper permitted by a
# narrow sudoers rule. install.sh puts both in place.
WAYDROID = os.environ.get("PILAUNCHER_WAYDROID", "waydroid")
WAYDROID_STOP = os.environ.get(
    "PILAUNCHER_WAYDROID_STOP", "/usr/local/sbin/pilauncher-waydroid-stop"
)
WAYDROID_UNIT = os.environ.get("PILAUNCHER_WAYDROID_UNIT", "waydroid-session.service")
# What waydroid prints when a binder transaction fails in transport. It is not
# an error the CLI reports any other way: the exit status is 0 and the app
# simply never appears. See start_android().
WAYDROID_DEAD = "Sending reply failed"
# A known-good Widevine CDM copied into each new profile. See seed_widevine().
CDM_SEED = Path(os.environ.get("PILAUNCHER_CDM_SEED", PROFILE_ROOT.parent / "widevine"))
# Tile order lives outside the repo. services.json is the catalog and is
# tracked in git; rewriting it from the UI would conflict with every pull.
ORDER_FILE = Path(os.environ.get("PILAUNCHER_ORDER", PROFILE_ROOT.parent / "order.json"))
# Logos are fetched per machine rather than committed. They are trademarked
# brand assets, and a repo that ships them is redistributing them.
LOGO_DIR = Path(os.environ.get("PILAUNCHER_LOGOS", PROFILE_ROOT.parent / "logos"))
# Screensaver imagery. A local folder is the dependable source: it needs no
# network, no API key, and cannot be rate limited or discontinued. NASA's
# picture-of-the-day feed is the fallback so the feature works out of the box.
WALLPAPER_DIR = Path(os.environ.get("PILAUNCHER_WALLPAPERS", PROFILE_ROOT.parent / "screensaver"))
NASA_KEY = os.environ.get("PILAUNCHER_NASA_KEY", "DEMO_KEY")
CACHE_MAX_AGE = 22 * 60 * 60      # a day's worth, well inside DEMO_KEY limits
# Which feeds to draw from, in order. Everything here works without an API key
# of your own; NASA_KEY only raises the shared DEMO_KEY rate limit.
WALLPAPER_FEEDS = [
    f.strip() for f in
    os.environ.get("PILAUNCHER_WALLPAPER_FEEDS", "bing,apod").split(",")
    if f.strip()
]
# Search terms for the nasa-library feed, which takes a query rather than
# returning a fixed set.
NASA_QUERY = os.environ.get("PILAUNCHER_NASA_QUERY", "nebula galaxy")

LOGO_TYPES = {
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}

# Flags applied to every service window. Kiosk gives a bare fullscreen surface;
# the rest suppress the dialogs and bubbles that would otherwise appear on a TV
# with no keyboard attached.
COMMON_FLAGS = [
    "--kiosk",
    "--noerrdialogs",
    "--disable-infobars",
    "--disable-session-crashed-bubble",
    "--disable-features=TranslateUI,Translate",
    "--autoplay-policy=no-user-gesture-required",
    "--check-for-update-interval=31536000",
    "--password-store=basic",
    # Service pages draw their own scrollbars, which look wrong on a TV and
    # cannot be grabbed without a pointer. Scrolling still works; only the bar
    # is hidden.
    "--hide-scrollbars",
    # Streaming sites are built for a pointer, so arrow keys do nothing on most
    # of their menus. This moves focus between clickable elements instead,
    # which is as close to d-pad navigation as a plain web app gets. It applies
    # to service windows only; the launcher's own page handles arrows itself.
    "--enable-spatial-navigation",
]

_proc: subprocess.Popen | None = None
# Package name of the Android app on screen, or None. Android has no process
# of ours to hold on to: waydroid launches the app inside the container and
# returns, so the package name is the only handle we get.
_android: str | None = None
_lock = threading.Lock()


def seed_catalog() -> None:
    """Put the shipped catalog in place if this machine has none yet."""
    if SERVICES_FILE.exists():
        return
    SERVICES_FILE.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(DEFAULT_SERVICES, SERVICES_FILE)


def load_catalog() -> list[dict]:
    """The catalog as written in services.json, in file order."""
    seed_catalog()
    with open(SERVICES_FILE, encoding="utf-8") as fh:
        return json.load(fh)


def load_services() -> list[dict]:
    """The catalog in display order, honouring a saved arrangement."""
    services = load_catalog()
    try:
        ids = json.loads(ORDER_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return services
    if not isinstance(ids, list):
        return services
    rank = {sid: i for i, sid in enumerate(ids)}
    # sorted() is stable, so a service added to the catalog after the order was
    # saved keeps its relative position and lands at the end rather than
    # vanishing or jumping to the front.
    return sorted(services, key=lambda s: rank.get(s.get("id"), len(rank)))


def local_wallpapers() -> list[str]:
    if not WALLPAPER_DIR.is_dir():
        return []
    names = [
        p.name for p in sorted(WALLPAPER_DIR.iterdir())
        if p.is_file() and p.suffix.lower() in LOGO_TYPES and not p.name.startswith(".")
    ]
    return ["/wallpaper/" + n for n in names]


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "pilauncher"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def feed_apod() -> list[str]:
    """NASA picture of the day. Space, curated, one request per batch."""
    data = _get_json(
        "https://api.nasa.gov/planetary/apod"
        "?api_key=" + NASA_KEY + "&count=25&thumbs=true"
    )
    out = []
    for e in data if isinstance(data, list) else []:
        if e.get("media_type") == "image":
            link = e.get("hdurl") or e.get("url")
            if link and link.startswith("https://"):
                out.append(link)
    return out


def feed_bing() -> list[str]:
    """Bing's daily wallpaper. Landscape and nature photography, no key."""
    data = _get_json(
        "https://www.bing.com/HPImageArchive.aspx?format=js&idx=0&n=8&mkt=en-US"
    )
    out = []
    for img in data.get("images", []) if isinstance(data, dict) else []:
        path = img.get("url") or ""
        if not path:
            continue
        # The feed hands back a 1920x1080 crop; the UHD variant is the same
        # asset at full resolution and costs nothing extra to request.
        path = path.replace("_1920x1080.jpg", "_UHD.jpg")
        out.append("https://www.bing.com" + path)
    return out


def feed_nasa_library() -> list[str]:
    """NASA's image archive. No key at all, and searchable."""
    query = urllib.parse.quote(NASA_QUERY)
    data = _get_json(
        "https://images-api.nasa.gov/search?q=" + query +
        "&media_type=image&page_size=40"
    )
    out = []
    items = (data.get("collection", {}) or {}).get("items", []) if isinstance(data, dict) else []
    for item in items:
        for link in item.get("links", []) or []:
            href = link.get("href", "")
            # Listings return a thumbnail; the large rendition sits beside it
            # under the same asset name.
            if href.endswith("~thumb.jpg"):
                out.append(href.replace("~thumb.jpg", "~large.jpg"))
                break
    return out


def feed_epic() -> list[str]:
    """Live full-disc Earth from DSCOVR. Striking, but visually repetitive."""
    data = _get_json("https://api.nasa.gov/EPIC/api/natural?api_key=" + NASA_KEY)
    out = []
    for e in data if isinstance(data, list) else []:
        image, date = e.get("image"), e.get("date", "")[:10].replace("-", "/")
        if image and date:
            out.append(
                "https://api.nasa.gov/EPIC/archive/natural/" + date +
                "/png/" + image + ".png?api_key=" + NASA_KEY
            )
    return out


FEED_FUNCS = {
    "apod": feed_apod,
    "bing": feed_bing,
    "nasa-library": feed_nasa_library,
    "epic": feed_epic,
}


def wallpaper_cache(feeds: list[str]) -> Path:
    """Cache file for one set of feeds.

    Named after the set, because the backdrop asks for Bing alone while the
    screensaver takes whatever is configured, and one shared file would have
    each of them serving the other's answer.
    """
    return WALLPAPER_DIR / (".cache-" + "-".join(sorted(feeds)) + ".json")


def remote_wallpapers(feeds: list[str] | None = None) -> list[str]:
    """Image URLs from the given feeds, cached to disk for a day.

    Batched deliberately: a slideshow changing every half minute would exhaust
    a shared API key within the hour, while one request per feed per day does
    not come close. A stale cache is preferred over an empty screen when the
    network is down.
    """
    feeds = feeds or WALLPAPER_FEEDS
    cache_file = wallpaper_cache(feeds)
    try:
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        fresh = time.time() - cached.get("fetched", 0) < CACHE_MAX_AGE
        if fresh and cached.get("urls"):
            return cached["urls"]
    except (OSError, json.JSONDecodeError, TypeError):
        cached = {}

    urls: list[str] = []
    for name in feeds:
        func = FEED_FUNCS.get(name)
        if func is None:
            continue
        try:
            urls.extend(func())
        except Exception:
            # One dead feed should not take the others down with it.
            continue

    # APOD counts animations as images, and they arrive as GIFs: diagrams of
    # orbital mechanics, simulation loops, that sort of thing. They look wrong
    # between photographs and they loop distractingly under the drift.
    urls = [u for u in urls if ".gif" not in u.split("?")[0].lower()]

    if not urls:
        return cached.get("urls", []) if isinstance(cached, dict) else []

    try:
        WALLPAPER_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps({"fetched": time.time(), "urls": urls}), encoding="utf-8"
        )
    except OSError:
        pass
    return urls


def with_logos(services: list[dict]) -> list[dict]:
    """Attach a logo URL to any service that has an image on disk.

    Detected rather than declared, so dropping netflix.png into the logo
    directory is all it takes; services.json stays free of local file paths.
    """
    out = []
    for svc in services:
        svc = dict(svc)
        for ext in LOGO_TYPES:
            if (LOGO_DIR / (svc.get("id", "") + ext)).is_file():
                svc["logo"] = "/logos/" + svc["id"] + ext
                break
        out.append(svc)
    return out


def save_order(ids: list[str]) -> None:
    ORDER_FILE.parent.mkdir(parents=True, exist_ok=True)
    ORDER_FILE.write_text(json.dumps(ids, indent=2), encoding="utf-8")


def find_service(service_id: str) -> dict | None:
    for svc in load_catalog():
        if svc.get("id") == service_id:
            return svc
    return None


# A service id becomes a logo filename and a key in order.json, so it is held
# to something that needs no escaping in either place.
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# Package names as Android defines them: dot-separated segments each starting
# with a letter. A typo here fails at launch with nothing useful on screen,
# which is why the settings page offers a list instead of a text box.
PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*([.][A-Za-z][A-Za-z0-9_]*)+$")
CATALOG_FIELDS = frozenset({
    "id", "name", "url", "color", "ink", "tile_bg", "note",
    "kind", "package", "user_agent", "extra_flags", "hidden", "logo_shadow",
    "logo_scale",
})


def slugify(name: str) -> str:
    """Turn a display name into a candidate id."""
    flat = "".join(c if c.isalnum() else "-" for c in name.lower())
    return "-".join(part for part in flat.split("-") if part)[:32]


def validate_service(raw: dict) -> dict:
    """Check one catalog entry and return it cleaned.

    Raises ValueError carrying a message written for whoever is editing: it is
    shown in the settings page, not filed in a log.

    Unknown fields are rejected rather than dropped. Silently discarding a
    misspelled key would look like the setting had been saved.
    """
    if not isinstance(raw, dict):
        raise ValueError("service must be an object")
    unknown = sorted(set(raw) - CATALOG_FIELDS)
    if unknown:
        raise ValueError("unknown fields: " + ", ".join(unknown))

    svc: dict = {}
    sid = str(raw.get("id") or "").strip()
    if not ID_RE.match(sid):
        raise ValueError("id must be lowercase letters, digits and hyphens")
    svc["id"] = sid

    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    if len(name) > 40:
        raise ValueError("name is too long for a tile, 40 characters at most")
    svc["name"] = name

    kind = str(raw.get("kind") or "").strip()
    if kind and kind != "android":
        raise ValueError("kind must be android, or empty for a web page")

    if kind == "android":
        package = str(raw.get("package") or "").strip()
        if not PACKAGE_RE.match(package):
            raise ValueError("package must look like com.example.app")
        svc["kind"] = "android"
        svc["package"] = package
    else:
        url = str(raw.get("url") or "").strip()
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("url must start with http:// or https://")
        svc["url"] = url

    for field in ("color", "ink", "tile_bg"):
        value = str(raw.get(field) or "").strip()
        if not value:
            continue
        if not HEX_RE.match(value):
            raise ValueError(field + " must be a color like #1A2B3C")
        svc[field] = value.upper()

    for field in ("note", "user_agent"):
        value = str(raw.get(field) or "").strip()
        if value:
            svc[field] = value

    flags = raw.get("extra_flags")
    if flags:
        if not isinstance(flags, list) or not all(isinstance(f, str) for f in flags):
            raise ValueError("extra_flags must be a list of strings")
        svc["extra_flags"] = flags

    if raw.get("hidden"):
        svc["hidden"] = True
    # Recorded only when switched off. A shadow is the default, so writing an
    # explicit true into every entry would be noise in the file.
    if raw.get("logo_shadow") is False:
        svc["logo_shadow"] = False

    # How large the logo sits inside its tile, as a multiple of the size every
    # tile uses. Artwork arrives with wildly different amounts of padding
    # baked into its own bounding box, so one cap cannot suit all of it.
    scale = raw.get("logo_scale")
    if scale is not None:
        try:
            value = float(scale)
        except (TypeError, ValueError):
            raise ValueError("logo_scale must be a number")
        if not 0.4 <= value <= 1.4:
            raise ValueError("logo_scale must be between 0.4 and 1.4")
        # Left out when it is the default, so the file stays readable.
        if abs(value - 1.0) > 0.001:
            svc["logo_scale"] = round(value, 2)
    return svc


def save_catalog(services: list[dict]) -> None:
    """Replace services.json, keeping the previous version beside it.

    The catalog is the entire launcher, so a half-written file costs every
    tile at once. The replacement is built alongside the original and renamed
    over it, which is atomic within one filesystem, and the .bak left behind
    is what to reach for when the mistake was in the content rather than in
    the writing.
    """
    body = json.dumps(services, indent=2, ensure_ascii=False) + "\n"
    tmp = SERVICES_FILE.with_name(SERVICES_FILE.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)
        fh.flush()
        # This box loses power without warning. Without the sync a rename can
        # land before the bytes do, which is how a catalog comes back empty.
        os.fsync(fh.fileno())
    if SERVICES_FILE.exists():
        shutil.copy2(SERVICES_FILE, SERVICES_FILE.with_name(SERVICES_FILE.name + ".bak"))
    os.replace(tmp, SERVICES_FILE)


def logo_path(service_id: str) -> Path | None:
    for ext in LOGO_TYPES:
        candidate = LOGO_DIR / (service_id + ext)
        if candidate.is_file():
            return candidate
    return None


def clear_logo(service_id: str) -> None:
    """Remove every logo file for a service, whatever extension it used."""
    for ext in LOGO_TYPES:
        candidate = LOGO_DIR / (service_id + ext)
        if candidate.is_file():
            candidate.unlink()


def fetch_logo(svc: dict) -> None:
    """Pull a logo for one service, reusing the standalone fetcher.

    Loaded by path at call time because scripts/ is not a package, and because
    a fault in the fetcher should cost the settings page one feature rather
    than stop the daemon from starting.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "pilauncher_fetch_logos", BASE / "scripts" / "fetch_logos.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("scripts/fetch_logos.py is missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    LOGO_DIR.mkdir(parents=True, exist_ok=True)
    module.fetch_one(svc, LOGO_DIR, True)


def palette_from_logo(path: Path) -> dict:
    """Guess tile colors from a logo.

    A suggestion only: the settings page fills the fields in and lets them be
    changed afterwards. Pillow is optional, so a box without it gets no guess.
    """
    try:
        from PIL import Image
    except ImportError:
        return {}
    counts: dict[tuple[int, int, int], int] = {}
    colored: dict[tuple[int, int, int], int] = {}
    try:
        with Image.open(path) as img:
            img = img.convert("RGBA")
            img.thumbnail((64, 64))
            for red, green, blue, alpha in img.getdata():
                if alpha < 128:
                    continue
                bucket = (red // 32, green // 32, blue // 32)
                counts[bucket] = counts.get(bucket, 0) + 1
                # Greys are counted separately. Most logos are mostly white,
                # black or something in between, so counting those together
                # with the rest reliably returns the background rather than
                # the brand, which is what a first attempt at this did.
                if max(red, green, blue) - min(red, green, blue) >= 40:
                    colored[bucket] = colored.get(bucket, 0) + 1
    except (OSError, ValueError):
        return {}
    # Fall back to the greys only for a logo that genuinely has no color in
    # it, so a black-and-white mark still produces something usable.
    counts = colored or counts
    if not counts:
        return {}
    red, green, blue = (v * 32 + 16 for v in max(counts, key=lambda k: counts[k]))
    # sRGB luminance weights, deciding whether a label sitting on this color
    # should be black or white.
    luminance = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255
    return {
        "color": "#%02X%02X%02X" % (red, green, blue),
        "ink": "#000000" if luminance > 0.6 else "#FFFFFF",
        # Tile backgrounds in this catalog are near-black shades of the brand
        # color, which keeps a wall of them calm on a big screen.
        "tile_bg": "#%02X%02X%02X" % (red // 8, green // 8, blue // 8),
    }


def android_apps() -> list[dict]:
    """Launchable Android apps, as waydroid currently reports them.

    Filtered to the LAUNCHER category, which is what separates an app someone
    can open from the system plumbing that shares the same list. Nothing is
    cached: the point of asking is to see what was installed from the Play
    Store a minute ago.
    """
    if not shutil.which(WAYDROID):
        return []
    try:
        listing = subprocess.run(
            [WAYDROID, "app", "list"],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []

    apps: list[dict] = []
    current: dict = {}

    def keep(entry: dict) -> None:
        if entry.get("launchable") and entry.get("name") and entry.get("package"):
            apps.append({"name": entry["name"], "package": entry["package"]})

    for line in listing.splitlines():
        line = line.strip()
        if line.startswith("Name:"):
            keep(current)
            current = {"name": line[len("Name:"):].strip()}
        elif line.startswith("packageName:"):
            current["package"] = line[len("packageName:"):].strip()
        elif line == "android.intent.category.LAUNCHER":
            current["launchable"] = True
    keep(current)
    return sorted(apps, key=lambda a: a["name"].lower())


def stop_current() -> bool:
    """Stop the running service. Returns True if there was one.

    Deliberately says nothing about the launcher's own window. Callers decide
    whether the tiles should come back, because /close wants them and
    /desktop does not.
    """
    global _proc, _android
    with _lock:
        proc, _proc = _proc, None
        pkg, _android = _android, None

    stopped = False

    if pkg is not None:
        # An Android app has to be stopped explicitly. Its surface is mapped
        # for the whole waydroid session, so raising the tiles back over it
        # would leave the app running and still playing audio underneath.
        stop_android_app(pkg)
        stopped = True

    if proc is not None and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        except ProcessLookupError:
            pass
        stopped = True

    return stopped


def clear_active_app() -> None:
    """Tell waydroid that nothing is on screen any more.

    waydroid writes the package into waydroid.active_apps on every launch and
    never writes anything back when the app stops, so after a stop the property
    still names an app that is gone. An empty value is the state a freshly
    started session is in before anything has been launched, which is exactly
    the state we are trying to get back to.

    Best effort, and deliberately not checked. A stale value puts nothing on
    screen by itself, because the app it names is not running, and the next
    launch overwrites it regardless.
    """
    try:
        subprocess.run(
            [WAYDROID, "prop", "set", "waydroid.active_apps", ""],
            check=False,
            timeout=20,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def stop_android_app(pkg: str) -> bool:
    """Force-stop one Android app through the root helper."""
    try:
        subprocess.run(
            ["sudo", "-n", WAYDROID_STOP, pkg],
            check=True,
            timeout=30,
            capture_output=True,
        )
        stopped = True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        # Not fatal on its own. The caller still brings the tiles back, so the
        # worst case is an app left running behind them rather than a box with
        # no way out.
        stopped = False

    # Whether or not the force-stop landed, the launcher's position is that no
    # Android app should be on screen now, so say so.
    clear_active_app()
    return stopped


def start_service(svc: dict) -> int:
    global _proc
    # Opening something is the clearest possible sign that a person is here,
    # so it is also the safest place to undo a blanked panel. Covers the case
    # where the screensaver turned the screen off and the page then failed to
    # turn it back on for any reason.
    wake_display()
    stop_current()

    if svc.get("kind") == "android":
        return start_android(svc)

    profile = PROFILE_ROOT / svc["id"]
    profile.mkdir(parents=True, exist_ok=True)
    seed_widevine(profile)

    argv = [BROWSER, *COMMON_FLAGS, f"--user-data-dir={profile}"]
    if svc.get("user_agent"):
        argv.append(f"--user-agent={svc['user_agent']}")
    argv.extend(svc.get("extra_flags", []))
    argv.append(svc["url"])

    # start_new_session puts the browser in its own process group so the whole
    # tree can be signalled at once. Chromium forks a lot of helpers.
    proc = subprocess.Popen(argv, start_new_session=True)
    with _lock:
        _proc = proc
    return proc.pid


def android_ready(timeout: int = 120) -> bool:
    """Wait until Android can actually answer, rather than merely be running.

    "waydroid status" reports Session RUNNING within a second of the unit
    starting and roughly half a minute before Android has finished booting, so
    it is useless as a readiness test and actively misleading as a health test.
    sys.boot_completed is set by Android itself at the end of its own startup.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            out = subprocess.run(
                [WAYDROID, "prop", "get", "sys.boot_completed"],
                capture_output=True, text=True, timeout=20, check=False,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            out = ""
        if out == "1":
            return True
        time.sleep(2)
    return False


def restart_android_session() -> bool:
    """Restart the waydroid session unit and wait for Android to come back."""
    try:
        subprocess.run(
            ["systemctl", "--user", "restart", WAYDROID_UNIT],
            check=True, timeout=300, capture_output=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return android_ready()


def _ask_android_to_open(pkg: str) -> str:
    """Ask waydroid to open a package, returning everything it said."""
    proc = subprocess.run(
        [WAYDROID, "app", "launch", pkg],
        check=True, timeout=60, capture_output=True, text=True,
    )
    return (proc.stdout or "") + (proc.stderr or "")


def start_android(svc: dict) -> int:
    """Bring an Android app to the screen.

    Returns 0 rather than a pid. The app runs inside the waydroid container,
    so there is no process here to report; the package name in _android is the
    handle instead.

    Retries once through a session restart, because waydroid reaches a state
    where the session and the container both report RUNNING, listing apps and
    reading properties both work, and only the call that opens an app fails.
    It fails in the least helpful way available: one line on stderr, exit
    status 0, and no window. From the sofa that is indistinguishable from the
    OK button having stopped working, which is exactly how it was reported.

    Repairing it here rather than from a timer is deliberate. There is no way
    to test for the condition without launching something, and a health check
    that throws an app over whatever is playing every minute would be worse
    than the fault. The launch is the one moment the answer matters and the one
    moment a window is wanted anyway.
    """
    global _android
    pkg = svc.get("package")
    if not pkg:
        raise ValueError(f"service {svc.get('id')!r} is kind=android with no package")

    said = _ask_android_to_open(pkg)
    if WAYDROID_DEAD in said:
        print(f"android: {pkg} did not open ({WAYDROID_DEAD}). Restarting the session.")
        if not restart_android_session():
            raise RuntimeError(
                "the Android session would not come back. Check: "
                f"systemctl --user status {WAYDROID_UNIT}"
            )
        said = _ask_android_to_open(pkg)
        if WAYDROID_DEAD in said:
            raise RuntimeError(
                f"Android would not open {pkg} even after restarting the session"
            )
        print(f"android: session restarted, {pkg} opened on the retry")

    with _lock:
        _android = pkg

    # Nothing else to do. Waydroid maps a window only while an app is running,
    # not for the whole session, so the app's window is new and the compositor
    # raises it above the tiles the same way it does a Chromium service window.
    #
    # Worth stating because the obvious reading is wrong: the tiles do not have
    # to be hidden to reveal Android. An earlier version stopped the launcher's
    # own window here, which worked but cost a page reload on the way back and
    # lost the highlighted tile every time. Confirmed on the TV that the app
    # comes up in front with the tiles still running behind it.
    return 0


def seed_widevine(profile: Path) -> None:
    """Give a new profile a Widevine CDM so DRM works the first time it opens.

    Chromium registers a CDM only from the profile's own directory, located
    through a hint file holding an absolute path. It does not look at the
    packaged CDM in /opt/WidevineCdm at all. Left alone, a fresh profile fails
    DRM once, quietly downloads a CDM, and only plays on the next launch, which
    reads as a broken service rather than a cold profile.
    """
    dest = profile / "WidevineCdm"
    if dest.exists() or not CDM_SEED.is_dir():
        return
    versions = [p for p in CDM_SEED.iterdir() if p.is_dir()]
    if not versions:
        return
    try:
        shutil.copytree(CDM_SEED, dest)
        newest = max(versions, key=lambda p: p.name).name
        # The hint path is absolute, so it has to be rewritten per profile.
        (dest / "latest-component-updated-widevine-cdm").write_text(
            json.dumps({"Path": str(dest / newest)}), encoding="utf-8"
        )
    except OSError:
        # Not fatal. The component updater will fetch one on its own; the
        # service just will not play protected content until it reopens.
        pass


def _shell_unit(action: str) -> bool:
    """start or stop the launcher's own window, without touching services."""
    try:
        subprocess.run(
            ["systemctl", "--user", action, SHELL_UNIT],
            check=True,
            timeout=30,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False


def start_shell() -> bool:
    """Put the tiles back. A no-op if the window is already up."""
    return _shell_unit("start")


def stop_shell() -> bool:
    """Stop the launcher's own Chromium window, revealing the Pi desktop.

    The daemon keeps running. It holds no display state and costs nothing
    idle, and leaving it up means the desktop shortcut only has to start the
    shell again rather than the whole stack.
    """
    stop_current()
    return _shell_unit("stop")


def set_display(on: bool) -> bool:
    """Power the TV panel on or off through wlopm.

    Only the panel. The compositor keeps running and keeps delivering input,
    which is the whole reason this is safe: a dark screen is still a screen
    that reacts to the remote, and the first keypress turns it back on.
    """
    try:
        subprocess.run(
            [WLOPM, "--on" if on else "--off", "*"],
            check=True, timeout=10, capture_output=True,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def wake_display() -> None:
    """Force the panel on, unconditionally.

    Called from every route that means a person is doing something, so that no
    single failure can leave a black television with no way back. wlopm turning
    on an output that is already on is a no-op, so the cost of calling this
    more often than strictly necessary is one cheap subprocess on a path that
    already starts a browser.
    """
    set_display(True)


def service_running() -> bool:
    with _lock:
        # An Android app counts as running even though we hold no process for
        # it. The tile page polls this to decide whether to arm the
        # screensaver, and a show playing in the container is exactly the case
        # where elapsed time alone must not trip it.
        if _android is not None:
            return True
        return _proc is not None and _proc.poll() is None


class Handler(BaseHTTPRequestHandler):
    server_version = "pilauncher"

    def log_message(self, fmt, *args):  # quieter journal output
        pass

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _is_local(self) -> bool:
        """True when the request came from this machine.

        Read from the peer address rather than a header, because a header is
        whatever the client chooses to say.
        """
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            try:
                body = (BASE / "index.html").read_bytes()
            except OSError:
                self._json(500, {"error": "index.html missing"})
                return
            self._send(200, body, "text/html; charset=utf-8")
        elif path == "/services.json":
            # Hidden entries stay in the catalog but off the screen. They are
            # still launchable by id, so a service can be parked without
            # losing its colors and notes.
            visible = with_logos([s for s in load_services() if not s.get("hidden")])
            self._send(200, json.dumps(visible).encode(), "application/json")
        elif path == "/edit":
            # The settings page. A page of its own rather than a panel inside
            # the tiles, so that opening it from another machine gives an
            # editor and not a screenful of buttons that launch things here.
            try:
                body = (BASE / "edit.html").read_bytes()
            except OSError:
                self._json(500, {"error": "edit.html missing"})
                return
            self._send(200, body, "text/html; charset=utf-8")
        elif path == "/android/apps":
            # Feeds the picker in the settings page, so an app installed from
            # the Play Store becomes a tile without anyone typing a package
            # name, which is the one field that fails silently when wrong.
            self._send(200, json.dumps(android_apps()).encode(), "application/json")
        elif path == "/catalog":
            # Everything, hidden entries included, which is what the settings
            # page edits. /services.json stays the filtered view the tiles use.
            body = json.dumps(with_logos(load_catalog())).encode()
            self._send(200, body, "application/json")
        elif path.startswith("/logos/"):
            name = path[len("/logos/"):]
            # Resolve and confirm the result is still inside LOGO_DIR, so a
            # crafted path cannot read arbitrary files off the box.
            target = (LOGO_DIR / name).resolve()
            try:
                inside = target.is_relative_to(LOGO_DIR.resolve())
            except (OSError, ValueError):
                inside = False
            ctype = LOGO_TYPES.get(target.suffix.lower())
            if not inside or ctype is None or not target.is_file():
                self._json(404, {"error": "no such logo"})
                return
            try:
                self._send(200, target.read_bytes(), ctype)
            except OSError:
                self._json(500, {"error": "could not read logo"})
        elif path == "/wallpapers":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            asked = [
                f for f in (query.get("feeds", [""])[0] or "").split(",")
                if f in FEED_FUNCS
            ]
            if asked:
                # An explicit request is answered literally. Local images
                # override the default set below, but asking for Bing and
                # being handed the contents of a folder would be a puzzle.
                urls = remote_wallpapers(asked)
                source = ",".join(asked)
            else:
                local = local_wallpapers()
                # Local images win outright. Someone who put pictures in the
                # folder wants those, not whatever the feed is serving.
                urls = local if local else remote_wallpapers()
                source = "local" if local else ",".join(WALLPAPER_FEEDS)
            random.shuffle(urls)
            self._json(200, {"urls": urls, "source": source})
        elif path.startswith("/wallpaper/"):
            name = path[len("/wallpaper/"):]
            target = (WALLPAPER_DIR / name).resolve()
            try:
                inside = target.is_relative_to(WALLPAPER_DIR.resolve())
            except (OSError, ValueError):
                inside = False
            ctype = LOGO_TYPES.get(target.suffix.lower())
            if not inside or ctype is None or not target.is_file():
                self._json(404, {"error": "no such wallpaper"})
                return
            try:
                self._send(200, target.read_bytes(), ctype)
            except OSError:
                self._json(500, {"error": "could not read wallpaper"})
        elif path == "/status":
            self._json(200, {"running": service_running()})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "body must be JSON"})
            return

        if path in LOCAL_ONLY and not self._is_local():
            # The settings page may be open on a laptop. Driving the TV from
            # one is a different thing entirely and is not on offer.
            self._json(403, {"error": "only this machine can drive the TV"})
            return

        if path == "/launch":
            svc = find_service(str(payload.get("id", "")))
            if svc is None:
                self._json(404, {"error": "no such service"})
                return
            try:
                pid = start_service(svc)
            except FileNotFoundError as exc:
                missing = WAYDROID if svc.get("kind") == "android" else BROWSER
                self._json(500, {"error": f"{missing} not found on PATH ({exc})"})
                return
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                # The launcher's own window is still up in this case, since
                # start_android only drops it once the app has started, so the
                # tiles show the error rather than a bare desktop.
                self._json(500, {"error": f"could not launch {svc['id']}: {exc}"})
                return
            except RuntimeError as exc:
                # Android refused to open the app and would not be talked round
                # by a session restart either. Worth its own message: the tile
                # is fine, the catalog is fine, and the thing to look at is the
                # container.
                self._json(500, {"error": str(exc)})
                return
            self._json(200, {"launched": svc["id"], "pid": pid})
        elif path == "/display":
            # The tile page asks for this. It is the only thing that knows how
            # long the screensaver has been up, and it is also the thing that
            # sees the keypress ending it.
            on = bool(payload.get("on", True))
            if not set_display(on):
                self._json(500, {"error": f"{WLOPM} could not change the panel"})
                return
            self._json(200, {"display": "on" if on else "off"})
        elif path == "/close":
            wake_display()
            closed = stop_current()
            # Unconditional, not just for Android. Starting a unit that is
            # already running is a no-op, and the alternative is tracking
            # whether the window was dropped, which strands the user on the
            # bare desktop the first time that bookkeeping is wrong.
            start_shell()
            self._json(200, {"closed": closed})
        elif path == "/order":
            ids = payload.get("ids")
            if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
                self._json(400, {"error": "ids must be a list of strings"})
                return
            known = {s.get("id") for s in load_catalog()}
            unknown = [i for i in ids if i not in known]
            if unknown:
                self._json(400, {"error": f"unknown service ids: {unknown[:3]}"})
                return
            try:
                save_order(ids)
            except OSError as exc:
                self._json(500, {"error": f"could not save order: {exc}"})
                return
            self._json(200, {"saved": len(ids)})
        elif path == "/desktop":
            # Leaving for the desktop hands control to something that knows
            # nothing about the screensaver, so the panel has to be on before
            # the tiles stop being the thing on screen.
            wake_display()
            if stop_shell():
                self._json(200, {"desktop": True})
            else:
                self._json(500, {"error": f"could not stop {SHELL_UNIT}"})
        elif path == "/service":
            try:
                svc = validate_service(payload)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            catalog = load_catalog()
            at = next(
                (i for i, s in enumerate(catalog) if s.get("id") == svc["id"]), None
            )
            if at is None:
                catalog.append(svc)
            else:
                catalog[at] = svc
            try:
                save_catalog(catalog)
            except OSError as exc:
                self._json(500, {"error": f"could not save the catalog: {exc}"})
                return
            self._json(200, {"saved": svc["id"], "created": at is None})
        elif path == "/service/delete":
            sid = str(payload.get("id") or "")
            catalog = load_catalog()
            remaining = [s for s in catalog if s.get("id") != sid]
            if len(remaining) == len(catalog):
                self._json(404, {"error": "no such service"})
                return
            try:
                save_catalog(remaining)
            except OSError as exc:
                self._json(500, {"error": f"could not save the catalog: {exc}"})
                return
            # Drop the image too. Leaving it behind means a later service that
            # happens to reuse the id silently inherits the old picture.
            clear_logo(sid)
            self._json(200, {"deleted": sid})
        elif path == "/logo":
            sid = str(payload.get("id") or "").strip()
            if not ID_RE.match(sid):
                self._json(400, {"error": "id must be lowercase letters, digits and hyphens"})
                return
            if payload.get("clear"):
                clear_logo(sid)
                self._json(200, {"cleared": sid})
                return
            data = payload.get("data")
            if data:
                ext = str(payload.get("ext") or ".png").lower()
                if ext not in LOGO_TYPES:
                    self._json(400, {"error": "logo must be " + ", ".join(sorted(LOGO_TYPES))})
                    return
                try:
                    # Accepts a bare base64 body or a whole data: URL, since the
                    # settings page reads files with FileReader and gets the latter.
                    blob = base64.b64decode(str(data).split(",", 1)[-1], validate=True)
                except (ValueError, TypeError):
                    self._json(400, {"error": "data must be base64"})
                    return
                if len(blob) > LOGO_MAX_BYTES:
                    self._json(400, {"error": "that image is too large"})
                    return
                clear_logo(sid)
                LOGO_DIR.mkdir(parents=True, exist_ok=True)
                (LOGO_DIR / (sid + ext)).write_bytes(blob)
            else:
                url = str(payload.get("url") or "").strip()
                parsed = urllib.parse.urlparse(url)
                if parsed.scheme not in ("http", "https") or not parsed.netloc:
                    self._json(400, {"error": "url must start with http:// or https://"})
                    return
                previous = logo_path(sid)
                keep = (previous.name, previous.read_bytes()) if previous else None
                clear_logo(sid)
                try:
                    fetch_logo({"id": sid, "url": url})
                except Exception as exc:
                    # Deliberately broad: the fetcher reaches out over the
                    # network to sites that misbehave in inventive ways, and
                    # none of it is worth taking the daemon down for.
                    if keep is not None:
                        # A failed lookup should not cost the tile the image it
                        # already had.
                        (LOGO_DIR / keep[0]).write_bytes(keep[1])
                    self._json(502, {"error": f"could not fetch a logo: {exc}"})
                    return
            found = logo_path(sid)
            if found is None:
                self._json(502, {"error": "that site published no usable icon"})
                return
            self._json(
                200,
                {"logo": "/logos/" + found.name, "suggested": palette_from_logo(found)},
            )
        else:
            self._json(404, {"error": "not found"})


def main() -> None:
    PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
    # The daemon can be restarted while the screensaver has the panel off, and
    # the page that knew about it is gone by the time we are back. Start from a
    # lit screen rather than inherit a dark one nobody remembers turning off.
    wake_display()
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    server.daemon_threads = True
    print(f"pilauncher listening on http://{BIND}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_current()
        server.server_close()


if __name__ == "__main__":
    main()
