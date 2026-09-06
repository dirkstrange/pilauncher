#!/usr/bin/env python3
"""
pilauncher: a 10-foot launcher for streaming web apps on Raspberry Pi OS.

Serves the tile UI on 127.0.0.1 and spawns one Chromium kiosk window per
service, each with its own profile directory so sessions stay independent.

Environment overrides:
    PILAUNCHER_BROWSER    browser binary          (default: chromium)
    PILAUNCHER_PROFILES   profile root directory
    PILAUNCHER_PORT       listen port             (default: 8800)
"""

from __future__ import annotations

import json
import os
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
    # Service pages render their own scrollbars, which look wrong on a TV and
    # cannot be reached without a pointer. Content still scrolls; only the bar
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
