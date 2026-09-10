#!/usr/bin/env python3
"""
Add pilauncher's keybinds to a labwc rc.xml, replacing any previous block.

Called by install.sh. Kept as its own file rather than embedded in the shell
script because the block contains XML, braces and backslashes that are painful
to quote correctly through a heredoc.

    labwc_keybinds.py <rc.xml path> <pilauncher directory>
"""

from __future__ import annotations

import io
import re
import sys
import xml.etree.ElementTree as ET

BEGIN = "<!-- pilauncher:begin -->"
END = "<!-- pilauncher:end -->"

# labwc does not expand ~ in Execute commands, so paths are absolute and get
# filled in at install time.
TEMPLATE = """    {begin}
    <!-- Kiosk windows swallow keystrokes, so a way back has to be bound at the
         compositor rather than in the page. A-F4 is a backstop that does not
         depend on the launcher daemon being healthy. C-A-d toggles between the
         launcher and the Pi desktop.

         The remote control has no modifier keys, which puts every binding
         above out of reach from the couch. Its Home button already sends
         XF86HomePage; Menu is bound here as the remote's way to the desktop.
         Its back arrow is deliberately absent: that key reaches the page as
         BrowserBack, and index.html handles it so a service window can still
         navigate back a page instead of being closed outright. -->
    <keybind key="A-Escape">
      <action name="Execute" command="{here}/back.sh" />
    </keybind>
    <keybind key="XF86HomePage">
      <action name="Execute" command="{here}/back.sh" />
    </keybind>
    <keybind key="A-F4">
      <action name="Close" />
    </keybind>
    <keybind key="C-A-d">
      <action name="Execute" command="{here}/desktop.sh" />
    </keybind>
    <keybind key="Menu">
      <action name="Execute" command="{here}/desktop.sh" />
    </keybind>
    {end}
"""

# Window rules live outside <keyboard>, so they go in as their own block. The
# same markers are used and the removal below is a global substitution, so a
# re-run replaces both.
WINDOW_RULES = """  {begin}
  <!-- Waydroid sizes its window to whatever the taskbar leaves free, so an
       Android app opens short of the screen and the panel sits on top of it.
       Chromium asks for real fullscreen in kiosk mode, which is why the tiles
       cover the panel; nothing on the Android side ever makes that request, so
       it is made here instead. Matches every waydroid window, since some apps
       map more than one and the extra would otherwise show through. -->
  <windowRules>
    <windowRule identifier="waydroid.*" matchOnce="false">
      <action name="ToggleFullscreen" />
    </windowRule>
  </windowRules>
  {end}
"""


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    path, here = sys.argv[1], sys.argv[2]

    with io.open(path, encoding="utf-8") as fh:
        text = fh.read()

    # Drop any previous block so a re-run picks up keybinds added since. Without
    # this an existing install would keep whatever it got the first time.
    text = re.sub(
        r"[ \t]*" + re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n",
        "",
        text,
        flags=re.S,
    )

    marker = "<keyboard>\n"
    if marker not in text:
        print("no <keyboard> section in rc.xml; add the keybinds by hand", file=sys.stderr)
        return 1

    block = TEMPLATE.format(begin=BEGIN, end=END, here=here)
    idx = text.index(marker) + len(marker)
    updated = text[:idx] + block + text[idx:]

    # Window rules are a sibling of <keyboard>, not a child, so they cannot go
    # in the block above. Append them just inside the root element.
    #
    # The root is <openbox_config> on Raspberry Pi OS, which labwc accepts for
    # Openbox compatibility, but install.sh's own fallback writes
    # <labwc_config>. Both are valid, so accept either rather than assuming.
    closing = next(
        (tag for tag in ("</openbox_config>", "</labwc_config>") if tag in updated),
        None,
    )
    if closing is None:
        print(
            "no </openbox_config> or </labwc_config> in rc.xml; "
            "add the window rules by hand",
            file=sys.stderr,
        )
        return 1
    rules = WINDOW_RULES.format(begin=BEGIN, end=END)
    at = updated.rindex(closing)
    updated = updated[:at] + rules + updated[at:]

    # Parse before writing. A malformed rc.xml costs the whole session's
    # keybinds, and labwc reports it only to a log nobody is watching.
    try:
        ET.fromstring(updated)
    except ET.ParseError as exc:
        print(f"refusing to write malformed rc.xml: {exc}", file=sys.stderr)
        return 1

    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(updated)
    return 0


if __name__ == "__main__":
    sys.exit(main())
