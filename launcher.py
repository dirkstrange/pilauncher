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
    PILAUNCHER_CDM_SEED   Widevine CDM copied into each new profile
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
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
# A known-good Widevine CDM copied into each new profile. See seed_widevine().
CDM_SEED = Path(os.environ.get("PILAUNCHER_CDM_SEED", PROFILE_ROOT.parent / "widevine"))

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
]

_proc: subprocess.Popen | None = None
_lock = threading.Lock()


def load_services() -> list[dict]:
    with open(BASE / "services.json", encoding="utf-8") as fh:
        return json.load(fh)


def find_service(service_id: str) -> dict | None:
    for svc in load_services():
        if svc.get("id") == service_id:
            return svc
    return None


def stop_current() -> bool:
    """Terminate the running service window. Returns True if one was killed."""
    global _proc
    with _lock:
        proc, _proc = _proc, None
    if proc is None or proc.poll() is not None:
        return False
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
    return True


def start_service(svc: dict) -> int:
    global _proc
    stop_current()

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


def stop_shell() -> bool:
    """Stop the launcher's own Chromium window, revealing the Pi desktop.

    The daemon keeps running. It holds no display state and costs nothing
    idle, and leaving it up means the desktop shortcut only has to start the
    shell again rather than the whole stack.
    """
    stop_current()
    try:
        subprocess.run(
            ["systemctl", "--user", "stop", SHELL_UNIT],
            check=True,
            timeout=15,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False


def service_running() -> bool:
    with _lock:
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
            visible = [s for s in load_services() if not s.get("hidden")]
            self._send(200, json.dumps(visible).encode(), "application/json")
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
            except FileNotFoundError:
                self._json(500, {"error": f"{BROWSER} not found on PATH"})
                return
            self._json(200, {"launched": svc["id"], "pid": pid})
        elif path == "/close":
            self._json(200, {"closed": stop_current()})
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
