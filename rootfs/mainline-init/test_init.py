#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Offline test of the mainline init (init.sh). Never contacts the headset.

Runs the `init` and `bin/busybox` bytes of the built initramfs (aarch64 BusyBox via qemu-user binfmt)
as an ordinary process in test mode (LVR_TEST_ROOT): every path it touches is under a fake root in a
temporary directory, with a fake /proc/uptime (driven by a fast clock), /proc/cmdline, /dev/kmsg,
/sys/class/udc, /sys/class/watchdog and /dev/watchdog0 (a regular file). Stubs record ifconfig, udhcpd,
telnetd and any forbidden call (mount, reboot, poweroff, halt, watchdog, ...).

Checked: the UDC wait; that no-UDC, not-configured and dead-man resets open the qcom_wdt node and never
write it; heartbeat handling (counts from boot, then from the last accepted heartbeat; rejects future,
garbled, stale and oversized ones; accepts the exact `tools/dev heartbeat` command) and lvr_deadman=
parsing; kernel-log follower dedupe; refusal to run outside PID 1 without a test root; the applet links
(stripped ones absent); that nothing outside /tmp, configfs and the allowed LED attributes changes;
front-LED stages; the LED diagnostic code (the init's shell encoder against tools/led_code.py on fixture
and random trees, and recorded LED writes decoded from full-init runs, with reset timing bounds); the
return-to-Android request and warm_return (marker, sync, `printf r` into /proc/lvr_warm_reset, force_reset
after WARM_WAIT; no marker or sync if the trigger is not `armed`); the archive contents (static kexec,
insmod/rmmod linked, no modprobe, firmware limited to the known Adreno files); build reproducibility.

Most scenarios run time-scaled: they get a copy of the init whose loop sleeps are divided by SCALE
(TIME_PATCH: exact lines and counts, else FAIL) and a fake clock SCALE times faster. The return-android
and LED-code scenarios always run the archived bytes in real time. --full runs everything unscaled and serial.

Run: python3 -I rootfs/mainline-init/test_init.py [--initramfs PATH] [--scale S] [--jobs N]
     [--full] [--only NAME[,NAME]] [--keep]        (env LVR_TEST_TIMESCALE = default --scale)
Needs the initramfs from build.py and qemu-user binfmt for aarch64.
"""
import argparse
import gzip
import hashlib
import json
import importlib.machinery
import importlib.util
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import led_code  # noqa: E402  (tools/led_code.py: reference encoder/decoder)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]



DEFAULT_INITRAMFS = ROOT / "rootfs" / "out" / "mainline-init.cpio.gz"
WORK = None        # private per run (set in main): a fresh temporary init-test-* directory
SCALE = 1          # time scale of the scaled scenarios (main: --scale / --full)
DEFAULT_SCALE = 4
# loop sleeps of the init copy used by scaled scenarios: (line regex, seconds, count).
# The count is exact: if init.sh changes these loops, the harness fails instead of testing
# something else. klog's `sleep 1` (re-reads /dev/kmsg; scaling it only adds forks) and the
# telnetd restart loop stay; LED-code sleeps go through LVR_TEST_LEDCODE_DIV (div x SCALE).
TIME_PATCH = [
    (r"^(\t+)sleep 5$", 5.0, 1),      # dead-man pass
    (r"^(\t+)sleep 1$", 1.0, 5),      # warm-return wait, led_worker (2), UDC wait, USB-configured wait
    (r"^(\t+)sleep 0\.5$", 0.5, 1),   # ledcode_then_reset backstop pass
    (r'^(\t+)sleep "\$LEDCODE_STUCK_GAP"$', 20.0, 1),   # path-4 stuck gap (LEDCODE_STUCK_GAP=20)
]
# sync: the return path's `sync; printf r > /proc/lvr_warm_reset`. busybox: stubbed so any
# call (e.g. a `/bin/busybox reboot -f`) is recorded; none is expected.
STUBS = ["ifconfig", "udhcpd", "telnetd", "sync", "busybox"]
WARM_STUBS = ("sync", "busybox")
FORBIDDEN = ["mount", "umount", "reboot", "poweroff", "halt", "watchdog", "switch_root",
             "insmod", "rmmod", "modprobe", "kexec", "dd", "mkfs.ext2", "mke2fs", "mkfs.vfat"]
FAILS = []
LINKS = []          # applet links in the archive under test (set in main)
# Must stay unlinked (build.py STRIPPED); a subset is enough to catch a regression.
MUST_STRIP = ["reboot", "poweroff", "halt", "dd", "devmem", "watchdog", "mkfs.ext2", "mke2fs",
              "mkfs.vfat", "fdisk", "blkdiscard", "nandwrite", "hwclock", "rtcwake", "i2cset",
              "modprobe"]
# Linked for the fixed tools/dev mod-load / mod-unload commands; modprobe stays stripped.
MUST_LINK_MODULES = ["insmod", "rmmod"]
# Applets the real (PID 1) path needs that test mode never runs, plus the ones it does.
MUST_LINK = ["sh", "mount", "mkdir", "sleep", "cat", "awk", "cut", "readlink", "uname", "ln",
             "touch", "ifconfig", "udhcpd", "telnetd"]
LED_ATTRS = r"(brightness|trigger|delay_on|delay_off|multi_intensity)"
# Paths (relative to the test root) the harness itself changes during a run.
HARNESS_OWNED = re.compile(r"^(tmp|config|bin)(/|$)|^(console\.log|stubs\.log|busybox|init)$|"
                           r"^proc/(uptime|\.uptime\.tmp)$|^sys/class/udc(/|$)|"
                           r"^sys/devices/platform/soc@0/a600000\.usb/udc(/|$)")


def work_base():
    """Parent of the per-run work dir: $TMPDIR (default /tmp)."""
    return Path(tempfile.gettempdir())


def scaled_init(init, scale):
    """The archived init with TIME_PATCH's loop sleeps divided by scale (scale 1: unchanged)."""
    if scale == 1:
        return init
    text = init.decode()
    if not re.search(r"(?m)^LEDCODE_STUCK_GAP=20$", text):
        raise ValueError("TIME_PATCH: LEDCODE_STUCK_GAP is not 20; update test_init.py")
    for pat, secs, count in TIME_PATCH:
        text, n = re.subn(pat, lambda m: f"{m.group(1)}sleep {secs / scale:.4g}", text, flags=re.M)
        if n != count:
            raise ValueError(f"TIME_PATCH {pat!r}: {n} match(es), want {count}; update test_init.py")
    return text.encode()


