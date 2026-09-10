#!/usr/bin/env python3
#
# pilauncher - a 10-foot launcher for streaming services on Raspberry Pi OS
# Copyright (C) 2026 DJ Strange
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version. See the LICENSE file for the full text.
#
"""Repair the session damage a boot with the TV off or on another input leaves.

The Pi asks each HDMI port what it can do exactly once, while PipeWire starts.
A TV that is off, or showing another input, cannot answer in time. PipeWire
concludes there is no usable output, parks both HDMI cards at profile "off",
and routes everything into a Dummy Output that accepts audio and discards it.
Turning the TV on afterwards does not make it ask again, so the box comes up
looking perfectly healthy and silent, and stays that way until something
restarts the session manager. That is what this script is for.

It runs from a timer rather than once at boot because the trigger is the TV
waking up, which can happen an hour later. Every check is read-only until one
of them fails, so the ordinary case costs a subprocess and nothing else.

Deliberately NOT handled here: the Android side. Waydroid can reach a state
where its session and container both report RUNNING while every app launch
fails in transport, and there is no way to test for that without actually
launching an app, which would throw a window over whatever is playing. That
repair belongs at the moment of the launch instead, and lives in launcher.py.

Exit status is 0 whenever the script itself ran, repair or no repair. A
non-zero exit would put the unit in failed state and mean a human has to clear
it before the timer is trusted again, which is the opposite of the point.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

STATE_FILE = Path(
    os.environ.get(
        "PILAUNCHER_HEALTH_STATE",
        Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        / "pilauncher"
        / "health.json",
    )
)

# A repair that did not take should not be attempted again on the next tick.
# Something structural is wrong at that point and hammering the session manager
# every minute makes it harder to see, not better. Long enough to be obviously
# deliberate in a log, short enough that turning the TV on and waiting through
# one advert break is the worst case.
COOLDOWN_SECONDS = 10 * 60

DRM_DIR = Path("/sys/class/drm")


def run(argv: list[str], timeout: int = 20) -> tuple[int, str]:
    """Run a command, returning (status, combined output). Never raises."""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"{exc}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        # Losing the state file costs us the cooldown, not the repair. Not
        # worth failing the run over.
        pass


def display_connected() -> bool:
    """True if any HDMI connector reports a display on the other end.

    Checked before touching audio because "no sound" is the correct and
    desirable state for a Pi with nothing plugged into it, and restarting the
    session manager on a loop would be the wrong answer to that.

    Read from DRM rather than from the sound card's ELD on purpose: the ELD of
    a DISCONNECTED port still reports the last monitor it saw, so both ports
    can name the same TV while only one is live.
    """
    try:
        for status in DRM_DIR.glob("card*-HDMI-A-*/status"):
            if status.read_text(encoding="utf-8").strip() == "connected":
                return True
    except OSError:
        # If DRM cannot be read, assume a display rather than suppress the
        # repair. A false positive here costs one wireplumber restart.
        return True
    return False


def audio_ok() -> tuple[bool, str]:
    """True if the default sink is backed by a real ALSA device.

    Tests for the presence of something real rather than for the name of the
    fallback. PipeWire's dummy sink has been called more than one thing across
    versions, but a working output is always an `alsa_output.*` node, so the
    positive test is the stable one.
    """
    status, out = run(["wpctl", "inspect", "@DEFAULT_AUDIO_SINK@"])
    if status != 0:
        return False, "no default sink at all"

    name = ""
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("* node.name") or line.startswith("node.name"):
            name = line.split("=", 1)[-1].strip().strip('"')
            break

    if name.startswith("alsa_output."):
        return True, name
    return False, name or "unnamed sink"


def repair_audio(verbose: bool) -> bool:
    """Restart the session manager so it re-asks the HDMI ports."""
    status, out = run(["systemctl", "--user", "restart", "wireplumber.service"], 60)
    if status != 0:
        print(f"health: could not restart wireplumber: {out.strip()}", file=sys.stderr)
        return False

    # WirePlumber needs a moment to enumerate the cards and pick a profile.
    # Poll rather than sleep a fixed amount so a fast box is not punished.
    for _ in range(20):
        time.sleep(1)
        ok, name = audio_ok()
        if ok:
            print(f"health: audio repaired, default sink is now {name}")
            return True

    print("health: restarted wireplumber but no ALSA sink appeared", file=sys.stderr)
    return False


def check_audio(state: dict, verbose: bool) -> None:
    ok, name = audio_ok()
    if ok:
        if verbose:
            print(f"health: audio ok ({name})")
        state.pop("audio_last_repair", None)
        return

    if not display_connected():
        if verbose:
            print("health: no sound, but no display connected either; leaving it")
        return

    last = state.get("audio_last_repair", 0)
    waited = time.time() - last
    if waited < COOLDOWN_SECONDS:
        if verbose:
            print(
                f"health: audio still broken, {int(COOLDOWN_SECONDS - waited)}s "
                "left on the cooldown"
            )
        return

    print(f"health: default sink is {name!r}, not a real output. Repairing.")
    state["audio_last_repair"] = time.time()
    save_state(state)
    repair_audio(verbose)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="say something even when everything is fine",
    )
    args = parser.parse_args()

    state = load_state()
    check_audio(state, args.verbose)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
