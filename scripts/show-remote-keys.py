#!/usr/bin/env python3
"""
Print what a remote control's buttons actually send.

Every readable node under /dev/input is watched at once, so there is no need to
work out which one a remote uses beforehand: press a button and the device it
came from is named in the output. Membership of the `input` group is enough to
read those nodes, which is why this does not want sudo.

    ./show-remote-keys.py [/dev/input/eventN ...]

Each press prints the evdev key name and, where the device reports one, the
hardware scancode. The scancode is the half that matters for a udev hwdb
remap, since hwdb matches on what the hardware sent rather than on the code the
kernel picked for it. See udev/70-pilauncher-remote.hwdb for a worked example.

Buttons that print nothing are worth noting too. Some are handled inside the
remote's own firmware and never reach the host at all, a mouse-mode toggle
being the usual case.
"""

from __future__ import annotations

import glob
import os
import re
import select
import struct
import sys
import time

EVENT_SIZE = struct.calcsize("llHHi")
EV_KEY = 0x01
EV_MSC = 0x04
MSC_SCAN = 0x04
KEY_NAMES = "/usr/include/linux/input-event-codes.h"


def load_key_names(path: str = KEY_NAMES) -> dict[int, str]:
    """Map evdev key codes to their KEY_/BTN_ names.

    Read from the kernel header rather than a table kept here, which would
    quietly drift from whatever kernel the Pi is actually running.
    """
    names: dict[int, str] = {}
    pattern = re.compile(r"^#define\s+((?:KEY|BTN)_\w+)\s+(0x[0-9a-fA-F]+|\d+)")
    try:
        with open(path) as fh:
            for line in fh:
                match = pattern.match(line)
                if match:
                    # First name wins, so the canonical KEY_* beats its aliases.
                    names.setdefault(int(match.group(2), 0), match.group(1))
    except OSError:
        print(
            f"note: {path} not readable, codes will be printed without names\n"
            "      install linux-libc-dev for the names",
            file=sys.stderr,
        )
    return names


def main(argv: list[str]) -> int:
    names = load_key_names()
    paths = argv or sorted(
        glob.glob("/dev/input/event*"),
        key=lambda p: int(p.rsplit("event", 1)[1]),
    )

    handles: dict[int, str] = {}
    for path in paths:
        try:
            handles[os.open(path, os.O_RDONLY)] = os.path.basename(path)
        except OSError as exc:
            # Nodes owned by another group are common and not worth stopping for.
            print(f"skip {path}: {exc}", file=sys.stderr)

    if not handles:
        print(
            "no readable input devices. Add yourself to the input group:\n"
            "  sudo usermod -aG input $USER    (log out and back in)",
            file=sys.stderr,
        )
        return 1

    print(
        f"watching {len(handles)} devices. "
        "Press a button, Ctrl-C to stop.\n",
        flush=True,
    )
    while True:
        ready, _, _ = select.select(list(handles), [], [], 1.0)
        for fd in ready:
            data = os.read(fd, EVENT_SIZE * 64)
            for at in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                _, _, etype, code, value = struct.unpack(
                    "llHHi", data[at:at + EVENT_SIZE]
                )
                stamp = time.strftime("%H:%M:%S")
                node = handles[fd]
                if etype == EV_MSC and code == MSC_SCAN:
                    print(f"{stamp} {node:<8} scancode 0x{value:x}", flush=True)
                elif etype == EV_KEY and value == 1:
                    # Releases and autorepeat only pad the output; the press is
                    # what identifies the button.
                    label = names.get(code, "unnamed")
                    print(f"{stamp} {node:<8} key {code:<5} {label}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print()
