#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""serve-usb: serve rootfs/out/ (or any directory) read-only over HTTP on the USB link (10.42.0.1 only).

    tools/serve-usb.py [--port 8000] [--dir rootfs/out] [--bind 10.42.0.1]

The headset fetches with busybox wget, e.g.
    wget -O /tmp/stage2.tar.gz http://10.42.0.1:8000/stage2-latest.tar.gz
Binding fails (on purpose) if the host has no 10.42.0.1 address, i.e. the USB
link is down; it never falls back to 0.0.0.0. GET/HEAD only; no directory
listings; dotfiles and paths outside --dir are refused. Ctrl-C to stop.
"""
import argparse
import hashlib
import http.server
import ipaddress
import os
import sys
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK = ipaddress.ip_network("10.42.0.0/24")


class Handler(http.server.SimpleHTTPRequestHandler):
    def list_directory(self, path):
        self.send_error(403, "no listings")
        return None

    def send_head(self):
        parts = self.path.split("?", 1)[0].split("/")
        if any(p.startswith(".") for p in parts if p):
            self.send_error(403)
            return None
        # Symlinks are followed only if they resolve inside the served directory.
        root = os.path.realpath(self.directory)
        real = os.path.realpath(self.translate_path(self.path))
        if os.path.commonpath([root, real]) != root:
            self.send_error(403)
            return None
        return super().send_head()

    def do_GET(self):
        if ipaddress.ip_address(self.client_address[0]) not in LINK:
            self.send_error(403)
            return
        super().do_GET()

    def do_HEAD(self):
        if ipaddress.ip_address(self.client_address[0]) not in LINK:
            self.send_error(403)
            return
        super().do_HEAD()


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bind", default="10.42.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--dir", type=Path, default=ROOT / "rootfs" / "out")
    a = ap.parse_args()
    if ipaddress.ip_address(a.bind) not in LINK:
        sys.exit(f"serve-usb: refusing to bind {a.bind}: only the USB link {LINK} is allowed")
    d = a.dir.resolve()
    if not d.is_dir():
        sys.exit(f"serve-usb: {d} does not exist (run rootfs/build.sh)")
    try:
        srv = http.server.ThreadingHTTPServer((a.bind, a.port), partial(Handler, directory=str(d)))
    except OSError as e:
        sys.exit(f"serve-usb: cannot bind {a.bind}:{a.port} ({e}); is the USB link up "
                 f"(the host needs the address 10.42.0.1)?")
    print(f"serving {d} on http://{a.bind}:{a.port}/")
    for f in sorted(d.glob("*.tar.gz")):
        if not f.is_symlink():
            print(f"  {f.name}  {f.stat().st_size} bytes  sha256 {sha256(f)}")
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
