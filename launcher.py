#!/usr/bin/env python3
"""
pilauncher: a 10-foot launcher for streaming web apps on Raspberry Pi OS.

Serves the tile UI on 127.0.0.1 and spawns one Chromium kiosk window per
service, each with its own profile directory so sessions stay independent.

Environment overrides:
    PILAUNCHER_BROWSER    browser binary          (default: chromium)
    PILAUNCHER_PROFILES   profile root directory
    PILAUNCHER_PORT       listen port             (default: 8800)
    PILAUNCHER_SHELL_UNIT systemd unit for the launcher's own window
    PILAUNCHER_WAYDROID   waydroid binary         (default: waydroid)
    PILAUNCHER_WAYDROID_STOP  root helper that stops an Android app
    PILAUNCHER_CDM_SEED   Widevine CDM copied into each new profile
    PILAUNCHER_ORDER      saved tile order (default: alongside the profiles)
    PILAUNCHER_WALLPAPERS screensaver image folder
    PILAUNCHER_NASA_KEY   api.nasa.gov key, to lift the shared DEMO_KEY limit
    PILAUNCHER_WALLPAPER_FEEDS  bing,apod,nasa-library,epic (default bing,apod)
    PILAUNCHER_NASA_QUERY search terms for the nasa-library feed
    PILAUNCHER_LOGOS      directory of service logo images
"""

from __future__ import annotations

import json
import os
import random
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
SHELL_UNIT = os.environ.get("PILAUNCHER_SHELL_UNIT", "pilauncher-shell.service")
# Android services are launched through waydroid. Launching works as the
# desktop user, but stopping an app needs "waydroid shell", which insists on
# root, so that one step goes through a root-owned helper permitted by a
# narrow sudoers rule. install.sh puts both in place.
WAYDROID = os.environ.get("PILAUNCHER_WAYDROID", "waydroid")
WAYDROID_STOP = os.environ.get(
    "PILAUNCHER_WAYDROID_STOP", "/usr/local/sbin/pilauncher-waydroid-stop"
)
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
WALLPAPER_CACHE = WALLPAPER_DIR / ".remote-cache.json"
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


def load_catalog() -> list[dict]:
    """The catalog as written in services.json, in file order."""
    with open(BASE / "services.json", encoding="utf-8") as fh:
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


def remote_wallpapers() -> list[str]:
    """Image URLs from the configured feeds, cached to disk for a day.

    Batched deliberately: a slideshow changing every half minute would exhaust
    a shared API key within the hour, while one request per feed per day does
    not come close. A stale cache is preferred over an empty screen when the
    network is down.
    """
    try:
        cached = json.loads(WALLPAPER_CACHE.read_text(encoding="utf-8"))
        fresh = time.time() - cached.get("fetched", 0) < CACHE_MAX_AGE
        if fresh and cached.get("urls"):
            return cached["urls"]
    except (OSError, json.JSONDecodeError, TypeError):
        cached = {}

    urls: list[str] = []
    for name in WALLPAPER_FEEDS:
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
        WALLPAPER_CACHE.write_text(
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


def stop_android_app(pkg: str) -> bool:
    """Force-stop one Android app through the root helper."""
    try:
        subprocess.run(
            ["sudo", "-n", WAYDROID_STOP, pkg],
            check=True,
            timeout=30,
            capture_output=True,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        # Not fatal on its own. The caller still brings the tiles back, so the
        # worst case is an app left running behind them rather than a box with
        # no way out.
        return False


def start_service(svc: dict) -> int:
    global _proc
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


def start_android(svc: dict) -> int:
    """Bring an Android app to the screen.

    Returns 0 rather than a pid. The app runs inside the waydroid container,
    so there is no process here to report; the package name in _android is the
    handle instead.
    """
    global _android
    pkg = svc.get("package")
    if not pkg:
        raise ValueError(f"service {svc.get('id')!r} is kind=android with no package")

    subprocess.run([WAYDROID, "app", "launch", pkg], check=True, timeout=60)

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
            # losing its colours and notes.
            visible = with_logos([s for s in load_services() if not s.get("hidden")])
            self._send(200, json.dumps(visible).encode(), "application/json")
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
            local = local_wallpapers()
            # Local images win outright. Someone who put pictures in the folder
            # wants those, not whatever the feed happens to be serving.
            urls = local if local else remote_wallpapers()
            random.shuffle(urls)
            self._json(200, {
                "urls": urls,
                "source": "local" if local else ",".join(WALLPAPER_FEEDS),
            })
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
            self._json(200, {"launched": svc["id"], "pid": pid})
        elif path == "/close":
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
            if stop_shell():
                self._json(200, {"desktop": True})
            else:
                self._json(500, {"error": f"could not stop {SHELL_UNIT}"})
        else:
            self._json(404, {"error": "not found"})


def main() -> None:
    PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print(f"pilauncher listening on http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_current()
        server.server_close()


if __name__ == "__main__":
    main()
