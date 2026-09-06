#!/usr/bin/env python3
"""
Fetch a logo for each service in services.json.

Logos are trademarked brand assets, so they are not committed to this repo.
This pulls them onto the machine that runs the launcher instead, into
~/.local/share/pilauncher/logos by default.

It looks for the largest icon a site advertises: apple-touch-icon links first,
since those are square and typically 180px or better, then web app manifest
icons, then the plain favicon as a last resort. Sites vary wildly in what they
publish, so expect a few to come back small or ugly and to want replacing by
hand. Drop any image into the logo directory named <service id>.<ext> and it
will be used; nothing here has to succeed for the launcher to work.

    ./scripts/fetch_logos.py            fetch everything that is missing
    ./scripts/fetch_logos.py --force    refetch even if a file exists
    ./scripts/fetch_logos.py netflix    fetch just these services
"""

from __future__ import annotations

import argparse
import http.client
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DEFAULT_DIR = Path.home() / ".local" / "share" / "pilauncher" / "logos"

# Several of these refuse to serve anything to a client that looks automated.
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)
TIMEOUT = 15
# Streaming sites chunk aggressively and some close early; a partial read is a
# failed candidate, not a crash.
NET_ERRORS = (urllib.error.URLError, OSError, http.client.HTTPException, ValueError)
EXT_BY_TYPE = {
    "image/svg+xml": ".svg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/jpeg": ".jpg",
    "image/x-icon": ".png",
    "image/vnd.microsoft.icon": ".png",
}


def get(url: str) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read(), resp.headers.get("Content-Type", "").split(";")[0].strip()


def size_of(attrs: str) -> int:
    """Largest dimension named in a sizes="180x180" attribute, 0 if absent."""
    best = 0
    for w, h in re.findall(r'(\d+)x(\d+)', attrs):
        best = max(best, int(w), int(h))
    return best


def candidates(page: str, base_url: str) -> list[tuple[int, str]]:
    """Icon URLs found in the page, best first."""
    found: list[tuple[int, str]] = []
    for tag in re.findall(r'<link\s[^>]*>', page, re.I):
        rel = re.search(r'rel=["\']([^"\']+)["\']', tag, re.I)
        href = re.search(r'href=["\']([^"\']+)["\']', tag, re.I)
        if not rel or not href:
            continue
        rel_v = rel.group(1).lower()
        url = urllib.parse.urljoin(base_url, href.group(1))
        if "apple-touch-icon" in rel_v:
            # Ranked above manifest icons: square, meant to be seen large.
            found.append((1000 + size_of(tag), url))
        elif "icon" in rel_v:
            found.append((size_of(tag), url))
    found.sort(key=lambda p: -p[0])
    return found


def manifest_icons(page: str, base_url: str) -> list[tuple[int, str]]:
    m = re.search(r'<link\s[^>]*rel=["\']manifest["\'][^>]*>', page, re.I)
    if not m:
        return []
    href = re.search(r'href=["\']([^"\']+)["\']', m.group(0), re.I)
    if not href:
        return []
    try:
        raw, _ = get(urllib.parse.urljoin(base_url, href.group(1)))
        data = json.loads(raw)
    except NET_ERRORS:
        return []
    out = []
    for icon in data.get("icons", []) or []:
        src = icon.get("src")
        if not src:
            continue
        out.append((size_of(icon.get("sizes", "")), urllib.parse.urljoin(base_url, src)))
    out.sort(key=lambda p: -p[0])
    return out


def fetch_one(svc: dict, dest_dir: Path, force: bool) -> str:
    sid = svc["id"]
    existing = [p for p in dest_dir.glob(sid + ".*") if p.suffix.lower() in EXT_BY_TYPE.values()]
    if existing and not force:
        return f"kept {existing[0].name}"

    url = svc.get("url", "")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return "skipped, not a web service"
    origin = f"{parts.scheme}://{parts.netloc}"

    try:
        page_bytes, _ = get(url)
        page = page_bytes.decode("utf-8", "replace")
    except NET_ERRORS as exc:
        page = ""
        note = f"page unreadable ({exc.__class__.__name__})"
    else:
        note = ""

    tries = candidates(page, url) + manifest_icons(page, url)
    tries.append((0, origin + "/apple-touch-icon.png"))
    tries.append((0, origin + "/favicon.ico"))

    seen = set()
    for _, icon_url in tries:
        if icon_url in seen:
            continue
        seen.add(icon_url)
        try:
            data, ctype = get(icon_url)
        except NET_ERRORS:
            continue
        ext = EXT_BY_TYPE.get(ctype)
        if ext is None:
            ext = os.path.splitext(urllib.parse.urlsplit(icon_url).path)[1].lower()
            if ext not in EXT_BY_TYPE.values():
                continue
        if len(data) < 400:
            continue  # a placeholder or an error page, not a real icon
        for old in dest_dir.glob(sid + ".*"):
            old.unlink()
        out = dest_dir / (sid + ext)
        out.write_bytes(data)
        return f"got {out.name} ({len(data) // 1024}KB) from {urllib.parse.urlsplit(icon_url).netloc}"

    return f"no icon found{', ' + note if note else ''}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("ids", nargs="*", help="service ids; default is all of them")
    ap.add_argument("--force", action="store_true", help="refetch even if present")
    ap.add_argument("--dir", default=os.environ.get("PILAUNCHER_LOGOS", str(DEFAULT_DIR)))
    args = ap.parse_args()

    dest = Path(args.dir)
    dest.mkdir(parents=True, exist_ok=True)

    with io.open(BASE / "services.json", encoding="utf-8") as fh:
        services = json.load(fh)
    if args.ids:
        services = [s for s in services if s["id"] in args.ids]
        if not services:
            print("no matching services", file=sys.stderr)
            return 1

    print(f"logo directory: {dest}")
    for svc in services:
        print(f"  {svc['id']:<14} {fetch_one(svc, dest, args.force)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