def load_dev():
    """tools/dev as a module (for HEARTBEAT_CMD and parse_heartbeat); no device access."""
    loader = importlib.machinery.SourceFileLoader("dev", str(ROOT / "tools" / "dev"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("dev", loader))
    loader.exec_module(mod)
    return mod


def check(cond, what):
    print(("PASS  " if cond else "FAIL  ") + what)
    if not cond:
        FAILS.append(what)


def cpio_members(data):
    out, off = {}, 0
    while off < len(data):
        h = data[off:off + 110]
        assert h[:6] == b"070701", "not newc cpio"
        mode = int(h[14:22], 16)
        size, namesize = int(h[54:62], 16), int(h[94:102], 16)
        name = data[off + 110:off + 110 + namesize - 1].decode()
        off = (off + 110 + namesize + 3) & ~3
        if name == "TRAILER!!!":
            break
        out[name] = (mode, data[off:off + size])
        off = (off + size + 3) & ~3
    return out


class Clock(threading.Thread):
    """Writes R/proc/uptime from a piecewise speed profile [(until_fake_s, speed), ...]
    and applies scheduled events {fake_s: callable}."""

    def __init__(self, root, profile, events=None, start=5.0, scale=1):
        """the clock stands still until the init has logged `reached` (its INIT_UP is
        read just before), then runs at the profile's speeds x scale. The boot prologue (no loop
        sleeps, qemu-user forks, slower when scenarios run in parallel) then cannot move INIT_UP
        (the code's up= field) off the start value; before it ran at the profile's speed,
        which needed the prologue to take < 1 fake s)."""
        super().__init__(daemon=True)
        self.root, self.profile, self.events = root, profile, dict(events or {})
        self.fake, self.stop = start, False
        self.scale, self.boost = scale, 0
        self.write()

    def reached(self):
        try:
            with open(self.root / "console.log", "rb") as f:
                return b"] reached " in f.read(8192)
        except OSError:
            return False

    def write(self):
        tmp = self.root / "proc" / ".uptime.tmp"
        tmp.write_text(f"{self.fake:.2f} 0.00\n")
        os.replace(tmp, self.root / "proc" / "uptime")

    def run(self):
        last = time.monotonic()
        while not self.stop:
            time.sleep(0.02)
            now = time.monotonic()
            if self.boost != self.scale and self.reached():
                self.boost = self.scale
            speed = next(s for until, s in self.profile if self.fake < until) * self.boost
            self.fake += (now - last) * speed
            last = now
            for t in sorted(k for k in self.events if k <= self.fake):
                self.events.pop(t)()
            self.write()


def put(path, text):
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


class Case:
    def __init__(self, name, busybox, init, cmdline="rdinit=/init panic=0", wdt=True, scale=None):
        """scale: None = the run's SCALE; 1 = real-time case (archived init bytes, clock as given)."""
        self.name = name
        self.scale = SCALE if scale is None else scale
        self.r = WORK / name
        shutil.rmtree(self.r, ignore_errors=True)
        r = self.r
        r.mkdir(parents=True)
        (r / "LVR_TEST_ROOT").write_text("test root marker\n")
        for d in ["bin", "sbin", "proc", "sys/class/udc", "sys/class/watchdog",
                  "sys/devices/platform/soc@0/17c10000.watchdog",
                  "sys/devices/platform/soc@0/a600000.usb/power", "dev", "config", "tmp"]:
            (r / d).mkdir(parents=True, exist_ok=True)
        bb = r / "busybox"
        bb.write_bytes(busybox)
        bb.chmod(0o755)
        (r / "init").write_bytes(scaled_init(init, self.scale))
        for a in LINKS:                 # exactly the archive's applet links
            (r / "bin" / a).symlink_to(bb)
        for s in STUBS + FORBIDDEN:
            p = r / "bin" / s
            if p.exists() or p.is_symlink():
                p.unlink()
            tail = 'exec sleep 100000\n' if s == "telnetd" else ""
            p.write_text(f'#!/bin/sh\necho "{s} $*" >> "$LVR_TEST_ROOT/stubs.log"\n{tail}')
            p.chmod(0o755)
        put(r / "proc" / "cmdline", cmdline + "\n")
        # Seeded kernel log: 5 records + a dictionary continuation line.
        recs = [f"6,{i},{1000 * (i + 1)},-;seed record {i}" for i in range(5)]
        recs.insert(2, " SUBSYSTEM=platform")
        (r / "dev" / "kmsg").write_text("\n".join(recs) + "\n")
        if wdt:
            w = r / "sys/class/watchdog/watchdog0"
            w.mkdir()
            (w / "device").symlink_to("../../../devices/platform/soc@0/17c10000.watchdog")
            (r / "dev" / "watchdog0").write_bytes(b"")
        self.proc = None
        self.clock = None
        self.snap = None

    def add_led(self, name, max_brightness=511, trigger="[none] timer heartbeat",
                brightness="0", multi_index=None, flash=False):
        d = self.r / "sys/class/leds" / name
        d.mkdir(parents=True)
        (d / "brightness").write_text(brightness + "\n")
        (d / "max_brightness").write_text(f"{max_brightness}\n")
        (d / "trigger").write_text(trigger + "\n")
        if multi_index:
            (d / "multi_index").write_text(multi_index + "\n")
            (d / "multi_intensity").write_text(" ".join("0" for _ in multi_index.split()) + "\n")
        if flash:
            (d / "flash_brightness").write_text("0\n")
        return d

    def led(self, name):
        """{attribute: value} of a fake LED (missing attributes are absent)."""
        d = self.r / "sys/class/leds" / name
        return {f.name: f.read_text().strip() for f in d.iterdir() if f.is_file()}

    def wait_led(self, name, want, timeout):
        """Wait until every attribute in `want` has the given value; return the LED state."""
        end = time.monotonic() + timeout
        while True:
            st = self.led(name)
            if all(st.get(k) == v for k, v in want.items()) or time.monotonic() > end:
                return st
            time.sleep(0.05)

    def snapshot(self):
        """Every file, symlink and directory under the root the harness doesn't own."""
        out = {}
        for dirpath, dirs, files in os.walk(self.r):
            for n in dirs + files:
                p = Path(dirpath) / n
                rel = str(p.relative_to(self.r))
                if HARNESS_OWNED.search(rel):
                    continue
                if p.is_symlink():
                    out[rel] = ("l", os.readlink(p))
                elif stat.S_ISFIFO(p.lstat().st_mode):
                    out[rel] = ("p",)       # never open a FIFO (it would block)
                elif p.is_dir():
                    out[rel] = ("d",)
                else:
                    try:
                        out[rel] = ("f", p.read_bytes())
                    except OSError:
                        out[rel] = ("f?",)
        return out

    def add_udc(self, state):
        u = self.r / "sys/devices/platform/soc@0/a600000.usb/udc/a600000.usb"
        u.mkdir(parents=True, exist_ok=True)
        put(u / "state", state + "\n")
        link = self.r / "sys/class/udc/a600000.usb"
        if not link.is_symlink():
            link.symlink_to("../../devices/platform/soc@0/a600000.usb/udc/a600000.usb")
        if not (u / "device").is_symlink():
            (u / "device").symlink_to("../..")

    def set_udc_state(self, state):
        put(self.r / "sys/devices/platform/soc@0/a600000.usb/udc/a600000.usb/state",
            state + "\n")

    def heartbeat(self, text=None):
        """Write the heartbeat file like a host would (default: the current fake uptime)."""
        put(self.r / "tmp" / "lvr-heartbeat", (text if text is not None else f"{self.clock.fake:.2f}") + "\n")

    def read(self, rel):
        p = self.r / rel
        try:
            return p.read_text()
        except OSError:
            return ""

    def wait_file(self, rel, pattern, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            m = re.search(pattern, self.read(rel))
            if m:
                return m
            time.sleep(0.1)
        return None

    def start(self, profile, events=None, div=20, clock_start=5.0):
        """div: LVR_TEST_LEDCODE_DIV (LED code runs div times faster than on the device;
        match it to the clock speed so fake-time bounds hold). a scaled case runs the
        clock and the divisor self.scale times faster (its init's loop sleeps are shorter)."""
        div = min(1000, div * self.scale)
        self.snap = self.snapshot()
        self.clock = Clock(self.r, profile, events, start=clock_start, scale=self.scale)
        self.clock.start()
        env = {"LVR_TEST_ROOT": str(self.r), "PATH": f"{self.r}/bin",
               "LVR_TEST_LEDCODE_DIV": str(div)}
        self.proc = subprocess.Popen([str(self.r / "busybox"), "sh", str(self.r / "init")],
                                     env=env, start_new_session=True,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def console(self):
        p = self.r / "console.log"
        return p.read_text(errors="replace") if p.exists() else ""

    def wait_for(self, pattern, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            m = re.search(pattern, self.console())
            if m:
                return m
            time.sleep(0.1)
        return None

    def holders(self):
        """PIDs in the init's session holding R/dev/watchdog0 open."""
        target = os.path.realpath(self.r / "dev" / "watchdog0")
        sid = self.proc.pid
        found = []
        for p in os.listdir("/proc"):
            if not p.isdigit():
                continue
            try:
                if os.getsid(int(p)) != sid:
                    continue
                for fd in os.listdir(f"/proc/{p}/fd"):
                    if os.readlink(f"/proc/{p}/fd/{fd}") == target:
                        found.append((int(p), int(fd)))
            except OSError:
                continue
        return found

    def stubs(self):
        p = self.r / "stubs.log"
        return p.read_text() if p.exists() else ""

    def stop(self):
        if self.clock:
            self.clock.stop = True
        if self.proc:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.proc.wait()

    def common_checks(self, allow=None, writes=None, warm=False):
        """allow: regex for expected shell error lines (e.g. the failed watchdog open).
        writes: regex for further paths the init may change (besides LED attributes).
        warm: the return path's warm-reset trigger ran: exactly one sync, no busybox."""
        noise = [l for l in self.console().splitlines()
                 if not l.startswith("lvr-init: ") and not (allow and re.search(allow, l))]
        check(not noise, f"{self.name}: no shell error lines on the console ({noise[:1]})")
        st = self.stubs()
        check(not any(line.split()[0] in FORBIDDEN for line in st.splitlines()),
              f"{self.name}: no forbidden command run (mount/reboot/watchdog/...)")
        wl = [l for l in st.splitlines() if l.split()[0] in WARM_STUBS]
        want = ["sync "] if warm else []
        check(wl == want, f"{self.name}: warm-return calls (sync, busybox) are {want} ({wl})")
        check(not (self.r / "proc" / "qkx_bootdone").exists(),
              f"{self.name}: /proc/qkx_bootdone never created")
        w = self.r / "dev" / "watchdog0"
        if w.exists():
            # init's first act is the magic close (one 'V', then close).
            # A reset path's later open truncates the fake node and never writes it.
            data = w.read_bytes()
            check(data in (b"", b"V"), f"{self.name}: watchdog node holds at most the "
                                       f"takeover 'V' ({data[:8]!r})")
            if (self.r / "tmp" / "lvr-reset" / "device").exists():
                check(data == b"", f"{self.name}: reset path opened the watchdog and never wrote it")
            if os.access(w, os.W_OK):
                first = [l for l in self.console().splitlines() if l.startswith("lvr-init: ")][:1]
                check(bool(first) and re.fullmatch(r"lvr-init: \[\d+s\] watchdog \S+/dev/watchdog0 "
                                                   r"taken over and stopped \(magic close\)", first[0])
                      is not None,
                      f"{self.name}: init's first act is the watchdog takeover (magic close) ({first})")
        if self.snap is not None:
            time.sleep(1.2)             # let the LED worker finish its current pass
            after = self.snapshot()
            # dev/watchdog0: the takeover 'V' (content checked above).
            ok = re.compile(rf"^sys/class/leds/[^/]+/{LED_ATTRS}$|^dev/watchdog0$" + (f"|{writes}" if writes else ""))
            changed = sorted(k for k in set(self.snap) | set(after)
                             if self.snap.get(k) != after.get(k) and not ok.search(k))
            check(not changed, f"{self.name}: nothing written outside /tmp, configfs and LED "
                               f"attributes ({changed[:3]})")


def up_of(m):
    return int(m.group(1))


def scenario_no_udc(bb, init):
    c = Case("no-udc", bb, init, cmdline="rdinit=/init lvr_deadman=120")
    try:
        c.start([(1e9, 20)])
        w = c.wait_for(r"\[(\d+)s\] waiting up to 60s for a UDC", 10)
        m = c.wait_for(r"\[(\d+)s\] RESET: no UDC after 60s", 15)
        check(bool(w and m), "no-udc: init forces a reset when no UDC appears")
        if w and m:
            check(up_of(m) - up_of(w) >= 60, f"no-udc: waited >= 60 s ({up_of(w)}s -> {up_of(m)}s)")
        check(c.wait_for(r"held open and never written", 5) is not None,
              "no-udc: watchdog opened")
        time.sleep(0.5)
        h = c.holders()
        check([p for p, fd in h] == [c.proc.pid] and h[0][1] == 3,
              f"no-udc: fd 3 on watchdog0 held by PID-1 stand-in only ({h})")
        check("bound" not in c.console(), "no-udc: no bind, no network services")
        check("telnetd" not in c.stubs(), "no-udc: telnetd not started")
        d = c.wait_for(r"reset requested \(dead-man: no heartbeat for 120s \(last none, lvr_deadman=120\)\) but already armed", 20)
        check(d is not None, "no-udc: later dead-man sees the lock and only holds")
        check(not (c.r / "proc" / "sysrq-trigger").exists(), "no-udc: no sysrq fallback")
        con = c.console()
        check("lvr-init: [" in con and re.search(r"\] leds: none \(red= green= multicolor=\)", con)
              and re.search(r"\] led: stage red: no status LED", con)
              and re.search(r"\] led: stage reset: no status LED", con),
              "no-udc: no LEDs at all: logged 'leds: none', stages logged, nothing else")
        check(re.search(r"\] udc: none\n", con) and re.search(r"waiting for UDC \(udc: none\)", con)
              and re.search(r"\] no UDC after 60s \(udc: none\)", con),
              "no-udc: UDC list logged at the start, while waiting and at the timeout")
        check(re.search(r"\] deferred: \S*/sys/kernel/debug/devices_deferred not readable", con),
              "no-udc: missing devices_deferred logged, reset unaffected")
        check(re.search(r"\] ledcode v2 path=1 flags=RRRRRRRRRRRR ndef=99 d=00\.0\.00,00\.0\.00,00\.0\.00 "
                        r"up=5 usb=999 sum=\d\n", con) is not None
              and re.search(r"\] ledcode: (reset already armed|no red/green/blue LED); not played", con),
              "no-udc: code line logged; not played (dead-man deadline <= 60 s away, no LEDs)")
        check(re.search(r"\] ledcode: dead-man deadline 120s is <= 60 s away; resetting now", con)
              is not None, "no-udc: lvr_deadman=120 leaves no time for the code: reset right away")
        time.sleep(1.5)
        check(len(c.holders()) == 1, "no-udc: still exactly one holder after dead-man")
        c.common_checks()
    finally:
        c.stop()


def scenario_success_heartbeat(bb, init):
    """Default 600 s. No heartbeat until after the boot-based warning; heartbeats at ~580
    and ~850 keep it alive past uptime 600 and 1180; reset 600 s after the last one."""
    c = Case("success-heartbeat", bb, init)
    hb = {}

    def beat(k):
        hb[k] = int(c.clock.fake)
        c.heartbeat(f"{hb[k]}.37")
    try:
        c.start([(150, 10), (540, 50), (620, 5), (1380, 50), (1e9, 5)],
                events={20: lambda: c.add_udc("not attached"),
                        25: lambda: c.set_udc_state("configured"),
                        580: lambda: beat(1), 850: lambda: beat(2)})
        b = c.wait_for(r"\[(\d+)s\] bound a600000\.usb", 15)
        check(b is not None, "success: UDC bound after it appears late")
        udc = c.r / "config/usb_gadget/qkx/UDC"
        check(udc.exists() and udc.read_text().strip() == "a600000.usb", "success: UDC written")
        g = c.r / "config/usb_gadget/qkx"
        vals = {p: (g / p).read_text().strip() for p in
                ["idVendor", "idProduct", "functions/ecm.usb0/dev_addr",
                 "functions/ecm.usb0/host_addr", "strings/0x409/manufacturer"]}
        check(vals == {"idVendor": "0x1d6b", "idProduct": "0x0105",
                       "functions/ecm.usb0/dev_addr": "02:51:4b:58:00:02",
                       "functions/ecm.usb0/host_addr": "02:51:4b:58:00:01",
                       "strings/0x409/manufacturer": "quest2-kexec"},
              f"success: gadget IDs/MACs as reference ({vals})")
        r = c.wait_for(r"ready: telnet 10\.42\.0\.2", 10)
        check(r is not None, "success: ready after state 'configured'")
        # telnetd starts in a background subshell: its stub line may land after "ready".
        c.wait_file("stubs.log", r"telnetd -F -l /bin/sh -p 23", 5)
        st = c.stubs()
        check("ifconfig usb0 10.42.0.2 netmask 255.255.255.0 up" in st
              and "udhcpd " in st and "telnetd -F -l /bin/sh -p 23" in st,
              "success: ifconfig/udhcpd/telnetd as reference")
        conf = (c.r / "tmp/udhcpd.conf").read_text()
        check("start 10.42.0.1" in conf and "interface usb0" in conf, "success: udhcpd.conf")
        dm = c.read("tmp/lvr-deadman")
        check(dm.startswith("timeout=600 pid="), f"success: default dead-man timeout 600 ({dm.strip()})")
        dl = c.wait_file("tmp/lvr-deadline", r"deadline_uptime=600 heartbeat=none", 5)
        check(dl is not None, "success: before any heartbeat the deadline is uptime 600 (from boot)")
        # Kernel log follower: 5 seed records once each despite repeated re-reads.
        time.sleep(3)
        kl = (c.r / "tmp/kernel.log").read_text().splitlines()
        check(kl == [f"[{(i + 1) / 1000:12.6f}] seed record {i}" for i in range(5)],
              f"success: kernel.log has each record once, dict line dropped ({len(kl)} lines)")
        w1 = c.wait_for(r"\[(\d+)s\] dead-man: watchdog reset in <= 60 s \(no heartbeat since uptime 0s\)", 30)
        check(w1 is not None and 540 <= up_of(w1) < 580,
              f"success: boot-based warning in the last 60 s before uptime 600 ({w1 and w1.group(1)})")
        f1 = c.wait_for(r"\[(\d+)s\] dead-man: first heartbeat at uptime (\d+)s; reset at uptime (\d+)s", 30)
        check(f1 is not None and int(f1.group(2)) == hb.get(1) and int(f1.group(3)) == hb.get(1, 0) + 600,
              f"success: first heartbeat accepted, deadline = heartbeat + 600 ({f1 and f1.groups()}, wrote {hb})")
        check(f1 is not None and int(f1.group(2)) < 600 and not (c.r / "tmp/lvr-reset").exists(),
              f"success: heartbeat written before the boot-based deadline counts (read at {f1 and f1.group(1)}s); no reset")
        end = time.monotonic() + 40           # past the first heartbeat's deadline (~1180)
        while c.clock.fake < 1200 and time.monotonic() < end:
            time.sleep(0.1)
        dl2 = c.wait_file("tmp/lvr-deadline", r"deadline_uptime=(\d+) heartbeat=(\d+)", 1)
        check(dl2 is not None and 2 in hb and int(dl2.group(2)) == hb[2] and int(dl2.group(1)) == hb[2] + 600,
              f"success: second heartbeat moved the deadline to heartbeat + 600 ({dl2 and dl2.group(0)}, wrote {hb})")
        w2 = c.wait_for(r"\[(\d+)s\] dead-man: watchdog reset in <= 60 s \(no heartbeat since uptime ([1-9]\d*)s\)", 60)
        h2 = hb.get(2, 0)
        check(w2 is not None and int(w2.group(2)) == h2 and h2 + 540 <= up_of(w2) < h2 + 600,
              f"success: second warning 60 s before heartbeat + 600 ({w2 and w2.groups()})")
        check(not (c.r / "tmp/lvr-reset").exists() and not c.holders(),
              "success: no reset at the boot-based 600 s nor at the first heartbeat's deadline")
        check(not (c.r / "tmp/lvr-regdump").exists() and "usbdiag" not in c.console(),
              "success: link up -> regdump never read, no usbdiag line")
        m = c.wait_for(r"\[(\d+)s\] RESET: dead-man: no heartbeat for 600s \(last (\d+), lvr_deadman=600\)", 40)
        check(m is not None and int(m.group(2)) == h2 and h2 + 600 <= up_of(m) <= h2 + 635,
              f"success: dead-man reset 600..635 s after the last heartbeat ({m and m.groups()})")
        c.wait_for(r"held open and never written", 5)
        time.sleep(0.5)
        h = c.holders()
        dpid = int(re.search(r"pid=(\d+)", dm).group(1))
        check(len(h) >= 1 and all(p != c.proc.pid for p, fd in h) and any(p == dpid for p, fd in h),
              f"success: watchdog held by the dead-man subshell {dpid}, not PID 1 ({h})")
        c.common_checks()
    finally:
        c.stop()


def scenario_heartbeat_validate(bb, init, dev):
    """lvr_deadman=120, real-time clock. Bad heartbeats never move the deadline; a
    leading-zero one and the exact `tools/dev heartbeat` command do; stale ones don't."""
    c = Case("heartbeat-validate", bb, init, cmdline="rdinit=/init lvr_deadman=120 panic=0")
    try:
        c.add_udc("configured")
        c.start([(1e9, 1)])
        dl = c.wait_file("tmp/lvr-deadline", r"^deadline_uptime=120 heartbeat=none$", 10)
        check(dl is not None, "hb-validate: deadline 120 from boot before any heartbeat")
        for text, what in [("99999.00", "future uptime"), ("abc", "garbled"),
                           ("1234567", "more than 6 digits"), (".5", "no integer part")]:
            c.heartbeat(text)
            ig = c.wait_for(r"dead-man: ignored heartbeat '" + re.escape(text) + "'", 10)
            check(ig is not None and c.read("tmp/lvr-deadline").strip() == "deadline_uptime=120 heartbeat=none",
                  f"hb-validate: {what} heartbeat '{text}' ignored, deadline unchanged")
        c.heartbeat("0012")
        f = c.wait_for(r"dead-man: first heartbeat at uptime 12s; reset at uptime 132s", 10)
        check(f is not None and c.wait_file("tmp/lvr-deadline", r"^deadline_uptime=132 heartbeat=12$", 3),
              "hb-validate: leading-zero heartbeat 0012 read as 12 (not octal); deadline 132")
        # The exact command `tools/dev heartbeat` sends, run by the target BusyBox with its
        # absolute paths moved under the test root, then parsed by tools/dev itself.
        cmd = under_root(c, dev.HEARTBEAT_CMD)
        p = subprocess.run([str(c.r / "busybox"), "sh", "-c", cmd], env={"PATH": f"{c.r}/bin"},
                           capture_output=True, text=True, timeout=20)
        try:
            info = dev.parse_heartbeat(p.stdout)
        except ValueError as e:
            info = {"error": str(e)}
        up = int(info.get("hb", 0))
        check(p.returncode == 0 and info.get("timeout") == 120 and info.get("reset") == "" and up > 12,
              f"hb-validate: HEARTBEAT_CMD runs on the target shell and parses ({info})")
        dl = c.wait_file("tmp/lvr-deadline", rf"^deadline_uptime={up + 120} heartbeat={up}$", 10)
        check(dl is not None, f"hb-validate: HEARTBEAT_CMD moved the deadline to {up + 120} "
                              f"({c.read('tmp/lvr-deadline').strip()})")
        c.heartbeat("13")
        ig = c.wait_for(r"dead-man: ignored heartbeat '13'", 10)
        check(ig is not None and c.read("tmp/lvr-deadline").strip() == f"deadline_uptime={up + 120} heartbeat={up}",
              "hb-validate: stale heartbeat (older than the last accepted) ignored")
        check(not (c.r / "tmp/lvr-reset").exists(), "hb-validate: no reset during the checks")
        c.common_checks()
    finally:
        c.stop()


def under_root(c, cmd):
    """cmd with its absolute /proc/ and /tmp/ paths moved under the test root (one pass: the root
    itself may live under /tmp)."""
    return re.sub(r"/(proc|tmp)/", lambda m: f"{c.r}/{m.group(1)}/", cmd)


def target_cmd(c, cmd):
    """Run a fixed tools/dev command string with the target BusyBox, absolute /proc/ and /tmp/
    paths moved under the test root (as the device shell would run it)."""
    cmd = under_root(c, cmd)
    return subprocess.run([str(c.r / "busybox"), "sh", "-c", cmd], env={"PATH": f"{c.r}/bin"},
                          capture_output=True, text=True, timeout=20)


RETURN_BOUND = 7     # s from the request to the warm marker: one 5 s pass sleep + pass time + margin
WARM_WAIT = 10       # init.sh WARM_WAIT: warm-reset trigger -> watchdog fallback
TRIGGER = "proc/lvr_warm_reset"
TRIGGER_W = r"^proc/lvr_warm_reset$"   # common_checks writes= for the armed scenarios
BOOT_ID = "4f6c2a1e-9b0d-4c3a-8e7f-123456789abc"


def static_return_poll(init):
    """The return check sits in the dead-man loop before its only sleep (latency <= 1 pass)."""
    text = init.decode()
    body = text[text.index("deadman() {"):text.index("\n}\n", text.index("deadman() {"))]
    i_ret = body.find('[ -e "$R/tmp/lvr-return" ]')
    sleeps = re.findall(r"\bsleep\s+(\S+)", body)
    check(i_ret != -1 and i_ret < body.find("sleep 5") and sleeps == ["5"]
          and 'warm_return "return-android requested"' in body and "force_reset \"return" not in body,
          f"return: /tmp/lvr-return checked at the top of each dead-man pass, before its only sleep 5 ({sleeps})")
    # warm_return's order: armed-reset check, trigger check (not armed -> force_reset),
    # marker, the background sync + trigger write, the bounded wait, the unchanged force_reset; its
    # only caller is the dead-man's return branch; the init never runs reboot.
    w = text[text.index("warm_return() {"):text.index("\n}\n", text.index("warm_return() {"))]
    lines = [l.strip() for l in w.splitlines()]
    seq = ['if [ -e "$R/tmp/lvr-reset" ]; then', 'read -r wr 2>/dev/null < "$R/proc/lvr_warm_reset"',
           'if [ "$wr" != armed ]; then', 'log "LVR-WARM-MARKER ${bid:-no-boot-id} ${upf:-no-uptime}"',
           '(sync; printf r > "$R/proc/lvr_warm_reset") < /dev/null &', 'while [ "$n" -lt $((2 * WARM_WAIT)) ]; do',
           "sleep 1", 'force_reset "$1"']
    idx = [len(lines) - 1 - lines[::-1].index(x) if x in lines else -1 for x in seq]
    fr = [i for i, l in enumerate(lines) if l == 'force_reset "$1"']
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    check(-1 not in idx and idx == sorted(idx) and len(fr) == 3 and fr[1] < idx[3]
          and lines[-1] == 'force_reset "$1"' and re.search(r"(?m)^WARM_WAIT=10$", text)
          and len(re.findall(r"\bwarm_return\b", code)) == 2 and "busybox" not in code
          and not re.search(r"\breboot\b\s+-", code)
          and code.count('> "$R/proc/lvr_warm_reset"') == 1
          and re.findall(r"\bsleep\s+(\S+)", "\n".join(l for l in lines if not l.startswith("#"))) == ["1"],
          f"return: warm_return = armed-reset check, trigger check, marker, background sync + `printf r` to "
          f"/proc/lvr_warm_reset, sleep 1 until 10 s of uptime (<= 20 steps), force_reset; only caller the "
          f"dead-man; no reboot ({idx}, force_reset at {fr})")


def scenario_return_request(bb, init, dev):
    """step 1: `tools/dev return-android`'s fixed command, run by the target
    BusyBox, makes the dead-man loop take force_reset within one pass; real-time clock."""
    c = Case("return-request", bb, init, cmdline="rdinit=/init lvr_deadman=600 panic=10", scale=1)
    try:
        c.add_udc("configured")
        (c.r / "proc/sys/kernel/random").mkdir(parents=True)
        put(c.r / "proc/sys/kernel/random/boot_id", BOOT_ID + "\n")
        put(c.r / TRIGGER, "armed\n")      # kernel trigger armed; the write "returns" here
        c.start([(1e9, 1)])
        check(c.wait_for(r"\] ready: telnet", 15) is not None
              and c.wait_file("tmp/lvr-deadline", r"^deadline_uptime=600 heartbeat=none$", 10) is not None,
              "return: init ready, dead-man running")
        p = target_cmd(c, dev.RETURN_PROBE)
        try:
            kv = dev.parse_kv(p.stdout)
        except ValueError as e:
            kv = {"error": str(e)}
        check(p.returncode == 0 and kv.get("timeout") == "600" and kv.get("reset") == "" and kv.get("wdt") == "",
              f"return: RETURN_PROBE on the target shell parses as the mainline lvr-init ({kv})")
        time.sleep(6)
        check(not (c.r / "tmp/lvr-reset").exists() and "RESET" not in c.console(),
              "return: no reset without a request (one full pass)")
        t0, m0 = c.clock.fake, time.monotonic()
        p = target_cmd(c, dev.RETURN_CMD)
        check(p.returncode == 0 and "return=requested" in p.stdout.split()
              and (c.r / "tmp/lvr-return").exists(),
              f"return: RETURN_CMD creates /tmp/lvr-return on the target shell ({p.stdout.strip()!r})")
        mk = c.wait_for(r"\[(\d+)s\] LVR-WARM-MARKER (\S+) (\S+)\n", RETURN_BOUND + 3)
        dt = time.monotonic() - m0
        check(mk is not None and int(t0) <= up_of(mk) <= t0 + RETURN_BOUND and dt <= RETURN_BOUND
              and mk.group(2) == BOOT_ID and re.fullmatch(r"\d+\.\d\d", mk.group(3)) is not None
              and 0 <= int(float(mk.group(3))) - up_of(mk) <= 1,
              f"return: LVR-WARM-MARKER <boot_id> <uptime> within {RETURN_BOUND} s of the request "
              f"(request at {t0:.1f}, marker {mk and mk.group(0).strip()!r}, {dt:.1f} s real)")
        check(c.wait_file("tmp/lvr-warm", r"^return-android requested \(uptime \d+s\)$", 3) is not None,
              "return: /tmp/lvr-warm holds the reason for tools/dev's poll")
        tw = c.wait_file(TRIGGER, r"^r$", 3)
        time.sleep(0.5)
        st = [l for l in c.stubs().splitlines() if l.split()[0] in WARM_STUBS]
        con = c.console()
        check(tw is not None and st == ["sync "] and (c.r / TRIGGER).read_bytes() == b"r"
              and (c.r / "stubs.log").stat().st_mtime_ns <= (c.r / TRIGGER).stat().st_mtime_ns
              and con.index("LVR-WARM-MARKER") < con.index("warm reset: sync, then r to /proc/lvr_warm_reset"),
              f"return: marker logged, then sync, then exactly 'r' written to /proc/lvr_warm_reset ({st})")
        p = target_cmd(c, dev.RETURN_POLL)
        kv = dev.parse_kv(p.stdout)
        check(not (c.r / "tmp/lvr-reset").exists() and not c.holders() and kv.get("reset") == ""
              and kv.get("warm", "").startswith(dev.RETURN_REASON),
              f"return: during the warm-reset wait no watchdog is open and no reset armed; poll shows warm ({kv})")
        m = c.wait_for(r"\[(\d+)s\] RESET: return-android requested", WARM_WAIT + 5)
        dt = time.monotonic() - m0
        check(m is not None and up_of(mk) + WARM_WAIT <= up_of(m) <= up_of(mk) + WARM_WAIT + 2
              and dt <= RETURN_BOUND + WARM_WAIT + 1,
              f"return: trigger returned -> the same force_reset {WARM_WAIT} s after the marker "
              f"(marker at {mk and mk.group(1)}, RESET at {m and m.group(1)}, {dt:.1f} s real)")
        check(re.search(r"\] warm reset did not happen within 10s; falling back to the watchdog", c.console())
              is not None, "return: fallback logged")
        check(re.search(r"\] return-android: request /tmp/lvr-return seen", c.console()) is not None,
              "return: request logged")
        check(re.fullmatch(r"return-android requested \(uptime \d+s\)\n", c.read("tmp/lvr-reset/reason")) is not None,
              f"return: reset reason ({c.read('tmp/lvr-reset/reason').strip()!r})")
        check(c.wait_for(r"held open and never written: expect the bite", 5) is not None,
              "return: watchdog held open (same path as the dead-man)")
        time.sleep(0.5)
        h = c.holders()
        dpid = int(re.search(r"pid=(\d+)", c.read("tmp/lvr-deadman")).group(1))
        check(len(h) >= 1 and all(p_ != c.proc.pid for p_, fd in h) and any(p_ == dpid for p_, fd in h),
              f"return: watchdog held by the dead-man subshell {dpid} ({h})")
        p = target_cmd(c, dev.RETURN_POLL)
        kv = dev.parse_kv(p.stdout)
        check(kv.get("reset", "").startswith(dev.RETURN_REASON) and kv.get("wdt", "").endswith("/dev/watchdog0"),
              f"return: RETURN_POLL reports the armed reset and the held device ({kv})")
        p = target_cmd(c, dev.HEARTBEAT_CMD)
        info = dev.parse_heartbeat(p.stdout)
        check(info["reset"].startswith(dev.RETURN_REASON),
              "return: a later heartbeat sees the armed reset (the host loop stops; no cancel)")
        time.sleep(6)
        check(c.console().count("RESET: ") == 1 and c.console().count("request /tmp/lvr-return seen") == 1
              and c.console().count("LVR-WARM-MARKER") == 1 and (c.r / TRIGGER).read_bytes() == b"r",
              "return: armed once; the loop does not run another pass; the trigger written once")
        c.common_checks(warm=True, writes=TRIGGER_W)
    finally:
        c.stop()


def scenario_return_nowdt(bb, init, dev):
    """Return request without a qcom_wdt: the existing RESET FAILED path; tools/dev's poll
    sees a reason but no device (it then says to long-press the power button)."""
    c = Case("return-nowdt", bb, init, cmdline="rdinit=/init lvr_deadman=600 panic=0", wdt=False, scale=1)
    try:
        c.add_udc("configured")
        c.start([(1e9, 1)])
        check(c.wait_for(r"\] ready: telnet", 15) is not None, "return-nowdt: init ready")
        target_cmd(c, dev.RETURN_CMD)
        m = c.wait_for(r"RESET FAILED: no qcom_wdt device", RETURN_BOUND + 3)
        kv = dev.parse_kv(target_cmd(c, dev.RETURN_POLL).stdout)
        check(m is not None and kv.get("reset", "").startswith(dev.RETURN_REASON) and kv.get("wdt") == ""
              and "LVR-WARM-MARKER" not in c.console(),
              f"return-nowdt: no trigger -> RESET FAILED at once; poll shows reason without a device ({kv})")
        c.common_checks()
    finally:
        c.stop()


def scenario_return_armed(bb, init, dev):
    """a return request while another reset path is already armed (/tmp/lvr-reset made
    by the harness): no clean reboot, force_reset as before (logs and holds)."""
    c = Case("return-armed", bb, init, cmdline="rdinit=/init lvr_deadman=600 panic=0", scale=1)
    try:
        c.add_udc("configured")
        put(c.r / TRIGGER, "armed\n")      # armed trigger, but another reset path was first
        c.start([(1e9, 1)])
        check(c.wait_for(r"\] ready: telnet", 15) is not None, "return-armed: init ready")
        (c.r / "tmp/lvr-reset").mkdir()
        put(c.r / "tmp/lvr-reset/reason", "harness: another path (uptime 1s)\n")
        target_cmd(c, dev.RETURN_CMD)
        m = c.wait_for(r"reset requested \(return-android requested\) but already armed", RETURN_BOUND + 3)
        time.sleep(1)
        check(m is not None and "warm reset skipped: another reset path is already armed" in c.console()
              and "LVR-WARM-MARKER" not in c.console() and not (c.r / "tmp/lvr-warm").exists()
              and (c.r / TRIGGER).read_text() == "armed\n",
              "return-armed: trigger untouched; force_reset logs 'already armed' and holds")
        c.common_checks()
    finally:
        c.stop()


def scenario_return_warm(bb, init, dev):
    """trigger armed and the warm reset happens: marker, sync, `r` written, and at that
    moment no watchdog open, no reset armed, no RESET line (the harness then stops the init,
    as the reset would)."""
    c = Case("return-warm", bb, init, cmdline="rdinit=/init lvr_deadman=600 panic=10", scale=1)
    try:
        c.add_udc("configured")
        put(c.r / TRIGGER, "armed\n")
        c.start([(1e9, 1)])
        check(c.wait_for(r"\] ready: telnet", 15) is not None, "return-warm: init ready")
        t0 = c.clock.fake
        target_cmd(c, dev.RETURN_CMD)
        tw = c.wait_file(TRIGGER, r"^r$", RETURN_BOUND + 3)
        con = c.console()
        mk = re.search(r"\[(\d+)s\] LVR-WARM-MARKER no-boot-id (\S+)\n", con)
        check(tw is not None and mk is not None and up_of(mk) <= t0 + RETURN_BOUND
              and not (c.r / "tmp/lvr-reset").exists() and not c.holders() and "RESET" not in con
              and [l for l in c.stubs().splitlines() if l.split()[0] in WARM_STUBS] == ["sync "],
              f"return-warm: marker (no boot_id: placeholder) then 'r' to the trigger within {RETURN_BOUND} s; "
              f"no watchdog open, no reset armed when it is written")
        c.common_checks(warm=True, writes=TRIGGER_W)
    finally:
        c.stop()


def _return_not_armed(bb, init, dev, name, content, why):
    """trigger refused / unknown / missing: log why, force_reset at once; no marker, no
    sync, the trigger file unchanged."""
    c = Case(name, bb, init, cmdline="rdinit=/init lvr_deadman=600 panic=10", scale=1)
    try:
        c.add_udc("configured")
        if content is not None:
            put(c.r / TRIGGER, content)
        c.start([(1e9, 1)])
        check(c.wait_for(r"\] ready: telnet", 15) is not None, f"{name}: init ready")
        t0 = c.clock.fake
        target_cmd(c, dev.RETURN_CMD)
        m = c.wait_for(r"\[(\d+)s\] RESET: return-android requested", RETURN_BOUND + 3)
        con = c.console()
        check(m is not None and up_of(m) <= t0 + RETURN_BOUND and re.search(why, con) is not None
              and con.index(re.search(why, con).group(0)) < con.index("RESET: return-android")
              and "LVR-WARM-MARKER" not in con and not (c.r / "tmp/lvr-warm").exists(),
              f"{name}: logs why, then force_reset within {RETURN_BOUND} s of the request; no marker")
        check(c.wait_for(r"held open and never written: expect the bite", 5) is not None,
              f"{name}: watchdog held open (same path as the dead-man)")
        if content is None:
            check(not (c.r / TRIGGER).exists(), f"{name}: no trigger file created")
        c.common_checks()
    finally:
        c.stop()


def scenario_return_refused(bb, init, dev):
    _return_not_armed(bb, init, dev, "return-refused", "refused\n",
                      r"\] warm reset: /proc/lvr_warm_reset says 'refused' \(not armed\); watchdog reset instead")


def scenario_return_garbled(bb, init, dev):
    _return_not_armed(bb, init, dev, "return-garbled", "arme\n",
                      r"\] warm reset: /proc/lvr_warm_reset says 'arme' \(not armed\); watchdog reset instead")


def scenario_return_missing(bb, init, dev):
    _return_not_armed(bb, init, dev, "return-missing", None,
                      r"\] warm reset: no /proc/lvr_warm_reset \(kernel without quest2-warm-reset\); "
                      r"watchdog reset instead")


def scenario_deadman_cmdline(bb, init):
    c = Case("deadman-300", bb, init, cmdline="rdinit=/init lvr_deadman=300 panic=0")
    try:
        c.add_udc("configured")
        c.start([(250, 50), (1e9, 5)])
        m = c.wait_for(r"\[(\d+)s\] RESET: dead-man: no heartbeat for 300s \(last none, lvr_deadman=300\)", 40)
        check(m is not None and 300 <= up_of(m) <= 330,
              f"deadman-300: no heartbeat at all -> reset at uptime 300..330 ({m and m.group(1)})")
        c.common_checks()
    finally:
        c.stop()
    for i, (arg, want) in enumerate([("lvr_deadman=5", 120), ("lvr_deadman=119", 120), ("lvr_deadman=120", 120),
                      ("lvr_deadman=000", 120), ("lvr_deadman=abc", 600), ("lvr_deadman=", 600),
                      ("", 600), ("lvr_deadman=-5", 600), ("lvr_deadman=700 lvr_deadman=*", 600),
                      ("lvr_deadman=700 lvr_deadman=800", 800), ("lvr_deadman=0900", 900),
                      ("lvr_deadman=3600", 3600), ("lvr_deadman=3601", 3600),
                      ("lvr_deadman=99999", 3600), ("lvr_deadman=99999999999999999999999", 3600)]):
        # One directory per case: a dying child of the previous case may still write.
        c = Case(f"deadman-parse-{i:02d}", bb, init, cmdline=f"rdinit=/init {arg}")
        try:
            c.start([(1e9, 1)])
            got = c.wait_file("tmp/lvr-deadman", r"^timeout=\d+ pid=\d+$", 10)
            got = got.group(0) if got else c.read("tmp/lvr-deadman").strip()
            dl = c.wait_file("tmp/lvr-deadline", r"^deadline_uptime=\d+ heartbeat=none$", 10)
            dl = dl.group(0) if dl else c.read("tmp/lvr-deadline").strip()
            check(got.startswith(f"timeout={want} ") and dl == f"deadline_uptime={want} heartbeat=none",
                  f"deadman parse '{arg}' -> {want} ({got}; {dl})")
        finally:
            c.stop()


def scenario_not_configured(bb, init):
    c = Case("not-configured", bb, init)
    try:
        c.add_udc("not attached")
        c.start([(1e9, 20)])
        b = c.wait_for(r"\[(\d+)s\] bound a600000\.usb", 10)
        m = c.wait_for(r"\[(\d+)s\] RESET: USB not configured 120s after bind \(state not attached\)", 20)
        check(bool(b and m), "not-configured: reset when the host never configures the gadget")
        if b and m:
            check(up_of(m) - up_of(b) >= 120, f"not-configured: waited >= 120 s ({up_of(b)} -> {up_of(m)})")
        c.wait_for(r"held open and never written", 5)
        time.sleep(0.5)
        h = c.holders()
        check([p for p, fd in h] == [c.proc.pid], f"not-configured: held by PID-1 stand-in ({h})")
        if b and m:
            check(up_of(m) - up_of(b) <= 120 + 25,
                  f"not-configured: no LEDs -> no code, reset delay <= 25 s ({up_of(m) - up_of(b) - 120}s)")
        check(re.search(r"\] ledcode v2 path=2 flags=RRRRRRRRRRRG ", c.console()) is not None,
              "not-configured: code line logged with path 2 and the UDC flag green")
        c.common_checks()
    finally:
        c.stop()


def scenario_no_watchdog(bb, init):
    c = Case("no-watchdog", bb, init, wdt=False)
    try:
        c.start([(1e9, 20)])
        m = c.wait_for(r"RESET FAILED: no qcom_wdt device", 15)
        check(m is not None, "no-watchdog: logs RESET FAILED, needs long-press")
        check(not (c.r / "proc" / "sysrq-trigger").exists(), "no-watchdog: no sysrq crash")
        c.common_checks()
    finally:
        c.stop()


def scenario_open_fails(bb, init):
    c = Case("open-fails", bb, init)
    try:
        (c.r / "dev" / "watchdog0").chmod(0o444)
        c.start([(1e9, 20)])
        m = c.wait_for(r"open failed; fallback: sysrq crash", 15)
        check(m is not None, "open-fails: falls back to sysrq crash")
        time.sleep(0.3)
        s = c.r / "proc" / "sysrq-trigger"
        check(s.exists() and s.read_text() == "c\n", "open-fails: wrote 'c' to (fake) sysrq-trigger")
        c.common_checks(allow=r"can't create .*/dev/watchdog0: Permission denied",
                        writes=r"^proc/sysrq-trigger$")
    finally:
        c.stop()


def stage_order(con, stages):
    """True if the 'led: stage X' lines appear in this order (each at least once)."""
    seen = re.findall(r"\] led: stage (\S+):", con)
    pos = 0
    for st in seen:
        if pos < len(stages) and st == stages[pos]:
            pos += 1
        elif st not in stages[:pos]:
            return False
    return pos == len(stages)


def scenario_leds_rgb(bb, init):
    """Single-colour red/green/blue LEDs (+ unrelated ones). Real-time clock through the
    link stages, then fast to the dead-man reset (lvr_deadman=120)."""
    c = Case("leds-rgb", bb, init, cmdline="rdinit=/init lvr_deadman=120 panic=0")
    try:
        c.add_led("red:status")
        c.add_led("green:status", brightness="7", trigger="none [heartbeat]")
        c.add_led("blue:heartbeat", trigger="none timer [heartbeat]")
        c.add_led("red:flash", flash=True)
        c.add_led("mmc0::", trigger="[mmc0] none")
        untouched = {n: c.led(n) for n in ["blue:heartbeat", "red:flash", "mmc0::"]}
        c.start([(22, 1), (100, 20), (1e9, 3)],
                events={10: lambda: c.add_udc("not attached"),
                        15: lambda: c.set_udc_state("configured")})
        r = c.wait_led("red:status", {"trigger": "none", "brightness": "511"}, 5)
        g = c.led("green:status")
        check(r.get("brightness") == "511" and r.get("trigger") == "none"
              and g.get("brightness") == "0" and g.get("trigger") == "none",
              f"leds-rgb: init started -> red solid (max_brightness), green off ({r}, {g})")
        check(c.wait_for(r"\] leds: blue:heartbeat green:status mmc0:: red:flash red:status "
                         r"\(red=red:status green=green:status multicolor=\)", 2) is not None,
              "leds-rgb: LED names logged, red/green picked, flash LED skipped")
        g = c.wait_led("green:status", {"trigger": "none", "brightness": "511"}, 10)
        r = c.led("red:status")
        check(g.get("brightness") == "511" and r.get("brightness") == "0" and r.get("trigger") == "none",
              f"leds-rgb: UDC bound -> green solid, red off ({g}, {r})")
        g = c.wait_led("green:status", {"trigger": "heartbeat"}, 10)
        check(g.get("trigger") == "heartbeat" and c.led("red:status").get("brightness") == "0",
              f"leds-rgb: configured -> green heartbeat ({g})")
        m = c.wait_for(r"\[(\d+)s\] RESET: dead-man: no heartbeat for 120s", 30)
        check(m is not None and 120 <= up_of(m) <= 140, f"leds-rgb: dead-man reset still at 120 s ({m and m.group(1)})")
        r = c.wait_led("red:status", {"trigger": "timer", "delay_on": "100", "delay_off": "100"}, 5)
        g = c.led("green:status")
        check(r.get("trigger") == "timer" and r.get("delay_on") == "100" and r.get("delay_off") == "100"
              and r.get("brightness") == "511" and g.get("trigger") == "none" and g.get("brightness") == "0",
              f"leds-rgb: reset armed -> red fast blink 100/100 ms, green off ({r}, {g})")
        con = c.console()
        seen = re.findall(r"led: stage (\S+):", con)
        check(stage_order(con, ["red", "green", "green-hb", "reset"]),
              f"leds-rgb: stages logged in order red, green, green-hb, reset ({seen})")
        ls = re.search(r"\[(\d+)s\] led: stage reset: green:status=off red:status=blink\n", con)
        check(ls is not None and m is not None and up_of(m) <= up_of(ls) <= up_of(m) + 10,
              f"leds-rgb: reset stage logged with the LEDs it set, at the current uptime "
              f"({ls and ls.group(1)}s)")
        check(all(c.led(n) == v for n, v in untouched.items()),
              "leds-rgb: blue (kernel heartbeat), the flash LED and mmc0:: untouched")
        check("ledcode" not in c.console(), "leds-rgb: dead-man path plays no LED code")
        check(not (c.r / "tmp/lvr-regdump").exists() and "usbdiag" not in c.console(),
              "leds-rgb: dead-man path reads no regdump (nothing between it and the reset)")
        h = c.holders()
        check(len(h) == 1 and h[0][1] == 3, f"leds-rgb: watchdog held on fd 3 by one process ({h})")
        c.common_checks()
    finally:
        c.stop()


def scenario_leds_multicolor(bb, init):
    """One multicolour LED; no UDC; devices_deferred present."""
    c = Case("leds-mc", bb, init, cmdline="rdinit=/init lvr_deadman=120")
    try:
        c.add_led("rgb:status", multi_index="red green blue", trigger="[heartbeat] none timer")
        dbg = c.r / "sys/kernel/debug"
        dbg.mkdir(parents=True)
        (dbg / "devices_deferred").write_text(
            "a600000.usb\tplatform: supplier 88e3000.phy not ready\n88e3000.phy\t\n")
        c.start([(1e9, 20)])
        st = c.wait_led("rgb:status", {"multi_intensity": "511 0 0", "trigger": "none"}, 3)
        check(st.get("multi_intensity") == "511 0 0" and st.get("trigger") == "none"
              and st.get("brightness") == "511",
              f"leds-mc: init started -> red solid via multi_intensity ({st})")
        m = c.wait_for(r"\[(\d+)s\] RESET: no UDC after 60s", 15)
        check(m is not None, "leds-mc: no-UDC reset unchanged")
        st = c.wait_led("rgb:status", {"trigger": "timer", "delay_on": "100", "delay_off": "100"}, 5)
        check(st.get("multi_intensity") == "511 0 0" and st.get("trigger") == "timer"
              and st.get("delay_on") == "100" and st.get("delay_off") == "100",
              f"leds-mc: reset armed -> red fast blink ({st})")
        con = c.console()
        check(re.search(r"\] leds: rgb:status \(red= green= multicolor=rgb:status\)", con) is not None,
              "leds-mc: multicolour LED found and logged")
        d1 = re.search(r"\] deferred: a600000\.usb\tplatform: supplier 88e3000\.phy not ready\n", con)
        d2 = re.search(r"\] deferred: 88e3000\.phy\t\n", con)
        check(bool(d1 and d2), "leds-mc: deferred probes logged at the no-UDC timeout")
        time.sleep(0.5)
        h = c.holders()
        check([p for p, fd in h] == [c.proc.pid] and h[0][1] == 3,
              f"leds-mc: watchdog still held by the PID-1 stand-in only ({h})")
        c.common_checks()
    finally:
        c.stop()


def scenario_leds_readonly(bb, init):
    """LED attributes that refuse every write: init and the not-configured reset carry on."""
    c = Case("leds-ro", bb, init)
    try:
        for n in ["red:status", "green:status"]:
            d = c.add_led(n)
            for f in d.iterdir():
                f.chmod(0o444)
            d.chmod(0o555)
        before = {n: c.led(n) for n in ["red:status", "green:status"]}
        c.add_udc("not attached")
        c.start([(1e9, 20)])
        m = c.wait_for(r"\[(\d+)s\] RESET: USB not configured 120s after bind", 20)
        check(m is not None, "leds-ro: not-configured reset unchanged with unwritable LEDs")
        c.wait_for(r"held open and never written", 5)
        time.sleep(1.5)
        h = c.holders()
        check([p for p, fd in h] == [c.proc.pid], f"leds-ro: held by PID-1 stand-in ({h})")
        con = c.console()
        check(re.search(r"\] led: stage red: green:status=off red:status=solid \(some LED writes failed\)", con)
              and re.search(r"\] led: stage reset: .*\(some LED writes failed\)", con),
              "leds-ro: failed LED writes logged, no shell errors")
        check({n: c.led(n) for n in before} == before, "leds-ro: read-only LEDs unchanged")
        c.common_checks()
    finally:
        for n in ["red:status", "green:status"]:
            d = c.r / "sys/class/leds" / n
            if d.exists():
                d.chmod(0o755)
        c.stop()


def scenario_leds_stuck_and_empty(bb, init):
    c = Case("leds-stuck", bb, init, wdt=False)
    try:
        c.add_led("red:status")
        c.add_led("green:status")
        c.start([(1e9, 20)])
        check(c.wait_for(r"RESET FAILED: no qcom_wdt device", 40) is not None,
              "leds-stuck: no watchdog -> RESET FAILED as before")
        r = c.wait_led("red:status", {"trigger": "timer", "delay_on": "1000", "delay_off": "1000"}, 5)
        check(r.get("trigger") == "timer" and r.get("delay_on") == "1000" and r.get("delay_off") == "1000",
              f"leds-stuck: reset impossible -> red slow blink 1000/1000 ms ({r})")
        c.common_checks()
    finally:
        c.stop()
    c = Case("leds-empty", bb, init)
    try:
        (c.r / "sys/class/leds").mkdir(parents=True)
        c.add_udc("configured")
        c.start([(1e9, 1)])
        check(c.wait_for(r"ready: telnet 10\.42\.0\.2", 10) is not None
              and c.wait_for(r"\] led: stage green-hb: no status LED", 5) is not None
              and re.search(r"\] leds: none ", c.console()) is not None,
              "leds-empty: empty /sys/class/leds: ready as usual, stages logged only")
        c.common_checks()
    finally:
        c.stop()


# --- LED diagnostic code -------------------------------------------------------
V2_DEFERRED = (
    "100000.clock-controller\tplatform: wait for supplier /soc@0/rsc@18200000/clock-controller\n"
    "16e0000.interconnect\t\n"
    "88e3000.phy\tplatform: wait for supplier /soc@0/rsc@18200000/clock-controller\n"
    "1500000.interconnect\t\n1620000.interconnect\t\n163d000.interconnect\t\n"
    "90c0000.interconnect\t\n9100000.interconnect\t\n"
    "a6f8800.usb\tplatform: supplier 100000.clock-controller not ready\n"
    "fan\tplatform: wait for supplier /soc@0/rsc@18200000/regulators-1/bob\n")
# A predicted code (v2 line): F9 = 999 (no dwc3 IRQ line, UDC, regdump).
V2_LINE = "ledcode v2 path=1 flags=GBRRRBBBBRRR ndef=10 d=07.1.03,11.0.00,14.1.03 up=63 usb=999 sum=1"
ALL_BOUND = [("80860000.memory", "cmd-db"), ("18200000.rsc", "rpmh"),
             ("18200000.rsc:clock-controller", "clk-rpmh"),
             ("18200000.rsc:regulators-0", "qcom-rpmh-regulator"),
             ("18200000.rsc:bcm-voter", "bcm_voter"), ("100000.clock-controller", "gcc-sm8250"),
             ("16e0000.interconnect", "qnoc-sm8250"), ("88e3000.phy", "qcom-snps-hs-femto-v2-phy"),
             ("a6f8800.usb", "dwc3-qcom-legacy"), ("a600000.usb", "dwc3")]
V2_TREE = dict(bound=[("80860000.memory", "cmd-db"), ("f100000.pinctrl", "sm8250-pinctrl")],
               exists=["18200000.rsc", "100000.clock-controller", "16e0000.interconnect",
                       "1500000.interconnect", "88e3000.phy", "a6f8800.usb", "fan"],
               deferred=V2_DEFERRED)


def make_tree(r, bound=(), exists=(), deferred=None, udc=False, gadget_udc=None,
              interrupts=None, udc_state="not attached\n", speed=None, regdump=None):
    """Fixture sysfs: /sys/devices/platform/soc@0/<name> with a driver link for `bound`,
    /sys/bus/platform/devices/<name> -> it, debugfs devices_deferred (str/bytes, None =
    absent), a /sys/class/udc entry (with state/current_speed text), the gadget's UDC
    attribute, /proc/interrupts and the dwc3 debugfs regdump (str/bytes, FIFO = a FIFO
    nobody writes, None = absent)."""
    devs = r / "sys/devices/platform/soc@0"
    p = r / "sys/bus/platform/devices"
    drv = r / "sys/bus/platform/drivers"
    p.mkdir(parents=True, exist_ok=True)
    for name, driver in [(n, None) for n in exists] + list(bound):
        d = devs / name
        d.mkdir(parents=True, exist_ok=True)
        if not (p / name).is_symlink():
            (p / name).symlink_to(os.path.relpath(d, p))
        if driver:
            (drv / driver).mkdir(parents=True, exist_ok=True)
            (d / "driver").symlink_to(os.path.relpath(drv / driver, d))
    if deferred is not None:
        dbg = r / "sys/kernel/debug"
        dbg.mkdir(parents=True, exist_ok=True)
        (dbg / "devices_deferred").write_bytes(deferred.encode() if isinstance(deferred, str) else deferred)
    if udc:
        (r / "sys/class/udc").mkdir(parents=True, exist_ok=True)
        u = devs / "a600000.usb/udc/a600000.usb"
        u.mkdir(parents=True, exist_ok=True)
        if udc_state is not None:
            (u / "state").write_text(udc_state)
        if speed is not None:
            (u / "current_speed").write_text(speed)
        if not (r / "sys/class/udc/a600000.usb").is_symlink():
            (r / "sys/class/udc/a600000.usb").symlink_to("../../devices/platform/soc@0/a600000.usb/udc/a600000.usb")
    if gadget_udc is not None:
        g = r / "config/usb_gadget/qkx"
        g.mkdir(parents=True, exist_ok=True)
        (g / "UDC").write_text(gadget_udc)
    if interrupts is not None:
        (r / "proc").mkdir(parents=True, exist_ok=True)
        (r / "proc/interrupts").write_bytes(interrupts.encode() if isinstance(interrupts, str) else interrupts)
    if regdump is not None:
        f = r / led_code.REGDUMP
        f.parent.mkdir(parents=True, exist_ok=True)
        if regdump == FIFO:
            os.mkfifo(f)
        else:
            f.write_bytes(regdump.encode() if isinstance(regdump, str) else regdump)


# --- F9 fixtures ---------------------------------------------------------------
FIFO = object()
IRQ_HDR = "".join(f"       CPU{i}" for i in range(8)) + "\n"


def irq_text(counts, name="dwc3", other=True):
    """/proc/interrupts like the device's (8 CPUs): a few lines plus the dwc3 one."""
    t = IRQ_HDR
    if other:
        t += "  11:" + "".join(f" {7 * i + 3:10d}" for i in range(8)) + "     GICv3  27 Level     arch_timer\n"
        t += " 162:" + "".join(f" {0:10d}" for _ in range(8)) + "     GICv3 162 Level     dwc3-qcom-legacy\n"
    t += " 165:" + "".join(f" {c:10d}" for c in counts) + f"     GICv3 165 Level     {name}\n"
    t += " IPI0:" + "".join(f" {100 + i:10d}" for i in range(8)) + "       Rescheduling interrupts\n"
    return t


def regdump_text(gsts=0, cnt=0, **over):
    """A regdump in debugfs_print_regs32 format ("NAME = 0x%08x"), with neighbours."""
    regs = [("GSBUSCFG0", 0xe), ("GCTL", 0x30c12004), ("GSTS", gsts), ("GUCTL1", 0x8000182),
            ("GBUSERRADDR0", 0), ("GBUSERRADDR1", 0), ("GDBGFIFOSPACE", 0x420000),
            ("GEVNTADRLO(0)", 0x9a3c0000), ("GEVNTADRHI(0)", 0), ("GEVNTSIZ(0)", 0x1000),
            ("GEVNTCOUNT(0)", cnt), ("GHWPARAMS8", 0x7e2), ("DCFG", 0x80804), ("DCTL", 0x8cf00a00),
            ("DEVTEN", 0x1217), ("DSTS", 0xd20004), ("DGCMDPAR", 0)]
    return "".join(f"{n} = 0x{over.get(n, v):08x}\n" for n, v in regs)


def ledcode_block(init):
    text = init.decode()
    m = re.search(r"^# BEGIN ledcode\n(.*?)^# END ledcode\n", text, re.S | re.M)
    return m.group(1) if m else ""


def run_block(bb, block, root, script):
    """Source the init's ledcode block in the target BusyBox shell with R=root."""
    t = WORK / "syntax"
    (t / "ledcode.sh").write_text(block)
    pre = f'R={root}\nlog() {{ echo "lvr-init: $*"; }}\n. {t}/ledcode.sh\n'
    p = subprocess.run([str(t / "busybox"), "sh", "-c", pre + script], capture_output=True,
                       text=True, timeout=60, env={"PATH": f"{t}/bin-none"}, cwd=t)
    return p


def scenario_ledcode_unit(bb, init):
    """Shell encoder (archived init) == tools/led_code.py on fixture trees."""
    block = ledcode_block(init)
    check(block.count("lc_encode()") == 1 and "lc_collect()" in block,
          "ledcode-unit: BEGIN/END ledcode block found in the archived init")
    t = WORK / "syntax"
    (t / "bin-none").mkdir(exist_ok=True)
    rl = t / "bin-none" / "readlink"          # the archive links readlink -> busybox
    if not rl.is_symlink():
        rl.symlink_to(t / "busybox")
    many = "".join(f"dev{i}.x\tunknown-drv: some text {i}\n" for i in range(120))
    weird = ("a6f8800.usb\tdwc3-qcom-legacy: *?[ $(reboot) `id` \\\\n ${HOME} '\"\n"
             "no-tab-line\n"
             "88e3000.phy\tplatform: wait for supplier /soc@0/rsc@18200000x/ldo\n")
    fixtures = [
        ("v2-predicted", 1, "63", V2_TREE, V2_LINE),
        ("v3-not-configured", 2, "61",
         dict(bound=ALL_BOUND, deferred="", udc=True, gadget_udc="a600000.usb\n"),
         "ledcode v2 path=2 flags=GGGGGGGGGGRG ndef=0 d=00.0.00,00.0.00,00.0.00 up=61 usb=909 sum=8"),
        ("no-debugfs", 1, "5", dict(exists=["18200000.rsc"]),
         "ledcode v2 path=1 flags=RBRRRRRRRRRR ndef=99 d=00.0.00,00.0.00,00.0.00 up=5 usb=999 sum=3"),
        ("many-unknown", 4, "999999", dict(deferred=many, udc=True, gadget_udc=""),
         "ledcode v2 path=4 flags=RRRRRRRRRRRB ndef=98 d=99.9.00,99.9.00,99.9.00 up=999 usb=909 sum=9"),
        ("weird-text", 1, "0", dict(deferred=weird, exists=["a6f8800.usb"]), None),
        ("unterminated", 2, "1500", dict(deferred="fan\tpwm-fan: failed to get fan-supply"), None),
        ("empty-tree", 1, "0063", dict(), None),
        ("lone-udc", 1, "12", dict(udc=True), None),
        # F9 fixtures (init at uptime 0-2 now that the beacon is off).
        ("f9-stall-000", 2, "1", dict(bound=ALL_BOUND, deferred="", udc=True, gadget_udc="a600000.usb\n",
                                      interrupts=irq_text([0] * 8), udc_state="powered\n",
                                      speed="UNKNOWN\n", regdump=regdump_text()),
         "ledcode v2 path=2 flags=GGGGGGGGGGRG ndef=0 d=00.0.00,00.0.00,00.0.00 up=1 usb=000 sum=4"),
        ("f9-noirq-001", 2, "0", dict(bound=ALL_BOUND, udc=True, gadget_udc="a600000.usb\n",
                                      interrupts=irq_text([0] * 8), regdump=regdump_text(cnt=0x4)),
         "ledcode v2 path=2 flags=GGGGGGGGGGRG ndef=99 d=00.0.00,00.0.00,00.0.00 up=0 usb=001 sum=2"),
        ("f9-buserr-112", 2, "2", dict(bound=ALL_BOUND, udc=True, gadget_udc="a600000.usb\n",
                                       interrupts=irq_text([5, 0, 0, 0, 0, 0, 0, 0]), udc_state="default\n",
                                       speed="high-speed\n",
                                       regdump=regdump_text(gsts=0x10, GBUSERRADDR0=0x9a3c0000)),
         None),
        ("f9-redirect-200", 2, "1", dict(udc=True, interrupts=irq_text([800, 102, 0, 0, 0, 0, 0, 0]),
                                         regdump=regdump_text(gsts=0x30000000)), None),
        ("f9-configured-330", 2, "1", dict(udc=True, interrupts=irq_text([1500] + [0] * 7),
                                           udc_state="configured\n", regdump=regdump_text()), None),
        ("f9-both-243", 1, "2", dict(udc=True, interrupts=irq_text([9, 3] + [0] * 6), udc_state="suspended\n",
                                     regdump=regdump_text(gsts=0x10, cnt=0x8)), None),
        ("f9-partial-regdump", 1, "1", dict(udc=True, interrupts=irq_text([1] * 8), udc_state="addressed\n",
                                            regdump="GSTS = 0x00000010\nGEVNTCOUNT(0) = 0x123456789\n"
                                                    "GEVNTCOUNT(0) = 0xzz\nDSTS 0x1\n"), None),
        ("f9-huge-irq", 1, "1", dict(interrupts=IRQ_HDR + " 165: 123456789012 0000000000099999999 7 GICv3 dwc3\n"
                                               " 166: 5 dwc3\n", regdump=""), None),
        ("f9-weird", 1, "1", dict(udc=True, udc_state="$(reboot) `id` *\n", speed="high speed!\n",
                                  interrupts=IRQ_HDR + " 1: * $(reboot) dwc3 x\n 2:\t3\t\t4 x\tdwc3\t\n",
                                  regdump="GSTS = 0x00000000 \n$(reboot) = 0x1\nGSTS = 0x00000010\n"
                                          "GEVNTCOUNT(0) = 0x0000000c = 0x0\nGEVNTCOUNT(0) = 0x00000000\n"
                                          "DCTL = 0x\nDCTL=0x1\nGEVNTSIZ(0) = 0X00000001\n"), None),
        ("f9-no-dwc3-line", 1, "1", dict(udc=True, udc_state="\n", speed="", interrupts=irq_text([1] * 8, "xhci-hcd:usb1"),
                                         regdump=b"\xff\xfe = 0x1\n"), None),
    ]
    for name, path, up, tree, want in fixtures:
        r = WORK / "ledcode-unit" / name
        shutil.rmtree(r, ignore_errors=True)
        r.mkdir(parents=True)
        make_tree(r, **tree)
        out, f, ok = block_vs_python(bb, block, r, path, up)
        tl, cs = led_code.encode_fields(f)
        check(ok, f"ledcode-unit {name}: shell encoder == tools/led_code.py "
                  f"({out[0]!r} vs {led_code.fields_line(f, cs)!r})")
        if want:
            check(out[0] == want, f"ledcode-unit {name}: fields as specified ({want})")
        if name in ("weird-text", "f9-weird"):
            check(not (r / "reboot").exists() and not (WORK / "syntax" / "reboot").exists()
                  and "id" not in out[0] and "uid=" not in "".join(out),
                  f"ledcode-unit {name}: fixture text never evaluated")
        if name.startswith("f9-") and name[-3:].isdigit():
            check(f["usb"] == tuple(int(x) for x in name[-3:]) and out[0].endswith(f"usb={name[-3:]} sum={cs}"),
                  f"ledcode-unit {name}: F9 = {name[-3:]} ({out[0][-14:]})")
        if name == "v2-predicted":
            check(out[1].split() == ["80860000.memory=cmd-db", "18200000.rsc=-", "100000.clock-controller=-",
                                     "16e0000.interconnect=-", "88e3000.phy=-", "a6f8800.usb=-"],
                  f"ledcode-unit v2-predicted: bound driver names logged ({out[1].strip()})")
    # Hand-checked usbdiag lines and digits for the edge-case F9 fixtures.
    want_usb = {
        "f9-stall-000": ("irq=0 state=powered speed=UNKNOWN GSTS=0x00000000 BUSERR=0x00000000:0x00000000 "
                         "EVADR=0x00000000:0x9a3c0000 EVSIZ=0x00001000 EVCNT=0x00000000 DCTL=0x8cf00a00 "
                         "DEVTEN=0x00001217 DSTS=0x00d20004 f9=000"),
        "f9-buserr-112": ("irq=5 state=default speed=high-speed GSTS=0x00000010 BUSERR=0x00000000:0x9a3c0000 "
                          "EVADR=0x00000000:0x9a3c0000 EVSIZ=0x00001000 EVCNT=0x00000000 DCTL=0x8cf00a00 "
                          "DEVTEN=0x00001217 DSTS=0x00d20004 f9=112"),
        "f9-partial-regdump": ("irq=8 state=addressed speed=na GSTS=0x00000010 BUSERR=na:na EVADR=na:na "
                               "EVSIZ=na EVCNT=na DCTL=na DEVTEN=na DSTS=na f9=129"),
        "f9-huge-irq": ("irq=99999999 state=na speed=na GSTS=na BUSERR=na:na EVADR=na:na EVSIZ=na EVCNT=na "
                        "DCTL=na DEVTEN=na DSTS=na f9=399"),
        "f9-weird": ("irq=7 state=other speed=other GSTS=0x00000010 BUSERR=na:na EVADR=na:na EVSIZ=na "
                     "EVCNT=0x00000000 DCTL=na DEVTEN=na DSTS=na f9=192"),
        "f9-no-dwc3-line": ("irq=na state=na speed=na GSTS=na BUSERR=na:na EVADR=na:na EVSIZ=na EVCNT=na "
                            "DCTL=na DEVTEN=na DSTS=na f9=999"),
        "lone-udc": ("irq=na state=not_attached speed=na GSTS=na BUSERR=na:na EVADR=na:na EVSIZ=na EVCNT=na "
                     "DCTL=na DEVTEN=na DSTS=na f9=909"),
    }
    for name, want in want_usb.items():
        r = WORK / "ledcode-unit" / name
        out, f, ok = block_vs_python(bb, block, r, 1, "1")
        check(ok and out[4] == want, f"ledcode-unit {name}: usbdiag summary as hand-checked ({out[4]!r})")
    # Random trees: shell == Python for F9 and the usbdiag line.
    rng = __import__("random").Random(20)
    states = ["not attached", "attached", "powered", "default", "addressed", "configured", "suspended",
              "reconnecting", "", " default", "configured ", "x y"]
    speeds = ["high-speed", "full-speed", "UNKNOWN", "super-speed-plus", "", "low speed", "x_y"]
    names = ["GSTS", "GEVNTCOUNT(0)", "GBUSERRADDR0", "GBUSERRADDR1", "GEVNTADRLO(0)", "GEVNTADRHI(0)",
             "GEVNTSIZ(0)", "DCTL", "DEVTEN", "DSTS", "GCTL", "GSTS ", "gsts", "GEVNTCOUNT(1)"]
    vals = ["0x00000000", "0x00000010", "0x0000fffc", "0x00000003", "0xffffffff", "0x80000004", "0x1",
            "0x123456789", "0x", "1", "0xg0000000", "0x0000001F"]
    bad_fuzz = []
    for i in range(40):
        r = WORK / "ledcode-unit" / f"fuzz-{i:02d}"
        shutil.rmtree(r, ignore_errors=True)
        r.mkdir(parents=True)
        lines = []
        for _ in range(rng.randint(0, 6)):
            toks = [f"{rng.randint(0, 300)}:"] + [rng.choice(["0", "0", "0", "1", "7", "00012", "999999999",
                                                              "123456789012", str(rng.randint(0, 600))])
                                                  for _ in range(rng.randint(0, 9))]
            toks += rng.sample(["GICv3", "165", "Level", "Edge", "pdc"], rng.randint(0, 3))
            toks.append(rng.choice(["dwc3", "dwc3", "dwc3-qcom", "arch_timer", "dwc3 x"]))
            lines.append(rng.choice([" ", "\t", "  "]).join(toks))
        rdl = [f"{rng.choice(names)} = {rng.choice(vals)}" for _ in range(rng.randint(0, 14))]
        if rng.random() < 0.7:          # mostly a usable GSTS + GEVNTCOUNT(0) somewhere
            for n in ("GSTS", "GEVNTCOUNT(0)"):
                rdl.insert(rng.randint(0, len(rdl)), f"{n} = {rng.choice(vals[:7])}")
        rd = "".join(x + "\n" for x in rdl)
        make_tree(r, udc=rng.random() < 0.8, udc_state=rng.choice(states) + rng.choice(["\n", ""]),
                  speed=rng.choice(speeds + [None]),
                  interrupts=(IRQ_HDR + "\n".join(lines)) if rng.random() < 0.9 else None,
                  regdump=rd if rng.random() < 0.85 else None)
        out, f, ok = block_vs_python(bb, block, r, rng.randint(1, 4), str(rng.randint(0, 3)))
        if not ok:
            bad_fuzz.append((i, out[4:5], f["usbdiag"]))
    check(not bad_fuzz, f"ledcode-unit: 40 random F9 trees: shell == tools/led_code.py ({bad_fuzz[:1]})")
    # Reason categories and supplier indices, one reason per line.
    reasons = [
        "", "platform: ", "platform: wait for supplier /psci/power-domain-cpu-cluster0",
        "platform: wait for supplier /soc@0/rsc@18200000/clock-controller",
        "platform: wait for supplier /soc@0/rsc@18200000/regulators-1/bob",
        "platform: wait for supplier /soc@0/usb@a6f8800/usb@a600000/port",
        "platform: wait for supplier /soc@0/rsc@18200000x", "platform: wait for supplier /nowhere",
        "wait for supplier /fan", "platform: supplier fan not ready",
        "platform: supplier c440000.spmi:pmic@5:pwm not ready", "platform: supplier nosuch not ready",
        "supplier not ready", "supplier  not ready",
        "qcom-rpmh-regulator: failed to get supply", "qcom-snps-hs-femto-v2-phy: failed to get ref clk",
        "dwc3: failed to get usb2 phy", "dwc3: USB2 PHY", "dwc3-qcom-legacy: failed to get icc path",
        "x: power domain not ready", "rpmh: Command DB not available", "x: cmd foo",
        "x: something else", "some driver: phy fail", "a:b: c", ": wait for supplier /psci",
        "gcc: clk supply missing", "*?[ $(reboot) `id`", "x: rpmh-rsc down",
    ]
    t = WORK / "syntax"
    (t / "reasons.txt").write_text("\n".join(reasons) + "\n")
    p = run_block(bb, block, WORK / "ledcode-unit" / "v2-predicted",
                  f'while IFS= read -r l; do lc_reason "$l"; echo "$LC_C $LC_SS"; done < {t}/reasons.txt')
    got = p.stdout.split("\n")[:len(reasons)]
    want = ["%d %02d" % led_code.reason_category(x) for x in reasons]
    check(got == want, f"ledcode-unit: reason category/supplier of {len(reasons)} reasons == tools/led_code.py "
                       f"({[(r, g, w) for r, g, w in zip(reasons, got, want) if g != w][:2]})")
    check({int(w.split()[0]) for w in want} == set(range(10)), "ledcode-unit: every category 0-9 exercised")
    check(want[2] == "1 28" and want[3] == "1 03" and want[4] == "1 05" and want[5] == "1 16"
          and want[6] == "1 99" and want[9] == "2 18" and want[11] == "2 99",
          "ledcode-unit: longest-prefix DT path and exact-name supplier lookups as the reference encoder")
    # LEDCODE_MAX covers 3 worst-case cycles with 5 % write overhead, 5 s setup and the
    # regdump read limit.
    text = init.decode()
    mx = ledcode_max(init)
    cy = int(re.search(r"^LEDCODE_CYCLES=(\d+)$", text, re.M).group(1))
    rw = int(re.search(r"^LC_RD_WAIT=(\d+)$", text, re.M).group(1))
    check(cy == 3 and 1 <= rw <= 10 and mx >= 3 * led_code.worst_cycle() * 1.05 + 5 + rw,
          f"ledcode-unit: LEDCODE_CYCLES=3, LEDCODE_MAX={mx} >= 3 x {led_code.worst_cycle():.2f} s x 1.05 + 5"
          f" + LC_RD_WAIT {rw}")
    code = [l for l in text.splitlines() if not l.lstrip().startswith("#")]
    for bad in ("debug/pinctrl", "debug/gpio", "pinconf", "pinmux"):
        check(not any(bad in l for l in code), f"ledcode-unit: init never touches debugfs {bad}")
    # dwc3 debugfs: regdump only (lsp_dump, GDBGEPINFO and the fifo/queue files write
    # debug-mux registers on read; mode/testmode/link_state are writable).
    for bad in ("lsp_dump", "GDBGEPINFO", "fifo_size", "request_queue", "rx_info_queue",
                "descriptor_fetch_queue", "event_queue", "trb_ring", "transfer_type", "link_state",
                "testmode", "a600000.usb/mode", "/dev/mem", "15000000"):
        check(not any(bad in l for l in code), f"ledcode-unit: init never names {bad}")
    dbg = [l for l in code if "kernel/debug" in l or "debug/usb" in l]
    check(all(">" not in l.replace("2>/dev/null", "") for l in dbg)
          and [l.strip() for l in dbg if "debug/usb" in l] == ["ud_src=$R/sys/kernel/debug/usb/a600000.usb/regdump"],
          f"ledcode-unit: the only dwc3 debugfs path is usb/a600000.usb/regdump; no debugfs write ({len(dbg)} lines)")
    uses = [l.strip() for l in code if '"$ud_src"' in l]
    check(uses == ['if [ ! -e "$ud_src" ]; then',
                   '(cat "$ud_src" 2>/dev/null > "$ud_d/copy"; echo $? 2>/dev/null > "$ud_d/rc") &'],
          f"ledcode-unit: regdump only tested for existence and read by one cat ({uses})")


def ledcode_max(init):
    return int(re.search(r"^LEDCODE_MAX=(\d+)$", init.decode(), re.M).group(1))


def block_vs_python(bb, block, r, path, up):
    """Run lc_collect/lc_encode on tree r in the target shell (regdump parsed straight from
    the fixture) and compare with tools/led_code.py. Returns (output lines, fields, ok)."""
    p = run_block(bb, block, r, f'LC_RD=$R/{led_code.REGDUMP}; lc_collect {path} {up}; lc_encode; '
                                'printf "%s\\n%s\\n%s\\n%s\\n%s\\n" "$LC_LINE" "$LC_DRV" "$LC_TL" "$LC_MS" '
                                '"$LC_USBLINE"')
    out = p.stdout.split("\n") + [""] * 5
    f = led_code.collect(str(r), path, int(up))
    tl, cs = led_code.encode_fields(f)
    ok = (p.returncode == 0 and not p.stderr
          and out[0] == led_code.fields_line(f, cs)
          and out[1].strip() == " ".join(f["drivers"])
          and out[2] == led_code.timeline_str(tl)
          and int(out[3] or -1) == round(led_code.cycle_seconds(tl) * 1000)
          and out[4] == f["usbdiag"])
    if p.stderr:
        print("   stderr:", p.stderr[:200])
    return out, f, ok


class LedRecorder(threading.Thread):
    """Polls the brightness files of the given LEDs; records (monotonic_s, colour)."""
    COL = {(0, 0, 0): "dark", (1, 0, 0): "red", (0, 1, 0): "green", (0, 0, 1): "blue",
           (1, 1, 1): "white"}

    def __init__(self, case, names):
        super().__init__(daemon=True)
        self.files = [case.r / "sys/class/leds" / n / "brightness" for n in names]
        self.events, self.values, self.stop = [], set(), False

    def run(self):
        last, cur = None, [0] * len(self.files)
        while not self.stop:
            for i, f in enumerate(self.files):
                try:
                    v = f.read_text().strip()
                    if v:
                        cur[i] = int(v)
                        self.values.add(cur[i])
                except (OSError, ValueError):
                    pass
            col = self.COL.get(tuple(int(v > 0) for v in cur), "?")
            if col != last:
                self.events.append((time.monotonic(), col))
                last = col
            time.sleep(0.001)


def decode_recording(rec, div, t0):
    ev = [((t - t0) * div, c) for t, c in rec.events]
    return led_code.decode(ev, end=(time.monotonic() - t0) * div, min_ms=100)


def fields_of(c):
    return {k: c[k] for k in ("path", "flags", "ndef", "entries", "up", "usb")}


def rgb_leds(c):
    c.add_led("red:status")
    c.add_led("green:status")
    c.add_led("blue:heartbeat", trigger="none timer [heartbeat]")


def scenario_ledcode_noudc(bb, init):
    """v2-like tree, three single-colour LEDs, lvr_deadman=900: the code plays 3 cycles
    at 8x, is recorded and decoded, then the no-UDC reset as before."""
    S = 8
    c = Case("ledcode-noudc", bb, init, cmdline="rdinit=/init lvr_deadman=900", scale=1)
    try:
        rgb_leds(c)
        make_tree(c.r, **V2_TREE)
        f = led_code.collect(str(c.r), 1, 63)
        tl, cs = led_code.encode_fields(f)
        cyc = led_code.cycle_seconds(tl)
        rec = LedRecorder(c, ["red:status", "green:status", "blue:heartbeat"])
        rec.start()
        t0 = time.monotonic()
        c.start([(1e9, S)], div=S, clock_start=63.0)
        n = c.wait_for(r"\[(\d+)s\] no UDC after 60s", 20)
        line = c.wait_for(r"\] (ledcode v2 [^\n]*)\n", 10)
        check(line is not None and line.group(1) == V2_LINE == led_code.fields_line(f, cs),
              f"ledcode-noudc: code line = spec example = tools/led_code.py ({line and line.group(1)})")
        check(c.wait_for(r"\] ledcode drivers: 80860000\.memory=cmd-db 18200000\.rsc=- ", 2) is not None,
              "ledcode-noudc: bound driver names logged next to the code")
        check(c.wait_for(r"\] led: paused for the LED code \(pid \d+\)", 5) is not None,
              "ledcode-noudc: led_worker pauses while the code plays")
        m = c.wait_for(r"\[(\d+)s\] RESET: no UDC after 60s", 3 * cyc / S + 30)
        rec.stop = True
        check(bool(n and m), "ledcode-noudc: no-UDC reset after the code")
        if n and m:
            d = up_of(m) - up_of(n)
            lo, hi = 3 * cyc, 3 * cyc + 1 + 0.05 * 3 * cyc + 12
            check(lo <= d <= hi, f"ledcode-noudc: reset {d}s after the no-UDC timeout, within "
                                 f"3 cycles {lo:.1f}..{hi:.1f}s (cycle {cyc:.2f}s)")
        cycles = decode_recording(rec, S, t0)
        good = [x for x in cycles if x["check_ok"]]
        check(len(cycles) == 3 and len(good) == 3 and all(fields_of(x) == {**fields_of(good[0])} for x in good)
              and fields_of(good[0]) == {"path": 1, "flags": "GBRRRBBBBRRR", "ndef": 10,
                                         "entries": [(7, 1, 3), (11, 0, 0), (14, 1, 3)], "up": 63,
                                         "usb": (9, 9, 9)},
              f"ledcode-noudc: recorded LED writes decode to 3 cycles of the expected code "
              f"({[(x['t_start'], x['errors'][:1]) for x in cycles]})")
        check(rec.values <= {0, 255, 511} and 255 in rec.values,
              f"ledcode-noudc: code brightness 255 on max_brightness 511 LEDs ({sorted(rec.values)})")
        check(c.wait_for(r"\] ledcode: 3 cycle\(s\) done, LEDs handed back", 2) is not None,
              "ledcode-noudc: player ends after 3 cycles and hands the LEDs back")
        r = c.wait_led("red:status", {"trigger": "timer", "delay_on": "100", "delay_off": "100"}, 5)
        b = c.led("blue:heartbeat")
        check(r.get("trigger") == "timer" and r.get("brightness") == "511",
              f"ledcode-noudc: then the reset stage: red fast blink ({r})")
        check(b.get("trigger") == "heartbeat" and b.get("brightness") == "0"
              and set(b) == {"brightness", "max_brightness", "trigger"},
              f"ledcode-noudc: blue: only brightness/trigger written, kernel heartbeat restored ({b})")
        c.wait_for(r"held open and never written", 5)
        time.sleep(0.5)
        h = c.holders()
        check([p for p, fd in h] == [c.proc.pid] and h[0][1] == 3,
              f"ledcode-noudc: watchdog held on fd 3 by the PID-1 stand-in ({h})")
        c.common_checks()
    finally:
        c.stop()


def scenario_ledcode_notcfg(bb, init):
    S = 20
    c = Case("ledcode-notcfg", bb, init, cmdline="rdinit=/init lvr_deadman=900", scale=1)
    try:
        rgb_leds(c)
        make_tree(c.r, bound=ALL_BOUND, deferred="")
        c.add_udc("not attached")
        c.start([(1e9, S)], div=S)
        b = c.wait_for(r"\[(\d+)s\] bound a600000\.usb", 10)
        line = c.wait_for(r"\] (ledcode v2 [^\n]*)\n", 20)
        want = "ledcode v2 path=2 flags=GGGGGGGGGGRG ndef=0 d=00.0.00,00.0.00,00.0.00 up=5 usb=909 sum=6"
        check(line is not None and line.group(1) == want,
              f"ledcode-notcfg: path 2 code, all flags green but QMP, ndef 0 ({line and line.group(1)})")
        f = {"path": 2, "flags": "GGGGGGGGGGRG", "ndef": 0, "entries": [], "up": 5, "usb": (9, 0, 9)}
        cyc = led_code.cycle_seconds(led_code.encode_fields(f)[0])
        m = c.wait_for(r"\[(\d+)s\] RESET: USB not configured 120s after bind", 3 * cyc / S + 20)
        check(bool(b and m), "ledcode-notcfg: not-configured reset after the code")
        if b and m:
            d = up_of(m) - up_of(b) - 120
            lo, hi = 3 * cyc, 3 * cyc + 1 + 0.05 * 3 * cyc + 25
            check(lo <= d <= hi, f"ledcode-notcfg: reset {d}s after the 120 s wait, within {lo:.1f}..{hi:.1f}s")
        c.wait_for(r"held open and never written", 5)
        time.sleep(0.5)
        h = c.holders()
        check([p for p, fd in h] == [c.proc.pid], f"ledcode-notcfg: held by the PID-1 stand-in ({h})")
        c.common_checks()
    finally:
        c.stop()


def scenario_ledcode_deadline(bb, init):
    """lvr_deadman=300: the code must give way 60 s before the dead-man deadline; the
    no-UDC reset (not the dead-man) fires at ~240 and the player stops within a symbol."""
    c = Case("ledcode-deadline", bb, init, cmdline="rdinit=/init lvr_deadman=300", scale=1)
    try:
        rgb_leds(c)
        make_tree(c.r, **V2_TREE)
        c.start([(1e9, 20)], div=20)
        m = c.wait_for(r"\[(\d+)s\] RESET: no UDC after 60s", 25)
        check(m is not None and 240 <= up_of(m) <= 252,
              f"ledcode-deadline: no-UDC reset at dead-man deadline - 60 (240..252: {m and m.group(1)})")
        check(c.wait_for(r"\] ledcode: dead-man deadline 300s is <= 60 s away; resetting now", 2) is not None,
              "ledcode-deadline: logged why the code was cut short")
        s = c.wait_for(r"\[(\d+)s\] ledcode: reset armed; stopped in cycle (\d), LEDs handed back", 5)
        check(s is not None and m is not None and up_of(s) - up_of(m) <= 10,
              f"ledcode-deadline: player stops within one symbol of the reset ({s and s.groups()})")
        r = c.wait_led("red:status", {"trigger": "timer", "delay_on": "100"}, 5)
        check(r.get("trigger") == "timer" and c.led("blue:heartbeat").get("trigger") == "heartbeat",
              f"ledcode-deadline: LEDs back to the reset stage, blue heartbeat restored ({r})")
        d = c.wait_for(r"reset requested \(dead-man: no heartbeat for 300s .*\) but already armed", 10)
        check(d is not None, "ledcode-deadline: the dead-man still runs and finds the reset armed")
        h = c.holders()
        check([p for p, fd in h] == [c.proc.pid], f"ledcode-deadline: one holder ({h})")
        c.common_checks()
    finally:
        c.stop()


def scenario_ledcode_hung_led(bb, init):
    """An LED write that never returns (red brightness is a FIFO nobody reads): led_worker
    and the player hang, init still resets at LEDCODE_MAX after the no-UDC timeout."""
    c = Case("ledcode-hung", bb, init, cmdline="rdinit=/init lvr_deadman=1800", scale=1)
    try:
        d = c.add_led("red:status")
        (d / "brightness").unlink()
        os.mkfifo(d / "brightness")
        c.add_led("green:status")
        c.start([(1e9, 50)], div=50)
        n = c.wait_for(r"\[(\d+)s\] no UDC after 60s", 10)
        m = c.wait_for(r"\[(\d+)s\] RESET: no UDC after 60s", 20)
        mx = ledcode_max(init)
        check(c.wait_for(rf"\] ledcode: time limit \({mx}s\) reached", 1) is not None,
              f"ledcode-hung: LEDCODE_MAX ({mx} s) limit logged")
        check(bool(n and m) and mx <= up_of(m) - up_of(n) <= mx + 30,
              f"ledcode-hung: reset LEDCODE_MAX ({mx}..{mx + 30} s) after the no-UDC timeout "
              f"({n and m and up_of(m) - up_of(n)})")
        c.wait_for(r"held open and never written", 5)
        time.sleep(0.5)
        h = c.holders()
        check([p for p, fd in h] == [c.proc.pid] and h[0][1] == 3,
              f"ledcode-hung: watchdog held by the PID-1 stand-in although LED writes hang ({h})")
        c.common_checks()
    finally:
        c.stop()


def scenario_ledcode_nowdt(bb, init):
    """No qcom_wdt: path-1 code, RESET FAILED, then the path-4 code alternating with the
    slow red blink until a long press."""
    c = Case("ledcode-nowdt", bb, init, cmdline="rdinit=/init lvr_deadman=900", wdt=False, scale=1)
    try:
        rgb_leds(c)
        make_tree(c.r, **V2_TREE)
        c.start([(1e9, 20)], div=20)
        check(c.wait_for(r"\] ledcode: 3 cycle\(s\) done", 30) is not None,
              "ledcode-nowdt: path-1 code played first")
        check(c.wait_for(r"RESET FAILED: no qcom_wdt device", 5) is not None,
              "ledcode-nowdt: then RESET FAILED as before")
        p4 = c.wait_for(r"\] (ledcode v2 path=4 [^\n]*)\n", 5)
        check(p4 is not None and p4.group(1) == V2_LINE.replace("path=1", "path=4").replace("up=63", "up=5")
              .replace("sum=1", "sum=" + str(led_code.checksum(4, "GBRRRBBBBRRR", 10,
                                                                  [(7, 1, 3), (11, 0, 0), (14, 1, 3)], 5,
                                                                  (9, 9, 9)))),
              f"ledcode-nowdt: path-4 code line ({p4 and p4.group(1)})")
        r = c.wait_led("red:status", {"trigger": "timer", "delay_on": "1000", "delay_off": "1000"}, 5)
        check(r.get("trigger") == "timer" and r.get("delay_on") == "1000",
              f"ledcode-nowdt: slow red blink between code cycles ({r})")
        r = c.wait_led("red:status", {"trigger": "none"}, led_code_gap() + 10)
        check(r.get("trigger") == "none", "ledcode-nowdt: code resumes after the stuck gap")
        check(not (c.r / "proc" / "sysrq-trigger").exists(), "ledcode-nowdt: no sysrq crash")
        c.common_checks()
    finally:
        c.stop()


# --- F9 / usbdiag full-init scenarios -------------------------------------------
def scenario_usbdiag_notcfg(bb, init):
    """Beacon off: init at uptime 0 (F7 000). UDC bound, host never configures it; dwc3
    IRQs 902, state not attached, regdump with a bus error and pending events: F9 = 203.
    The code (with F9) is recorded and decoded; one usbdiag line; reset bounds.
    S = 8, as ledcode_noudc: at 20 a 100 ms symbol lasts 5 ms real, and the
    1 ms LED poll then misreads symbols whenever other scenarios or suites share the CPU."""
    S = 8
    c = Case("usbdiag-notcfg", bb, init, cmdline="rdinit=/init lvr_deadman=900", scale=1)
    rec = None
    try:
        rgb_leds(c)
        make_tree(c.r, bound=ALL_BOUND, deferred="", interrupts=irq_text([800, 102, 0, 0, 0, 0, 0, 0]),
                  regdump=regdump_text(gsts=0x10, cnt=0x8, GBUSERRADDR0=0x9a3c0040))
        c.add_udc("not attached")
        put(c.r / "sys/devices/platform/soc@0/a600000.usb/udc/a600000.usb/current_speed", "high-speed\n")
        rec = LedRecorder(c, ["red:status", "green:status", "blue:heartbeat"])
        rec.start()
        t0 = time.monotonic()
        # real-time speed for the first fake 0.7 s so the takeover (qemu-user forks)
        # still lets init reach its first stage at uptime 0.
        c.start([(1.0, 1), (1e9, S)], div=S, clock_start=0.3)
        r0 = c.wait_for(r"\[(\d+)s\] reached ", 10)
        b = c.wait_for(r"\[(\d+)s\] bound a600000\.usb", 10)
        check(r0 is not None and up_of(r0) == 0, f"usbdiag-notcfg: init reached at uptime 0 ({r0 and r0.group(1)})")
        line = c.wait_for(r"\] (ledcode v2 [^\n]*)\n", 20)
        f = led_code.collect(str(c.r), 2, 0)
        tl, cs = led_code.encode_fields(f)
        cyc = led_code.cycle_seconds(tl)
        check(line is not None and line.group(1) == led_code.fields_line(f, cs)
              and line.group(1).endswith(" up=0 usb=203 sum=%d" % cs),
              f"usbdiag-notcfg: code line with F7 000 and F9 203 == tools/led_code.py ({line and line.group(1)})")
        ud = c.wait_for(r"\[(\d+)s\] usbdiag: t=(\d+) (.*) regdump=(\w+)\n", 2)
        want = ("irq=902 state=not_attached speed=high-speed GSTS=0x00000010 BUSERR=0x00000000:0x9a3c0040 "
                "EVADR=0x00000000:0x9a3c0000 EVSIZ=0x00001000 EVCNT=0x00000008 DCTL=0x8cf00a00 "
                "DEVTEN=0x00001217 DSTS=0x00d20004 f9=203")
        check(ud is not None and ud.group(3) == want == f["usbdiag"] and ud.group(4) == "ok",
              f"usbdiag-notcfg: usbdiag line ({ud and ud.group(0).strip()})")
        m = c.wait_for(r"\[(\d+)s\] RESET: USB not configured 120s after bind", 3 * cyc / S + 25)
        rec.stop = True
        check(bool(b and m), "usbdiag-notcfg: not-configured reset after the code")
        if b and m:
            d = up_of(m) - up_of(b) - 120
            lo, hi = 3 * cyc, 3 * cyc + 1 + 0.05 * 3 * cyc + 25
            check(lo <= d <= hi, f"usbdiag-notcfg: reset {d}s after the 120 s wait, within {lo:.1f}..{hi:.1f}s "
                                 f"(cycle {cyc:.2f}s)")
        cycles = decode_recording(rec, S, t0)
        good = [x for x in cycles if x["check_ok"]]
        want_f = {**fields_of(f), "entries": (f["entries"] + [(0, 0, 0)] * 3)[:3]}
        check(len(cycles) == 3 and len(good) == 3 and all(fields_of(x) == want_f for x in good),
              f"usbdiag-notcfg: recorded LED writes decode to 3 cycles with F9 = 2 0 3 "
              f"({[(x['usb'], x['up'], x['errors'][:1]) for x in cycles]})")
        check(c.console().count("usbdiag:") == 1, "usbdiag-notcfg: exactly one usbdiag line")
        st = c.read("tmp/lvr-regdump/state").strip()
        cp = c.read("tmp/lvr-regdump/copy")
        ld = c.read("tmp/lvr-usbdiag")
        check(st == "ok" and cp == (c.r / led_code.REGDUMP).read_text()
              and re.fullmatch(r"t=\d+ path=2 " + re.escape(want) + r" regdump=ok\n", ld) is not None,
              f"usbdiag-notcfg: /tmp/lvr-regdump copy + state ok, /tmp/lvr-usbdiag line ({st}, {ld.strip()[:60]})")
        c.wait_for(r"held open and never written", 5)
        time.sleep(0.5)
        h = c.holders()
        check([p for p, fd in h] == [c.proc.pid], f"usbdiag-notcfg: held by the PID-1 stand-in ({h})")
        c.common_checks()
    finally:
        if rec:
            rec.stop = True
        c.stop()


def scenario_usbdiag_regdump_hung(bb, init):
    """The regdump never opens (a FIFO nobody writes; stands in for a hung runtime-PM
    resume): the player gives up after LC_RD_WAIT, F9.3 = 9, the reset stays bounded."""
    S = 20
    c = Case("usbdiag-hung", bb, init, cmdline="rdinit=/init lvr_deadman=900", scale=1)
    try:
        rgb_leds(c)
        make_tree(c.r, bound=ALL_BOUND[:9], deferred="a600000.usb\t\n", interrupts=irq_text([0] * 8),
                  regdump=FIFO)
        rw = int(re.search(r"^LC_RD_WAIT=(\d+)$", init.decode(), re.M).group(1))
        c.start([(2.0, 1), (1e9, S)], div=S, clock_start=1.5)   # see usbdiag-notcfg
        n = c.wait_for(r"\[(\d+)s\] no UDC after 60s", 15)
        line = c.wait_for(r"\[(\d+)s\] (ledcode v2 [^\n]*)\n", 10)
        f = led_code.collect(str(c.r), 1, 1, regdump="")
        tl, cs = led_code.encode_fields(f)
        cyc = led_code.cycle_seconds(tl)
        check(line is not None and line.group(2) == led_code.fields_line(f, cs) and " up=1 usb=099 " in line.group(2),
              f"usbdiag-hung: code line with F7 001 and F9 0 9 9 ({line and line.group(2)})")
        check(bool(n and line) and rw - 1 <= up_of(line) - up_of(n) <= rw + 15,
              f"usbdiag-hung: regdump read abandoned after LC_RD_WAIT ({n and line and up_of(line) - up_of(n)}s, "
              f"limit {rw}s)")
        ud = c.wait_for(r"\] usbdiag: t=\d+ (.*) regdump=(\w+)\n", 2)
        check(ud is not None and ud.group(2) == "timeout" and ud.group(1) == f["usbdiag"],
              f"usbdiag-hung: usbdiag line says regdump=timeout ({ud and ud.group(0).strip()})")
        check(c.read("tmp/lvr-regdump/state").strip() == "timeout" and c.read("tmp/lvr-regdump/copy") == "",
              "usbdiag-hung: state timeout, no regdump content")
        m = c.wait_for(r"\[(\d+)s\] RESET: no UDC after 60s", 3 * cyc / S + 25)
        check(bool(n and m) and 3 * cyc + rw - 1 <= up_of(m) - up_of(n) <= 3 * cyc * 1.05 + rw + 13,
              f"usbdiag-hung: reset {n and m and up_of(m) - up_of(n)}s after the no-UDC line "
              f"(3 cycles {3 * cyc:.1f}s + regdump limit {rw}s)")
        c.wait_for(r"held open and never written", 5)
        time.sleep(0.5)
        h = c.holders()
        check([p for p, fd in h] == [c.proc.pid] and h[0][1] == 3,
              f"usbdiag-hung: watchdog held by the PID-1 stand-in ({h})")
        c.common_checks()
    finally:
        c.stop()


def scenario_usbdiag_once(bb, init):
    """No qcom_wdt: the path-1 player and then the path-4 player both report F9, but the
    regdump is opened exactly once (a writer thread on a FIFO counts the opens)."""
    c = Case("usbdiag-once", bb, init, cmdline="rdinit=/init lvr_deadman=900", wdt=False, scale=1)
    opens = []
    stop = []

    def writer(path):
        while not stop:
            try:
                fd = os.open(path, os.O_WRONLY)
            except OSError:
                return
            if stop:
                os.close(fd)
                return
            opens.append(time.monotonic())
            os.write(fd, regdump_text(cnt=0x4).encode())
            os.close(fd)
            # Count readers, not writer opens: wait until this reader has closed the FIFO
            # (a non-blocking writer open fails with ENXIO once there is no reader).
            while not stop:
                try:
                    os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
                except OSError:
                    break
                time.sleep(0.01)
    try:
        rgb_leds(c)
        make_tree(c.r, **V2_TREE, interrupts=irq_text([3] * 8), regdump=FIFO)
        fifo = c.r / led_code.REGDUMP
        threading.Thread(target=writer, args=(fifo,), daemon=True).start()
        c.start([(1e9, 20)], div=20)
        check(c.wait_for(r"\] ledcode: 3 cycle\(s\) done", 40) is not None, "usbdiag-once: path-1 code played")
        p4 = c.wait_for(r"\] (ledcode v2 path=4 [^\n]*)\n", 10)
        lines = re.findall(r"\] usbdiag: t=\d+ (.*) regdump=(\w+)\n", c.console())
        check(p4 is not None and " usb=291 " in p4.group(1)
              and re.search(r"\] ledcode v2 path=1 [^\n]* usb=291 ", c.console()) is not None,
              f"usbdiag-once: both codes carry F9 2 9 1 ({p4 and p4.group(1)})")
        check(len(lines) == 2 and all(l == lines[0] for l in lines) and lines[0][1] == "ok",
              f"usbdiag-once: two usbdiag lines, the second reuses the first read ({lines})")
        check(len(opens) == 1, f"usbdiag-once: regdump opened exactly once per boot ({len(opens)})")
        c.common_checks()
    finally:
        stop.append(1)
        try:                            # release a writer blocked in open()
            fd = os.open(c.r / led_code.REGDUMP, os.O_RDONLY | os.O_NONBLOCK)
            os.close(fd)
        except OSError:
            pass
        c.stop()


def led_code_gap():
    return 20


def check_applets(members, bb):
    """Applet links in the archive: busybox-applets.txt minus build.py STRIPPED."""
    links = {n[4:]: d for n, (mode, d) in members.items()
             if n.startswith("bin/") and stat.S_ISLNK(mode)}
    check(all(d == b"busybox" for d in links.values()), "applets: every bin/ link points to busybox")
    t = WORK / "syntax" / "busybox"
    listed = subprocess.run([str(t), "--list"], capture_output=True, text=True, check=True).stdout.split()
    check((HERE / "busybox-applets.txt").read_text().split() == listed,
          "applets: busybox-applets.txt is the exact --list of the archived BusyBox")
    spec = importlib.util.spec_from_file_location("lvr_build", HERE / "build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    check(set(links) == set(listed) - build.STRIPPED, "applets: links = list minus STRIPPED")
    check(not [a for a in MUST_STRIP if a in links] and set(MUST_STRIP) <= build.STRIPPED,
          f"applets: no link for {' '.join(MUST_STRIP)}")
    check(all(a in links for a in MUST_LINK), "applets: everything the init runs is linked")
    code = [l for l in members["init"][1].decode().splitlines() if not l.lstrip().startswith("#")]
    check(not any("--install" in l for l in code), "applets: init never runs busybox --install")
    # The shell has no standalone applet lookup, so an unlinked name is "not found".
    # Probed with a harmless applet (seq), never with a stripped one.
    p = subprocess.run([str(t), "sh", "-c", "seq 1 1"], env={"PATH": "/nonexistent"},
                       capture_output=True, text=True, timeout=10)
    check(p.returncode == 127 and "not found" in p.stderr,
          f"applets: unlinked applets are not found by the shell (seq -> {p.returncode})")
    return sorted(links)


def check_extras(members, build):
    """sbin/kexec (static aarch64) and optional GPU firmware; nothing else new in the archive."""
    mode, kx = members.get("sbin/kexec", (0, b""))
    try:
        build.check_static_arm64("sbin/kexec", kx)
        static = True
    except SystemExit:
        static = False
    check(stat.S_ISREG(mode) and stat.S_IMODE(mode) == 0o755 and static,
          "extras: sbin/kexec is a static aarch64 ELF, mode 0755")
    links = {n[4:] for n, (m, _) in members.items() if n.startswith("bin/") and stat.S_ISLNK(m)}
    check(all(a in links for a in MUST_LINK_MODULES) and "modprobe" not in links
          and not any(n.endswith("/modprobe") for n in members),
          "extras: insmod and rmmod linked, no modprobe anywhere (nothing autoloads)")
    known = {dest for dest, _ in build.FIRMWARE}
    files = {n: d for n, (m, d) in members.items() if n.startswith("lib/") and stat.S_ISREG(m)}
    check(set(files) <= known, f"extras: files under lib/ are only Adreno firmware ({sorted(set(files) - known)})")
    check(all(stat.S_IMODE(members[n][0]) == 0o644 for n in files), "extras: firmware files are mode 0644")
    check(not [n for n in members if n.startswith("lib/")
               and re.search(r"syncboss|nrf|blu|swd|qca|wlan|bdwlan|amss|m3\.bin", n, re.I)],
          "extras: no MCU/BLU/QCA6390 firmware in the archive")
    sbin = sorted(n for n in members if n.startswith("sbin/"))
    check(sbin == ["sbin/kexec"], f"extras: sbin/ holds only kexec ({sbin})")


def scenario_refusals(bb):
    r = WORK / "refuse"
    shutil.rmtree(r, ignore_errors=True)
    r.mkdir(parents=True)
    bbp = r / "busybox"
    bbp.write_bytes(bb)
    bbp.chmod(0o755)
    init = WORK / "syntax" / "init"
    for env, what in [({}, "no LVR_TEST_ROOT"), ({"LVR_TEST_ROOT": "/"}, "LVR_TEST_ROOT=/"),
                      ({"LVR_TEST_ROOT": "//"}, "LVR_TEST_ROOT=//"),
                      ({"LVR_TEST_ROOT": str(r / "missing")}, "missing root"),
                      ({"LVR_TEST_ROOT": str(r)}, "root without marker"),
                      ({"LVR_TEST_ROOT": "relative"}, "relative root")]:
        p = subprocess.run([str(bbp), "sh", str(init)], env={"PATH": "/nonexistent", **env},
                           capture_output=True, text=True, timeout=10, cwd=r)
        check(p.returncode == 1 and "refusing" in p.stdout + p.stderr,
              f"refuse: {what} -> exit 1 ({p.returncode})")
    check(sorted(os.listdir(r)) == ["busybox"], "refuse: nothing created")


def load_build():
    spec = importlib.util.spec_from_file_location("lvr_build_mod", HERE / "build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check_reproducible(raw, members, build):
    """Reproducibility: rebuild twice from the archive's own busybox, kexec and firmware files
    (build.py's inputs, written out under their source names) and compare with the archive under test."""
    inp = WORK / "rebuild-inputs"
    inp.mkdir(exist_ok=True)
    (inp / "busybox").write_bytes(members["bin/busybox"][1])
    (inp / "kexec").write_bytes(members["sbin/kexec"][1])
    cmd = [sys.executable, "-I", str(HERE / "build.py"), "--busybox", str(inp / "busybox"),
           "--kexec", str(inp / "kexec")]
    fw = {dest: members[dest][1] for dest, _ in build.FIRMWARE if dest in members}
    if fw:
        (inp / "fw").mkdir(exist_ok=True)
        for dest, src in build.FIRMWARE:
            if dest in fw:
                (inp / "fw" / src).write_bytes(fw[dest])
        cmd += ["--firmware-dir", str(inp / "fw")]
    outs = []
    for i in range(2):
        o = WORK / f"rebuild-{i}.cpio.gz"
        subprocess.run(cmd + ["--output", str(o)], check=True, capture_output=True)
        outs.append(o.read_bytes())
    check(outs[0] == outs[1] == raw, "build.py reproducible and matches the tested archive")
    check(len(raw) <= 0x200000, f"archive {len(raw)} bytes <= 2 MiB")


def check_time_patch(init):
    """the scaled copy differs from the archive only in TIME_PATCH's sleep lines."""
    try:
        sc = scaled_init(init, DEFAULT_SCALE).decode().splitlines()
        ok = True
    except ValueError as e:
        check(False, f"time-scale: {e}")
        return
    orig = init.decode().splitlines()
    diff = [(a, b) for a, b in zip(orig, sc) if a != b]
    ok = len(orig) == len(sc) and len(diff) == sum(n for _, _, n in TIME_PATCH) and all(
        re.fullmatch(r"\t+sleep (\S+)", a) and re.fullmatch(r"\t+sleep [0-9.]+", b) for a, b in diff)
    check(ok, f"time-scale: the scaled init differs only in {len(diff)} loop-sleep lines "
              f"(scale {DEFAULT_SCALE}; this run: {SCALE})")


# name -> callable(bb, init, raw); the order of the report. Longest first when parallel (EST).
SCENARIOS = [
    ("reproducible", lambda bb, init, raw: check_reproducible(raw, cpio_members(gzip.decompress(raw)), load_build())),
    ("time_patch", lambda bb, init, raw: check_time_patch(init)),
    ("no_udc", lambda bb, init, raw: scenario_no_udc(bb, init)),
    ("success_heartbeat", lambda bb, init, raw: scenario_success_heartbeat(bb, init)),
    ("heartbeat_validate", lambda bb, init, raw: scenario_heartbeat_validate(bb, init, load_dev())),
    ("deadman_cmdline", lambda bb, init, raw: scenario_deadman_cmdline(bb, init)),
    ("static_return_poll", lambda bb, init, raw: static_return_poll(init)),
    ("return_request", lambda bb, init, raw: scenario_return_request(bb, init, load_dev())),
    ("return_nowdt", lambda bb, init, raw: scenario_return_nowdt(bb, init, load_dev())),
    ("return_armed", lambda bb, init, raw: scenario_return_armed(bb, init, load_dev())),
    ("return_warm", lambda bb, init, raw: scenario_return_warm(bb, init, load_dev())),
    ("return_refused", lambda bb, init, raw: scenario_return_refused(bb, init, load_dev())),
    ("return_garbled", lambda bb, init, raw: scenario_return_garbled(bb, init, load_dev())),
    ("return_missing", lambda bb, init, raw: scenario_return_missing(bb, init, load_dev())),
    ("not_configured", lambda bb, init, raw: scenario_not_configured(bb, init)),
    ("no_watchdog", lambda bb, init, raw: scenario_no_watchdog(bb, init)),
    ("open_fails", lambda bb, init, raw: scenario_open_fails(bb, init)),
    ("leds_rgb", lambda bb, init, raw: scenario_leds_rgb(bb, init)),
    ("leds_multicolor", lambda bb, init, raw: scenario_leds_multicolor(bb, init)),
    ("leds_readonly", lambda bb, init, raw: scenario_leds_readonly(bb, init)),
    ("leds_stuck_and_empty", lambda bb, init, raw: scenario_leds_stuck_and_empty(bb, init)),
    ("ledcode_unit", lambda bb, init, raw: scenario_ledcode_unit(bb, init)),
    ("ledcode_noudc", lambda bb, init, raw: scenario_ledcode_noudc(bb, init)),
    ("ledcode_notcfg", lambda bb, init, raw: scenario_ledcode_notcfg(bb, init)),
    ("ledcode_deadline", lambda bb, init, raw: scenario_ledcode_deadline(bb, init)),
    ("ledcode_hung_led", lambda bb, init, raw: scenario_ledcode_hung_led(bb, init)),
    ("ledcode_nowdt", lambda bb, init, raw: scenario_ledcode_nowdt(bb, init)),
    ("usbdiag_notcfg", lambda bb, init, raw: scenario_usbdiag_notcfg(bb, init)),
    ("usbdiag_regdump_hung", lambda bb, init, raw: scenario_usbdiag_regdump_hung(bb, init)),
    ("usbdiag_once", lambda bb, init, raw: scenario_usbdiag_once(bb, init)),
    ("refusals", lambda bb, init, raw: scenario_refusals(bb)),
]
# Seconds per scenario group in a parallel run (scale 4), only to start the long ones first.
EST = {"ledcode_noudc": 43, "ledcode_nowdt": 38, "return_request": 28, "ledcode_notcfg": 22,
       "usbdiag_notcfg": 50, "usbdiag_regdump_hung": 20, "success_heartbeat": 20, "usbdiag_once": 17,
       "return_nowdt": 17, "ledcode_deadline": 16, "ledcode_hung_led": 15, "heartbeat_validate": 10,
       "leds_rgb": 9, "leds_stuck_and_empty": 8, "return_armed": 7, "deadman_cmdline": 7}


def _run_in_child(name, fn, args, outdir):
    """Forked worker body: run one scenario, its lines to <outdir>/<name>.out, FAILS to .json."""
    out = open(outdir / f"{name}.out", "w", buffering=1)
    sys.stdout = sys.stderr = out
    FAILS.clear()
    try:
        fn(*args)
    except BaseException:
        check(False, f"{name}: exception\n" + traceback.format_exc())
    (outdir / f"{name}.json").write_text(json.dumps(FAILS))
    out.flush()


def run_scenarios(names, args, jobs):
    """Each scenario in its own forked process (no threads in this parent, so fork is safe),
    at most `jobs` at a time. Output is printed per scenario in SCENARIOS order."""
    table = dict(SCENARIOS)
    outdir = WORK / "_scenario-out"
    outdir.mkdir(exist_ok=True)
    pending = sorted(names, key=lambda n: -EST.get(n, 5))
    running, secs = {}, {}
    while pending or running:
        while pending and len(running) < jobs:
            name = pending.pop(0)
            sys.stdout.flush()
            sys.stderr.flush()
            pid = os.fork()
            if pid == 0:
                try:
                    _run_in_child(name, table[name], args, outdir)
                finally:
                    os._exit(0)
            running[pid] = (name, time.monotonic())
        pid, status = os.wait()
        if pid in running:
            name, t0 = running.pop(pid)
            secs[name] = time.monotonic() - t0
    for name in [n for n, _ in SCENARIOS if n in names]:
        text = (outdir / f"{name}.out").read_text() if (outdir / f"{name}.out").exists() else ""
        print(f"-- {name} ({secs.get(name, 0):.1f}s)")
        print(text, end="" if text.endswith("\n") or not text else "\n")
        try:
            FAILS.extend(json.loads((outdir / f"{name}.json").read_text()))
        except (OSError, ValueError):
            check(False, f"{name}: worker died without a result")


def main():
    global WORK, SCALE
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--initramfs", type=Path, default=DEFAULT_INITRAMFS)
    ap.add_argument("--scale", type=int, default=None,
                    help=f"time scale of the scaled scenarios (1..20; default env LVR_TEST_TIMESCALE or {DEFAULT_SCALE})")
    ap.add_argument("--jobs", "-j", type=int, default=None,
                    help="scenarios at once (default min(12, 3/4 of the CPUs); 1 = in-process, serial)")
    ap.add_argument("--full", action="store_true",
                    help="every scenario real-time (scale 1) and serial unless --jobs")
    ap.add_argument("--only", default="", help="comma-separated scenario names (default: all)")
    ap.add_argument("--keep", action="store_true", help="keep the work dir after a PASS")
    a = ap.parse_args()
    if os.geteuid() == 0:
        raise SystemExit("refusing to run as root")
    env_scale = os.environ.get("LVR_TEST_TIMESCALE", "")
    SCALE = 1 if a.full else a.scale if a.scale is not None else int(env_scale) if env_scale.isdigit() else DEFAULT_SCALE
    if not 1 <= SCALE <= 20:
        raise SystemExit("--scale must be 1..20")
    # Scenarios mostly sleep (the whole run averages well under one core), so up to 12 at once.
    jobs = a.jobs if a.jobs is not None else 1 if a.full else max(1, min(12, (os.cpu_count() or 2) * 3 // 4))
    names = [n for n, _ in SCENARIOS]
    if a.only:
        names = [n.strip() for n in a.only.split(",") if n.strip()]
        bad = [n for n in names if n not in dict(SCENARIOS)]
        if bad:
            raise SystemExit(f"unknown scenario(s) {bad}; known: {' '.join(n for n, _ in SCENARIOS)}")
    raw = a.initramfs.read_bytes()
    members = cpio_members(gzip.decompress(raw))
    bb, init = members["bin/busybox"][1], members["init"][1]
    base = work_base()
    base.mkdir(parents=True, exist_ok=True)
    WORK = Path(tempfile.mkdtemp(prefix="init-test-", dir=base))
    t_start = time.monotonic()
    print(f"work dir {WORK}; scale {SCALE}; jobs {jobs}")

    # Syntax check with the target shell.
    t = WORK / "syntax"
    t.mkdir(exist_ok=True)
    (t / "busybox").write_bytes(bb)
    (t / "busybox").chmod(0o755)
    (t / "init").write_bytes(init)
    p = subprocess.run([str(t / "busybox"), "sh", "-n", str(t / "init")], capture_output=True, text=True)
    check(p.returncode == 0, f"busybox sh -n init ({p.stderr.strip()})")

    LINKS[:] = check_applets(members, bb)
    check_extras(members, load_build())

    args = (bb, init, raw)
    if jobs == 1:
        for name in names:
            t0 = time.monotonic()
            print(f"-- {name}")
            try:
                dict(SCENARIOS)[name](*args)
            except Exception:
                check(False, f"{name}: exception\n" + traceback.format_exc())
            print(f"   ({time.monotonic() - t0:.1f}s)")
    else:
        run_scenarios(names, args, jobs)
    print(f"\ntest_init: {len(names)} scenario group(s), scale {SCALE}, jobs {jobs}, "
          f"{time.monotonic() - t_start:.0f}s")
    print(f"\n{len(FAILS)} failure(s)")
    for f in FAILS:
        print("  " + f.splitlines()[0])
    if not FAILS and not a.keep:
        shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
