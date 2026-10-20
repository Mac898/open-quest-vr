#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""LED diagnostic code: reference encoder, sysfs collector and video decoder.

When the mainline init cannot bring up USB it blinks the front LED in a code
(boot path, device/driver flags, deferred-probe reasons, uptime, USB-controller state,
checksum). This module is the reference implementation: the init's shell encoder
(rootfs/mainline-init/init.sh) must produce exactly the timeline `encode()` gives here;
rootfs/mainline-init/test_init.py checks that on fixture sysfs trees. The decoder turns
a phone video of the LED back into the code fields.

Host-only. Nothing here talks to the headset.

  led_code.py encode --path 2 --flags GGGGGGGGGGRG --ndef 0 --up 1 --usb 000
  led_code.py collect ROOT --path 1 --up 1       # fields from a sysfs tree copy/fixture
  led_code.py decode EVENTS.txt                  # lines "SECONDS COLOUR" (colour from then on)
  led_code.py frames FRAMES.csv                  # lines "SECONDS,R,G,B" (mean ROI colour per frame)
  led_code.py video CLIP.mp4 --roi X,Y,W,H       # ffmpeg -> per-frame ROI mean -> decode
  led_code.py worst                              # longest possible cycle
"""
import argparse
import os
import re
import subprocess
import sys

# --- timings (ms) -------------------------------------------------------------
SHORT_ON, SHORT_OFF, ZERO_ON, DIGIT_GAP = 250, 250, 1000, 750
FLAG_ON, FLAG_OFF = 400, 400
SEP_ON, SEP_OFF = 250, 1000
START_ON, START_OFF, END_OFF = 2000, 1500, 3000
COLOURS = ("white", "red", "green", "blue", "dark")
FLAG_COLOUR = {"G": "green", "B": "blue", "R": "red"}
FLAG_VALUE = {"R": 0, "G": 1, "B": 2}

# --- flag devices (platform device names, in order) and index table ---------
FLAG_DEVS = [
    ("80860000.memory", "cmd-db"),
    ("18200000.rsc", "rpmh-rsc"),
    ("18200000.rsc:clock-controller", "RPMh clocks"),
    ("18200000.rsc:regulators-0", "RPMh regulators PM8150"),
    ("18200000.rsc:bcm-voter", "BCM voter"),
    ("100000.clock-controller", "GCC"),
    ("16e0000.interconnect", "aggre1 NoC"),
    ("88e3000.phy", "HS PHY"),
    ("a6f8800.usb", "dwc3-qcom glue"),
    ("a600000.usb", "dwc3 core"),
    ("88e8000.phy", "QMP combo PHY"),
]
FLAG_NAMES = [d for _, d in FLAG_DEVS] + ["UDC"]
TABLE = [
    (1, "80860000.memory", "/reserved-memory/memory@80860000"),
    (2, "18200000.rsc", "/soc@0/rsc@18200000"),
    (3, "18200000.rsc:clock-controller", "/soc@0/rsc@18200000/clock-controller"),
    (4, "18200000.rsc:regulators-0", "/soc@0/rsc@18200000/regulators-0"),
    (5, "18200000.rsc:regulators-1", "/soc@0/rsc@18200000/regulators-1"),
    (6, "18200000.rsc:bcm-voter", "/soc@0/rsc@18200000/bcm-voter"),
    (7, "100000.clock-controller", "/soc@0/clock-controller@100000"),
    (8, "1500000.interconnect", "/soc@0/interconnect@1500000"),
    (9, "1620000.interconnect", "/soc@0/interconnect@1620000"),
    (10, "163d000.interconnect", "/soc@0/interconnect@163d000"),
    (11, "16e0000.interconnect", "/soc@0/interconnect@16e0000"),
    (12, "90c0000.interconnect", "/soc@0/interconnect@90c0000"),
    (13, "9100000.interconnect", "/soc@0/interconnect@9100000"),
    (14, "88e3000.phy", "/soc@0/phy@88e3000"),
    (15, "a6f8800.usb", "/soc@0/usb@a6f8800"),
    (16, "a600000.usb", "/soc@0/usb@a6f8800/usb@a600000"),
    (17, "88e8000.phy", "/soc@0/phy@88e8000"),
    (18, "fan", "/fan"),
    (19, "f100000.pinctrl", "/soc@0/pinctrl@f100000"),
    (20, "b220000.interrupt-controller", "/soc@0/interrupt-controller@b220000"),
    (21, "c440000.spmi", "/soc@0/spmi@c440000"),
    (22, "c440000.spmi:pmic@4:gpio@c000", "/soc@0/spmi@c440000/pmic@4/gpio@c000"),
    (23, "c440000.spmi:pmic@5:pwm", "/soc@0/spmi@c440000/pmic@5/pwm"),
    (24, "c263000.thermal-sensor", "/soc@0/thermal-sensor@c263000"),
    (25, "c265000.thermal-sensor", "/soc@0/thermal-sensor@c265000"),
    (26, "17c10000.watchdog", "/soc@0/watchdog@17c10000"),
    (27, "9ba40000.ramoops", "/reserved-memory/ramoops@9ba40000"),
    (28, "psci", "/psci"),
]
CATEGORIES = {
    0: "empty reason (-EPROBE_DEFER without dev_err_probe)",
    1: "fw_devlink: wait for supplier <DT path>",
    2: "device link: supplier <name> not ready",
    3: "regulator / supply",
    4: "clk / clock",
    5: "phy / PHY",
    6: "interconnect / icc / path / bandwidth",
    7: "power domain",
    8: "Command DB / cmd / rpmh",
    9: "other dev_err_probe text",
}
PATHS = {1: "no UDC after UDC_WAIT", 2: "UDC bound, host never configured it",
         3: "(reserved: dead-man; never played)", 4: "reset wanted but no qcom_wdt",
         5: "other"}
DEBUGFS_DEFERRED = "sys/kernel/debug/devices_deferred"
GADGET_UDC = "config/usb_gadget/qkx/UDC"
# --- F9 sources (USB controller state) ----------------------------
PROC_INTERRUPTS = "proc/interrupts"
UDC_DIR = "sys/class/udc/a600000.usb"
# The only dwc3 debugfs file the init reads (regdump only reads core registers; lsp_dump,
# GDBGEPINFO and the fifo/queue files write debug-mux registers on read).
REGDUMP = "sys/kernel/debug/usb/a600000.usb/regdump"
IRQ_CAP = 99999999            # per-CPU count and running sum cap (shell arithmetic stays small)
UDC_STATE_DIGIT = {"not attached": 0, "attached": 0, "powered": 0, "default": 1,
                   "addressed": 2, "configured": 3, "suspended": 4}
# usbdiag line: (label, regdump name(s)); two names = "hi:lo".
REGDUMP_KEYS = ["GSTS", "GBUSERRADDR0", "GBUSERRADDR1", "GEVNTADRLO(0)", "GEVNTADRHI(0)",
                "GEVNTSIZ(0)", "GEVNTCOUNT(0)", "DCTL", "DEVTEN", "DSTS"]
USBDIAG_REGS = [("GSTS", ("GSTS",)), ("BUSERR", ("GBUSERRADDR1", "GBUSERRADDR0")),
                ("EVADR", ("GEVNTADRHI(0)", "GEVNTADRLO(0)")), ("EVSIZ", ("GEVNTSIZ(0)",)),
                ("EVCNT", ("GEVNTCOUNT(0)",)), ("DCTL", ("DCTL",)), ("DEVTEN", ("DEVTEN",)),
                ("DSTS", ("DSTS",))]
F9_IRQ = {0: "0 IRQs", 1: "1-9 IRQs", 2: "10-999 IRQs", 3: ">= 1000 IRQs",
          9: "no dwc3 line in /proc/interrupts (or unreadable)"}
F9_STATE = {0: "not attached/attached/powered", 1: "default (bus reset seen)", 2: "addressed",
            3: "configured", 4: "suspended", 9: "UDC state unreadable or other"}
F9_REGS = {0: "GEVNTCOUNT(0) 0, no bus error", 1: "GEVNTCOUNT(0) != 0 (events pending), no bus error",
           2: "AXI bus error (GSTS bit 4), GEVNTCOUNT(0) 0", 3: "AXI bus error and events pending",
           9: "regdump unreadable (missing, timed out, or no GSTS/GEVNTCOUNT(0))"}


def lookup_name(name):
    for idx, n, _ in TABLE:
        if n == name:
            return idx
    return 99


def lookup_path(path):
    best, idx = 0, 99
    for i, _, p in TABLE:
        if (path == p or path.startswith(p + "/")) and len(p) > best:
            best, idx = len(p), i
    return idx


def strip_drv_prefix(text):
    """device_set_deferred_probe_reason() stores "<driver or bus name>: <message>"
    (drivers/base/dd.c:235), so the category rules apply to the part after the first ": "
    when the part before it has no space."""
    i = text.find(": ")
    if i >= 0 and " " not in text[:i]:
        return text[i + 2:]
    return text


def reason_category(reason):
    """(C, SS) for the reason text after the TAB (first match wins)."""
    t = strip_drv_prefix(reason)
    if t == "":
        return 0, 0
    if t.startswith("wait for supplier "):
        return 1, lookup_path(t[len("wait for supplier "):])
    if t.startswith("supplier ") and t.endswith(" not ready") and len(t) >= 19:
        return 2, lookup_name(t[len("supplier "):-len(" not ready")])
    for c, words in ((3, ("regulator", "supply")), (4, ("clk", "clock")), (5, ("phy", "PHY")),
                     (6, ("interconnect", "icc", "path", "bandwidth")), (7, ("domain",)),
                     (8, ("Command DB", "cmd", "rpmh"))):
        if any(w in t for w in words):
            return c, 0
    return 9, 0


def _read_text(path):
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except OSError:
        return None


def _lines(data):
    """Lines as the shell's `while IFS= read -r l || [ -n "$l" ]` sees them."""
    lines = data.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _first_line(path):
    """First line with surrounding blanks/tabs dropped (`read -r v < file`); None if
    unreadable."""
    data = _read_text(path)
    if data is None:
        return None
    return data.split("\n", 1)[0].strip(" \t")


def irq_count(text):
    """S1: per-CPU counts of the first /proc/interrupts line whose last token is "dwc3":
    the sum of the numeric tokens right after the IRQ label (each and the sum capped at
    IRQ_CAP). None if there is no such line."""
    if text is None:
        return None
    for line in _lines(text):
        t = [x for x in re.split(r"[ \t]+", line) if x]      # the shell's default IFS
        if len(t) < 2 or t[-1] != "dwc3":
            continue
        s = 0
        for x in t[1:]:
            if not re.fullmatch(r"[0-9]+", x):
                break
            s = min(s + min(int(x), IRQ_CAP), IRQ_CAP)
        return s
    return None


def irq_digit(n):
    if n is None:
        return 9
    return 0 if n == 0 else 1 if n < 10 else 2 if n < 1000 else 3


def _word(s):
    """A sysfs string as one log token: na (unreadable/empty), itself if [A-Za-z-]+,
    'not attached' -> not_attached, otherwise 'other'."""
    if not s:
        return "na"
    if s == "not attached":
        return "not_attached"
    return s if re.fullmatch(r"[A-Za-z-]+", s) else "other"


def _hex(v):
    return v if re.fullmatch(r"0x[0-9a-fA-F]{1,8}", v) else None


def parse_regdump(text):
    """S3: {name: '0x...'} for REGDUMP_KEYS, first valid 'NAME = 0x%08x' line per name."""
    regs = {}
    if text is None:
        return regs
    for line in _lines(text):
        if " = " not in line:
            continue
        k, _, v = line.partition(" = ")
        if k in REGDUMP_KEYS and k not in regs and _hex(v):
            regs[k] = v
    return regs


def regs_digit(regs):
    if "GSTS" not in regs or "GEVNTCOUNT(0)" not in regs:
        return 9
    cnt = int(regs["GEVNTCOUNT(0)"], 16) & 0xfffc != 0
    err = (int(regs["GSTS"], 16) >> 4) & 1
    return int(cnt) + 2 * err


def usb_fields(root, regdump=None):
    """F9 digits and the usbdiag summary (without t= and regdump=), as init.sh lc_usb.
    regdump: file to parse (default ROOT/REGDUMP; '' = none)."""
    irq = irq_count(_read_text(os.path.join(root, PROC_INTERRUPTS)))
    st = _first_line(os.path.join(root, UDC_DIR, "state"))
    sp = _first_line(os.path.join(root, UDC_DIR, "current_speed"))
    if regdump is None:
        regdump = os.path.join(root, REGDUMP)
    regs = parse_regdump(_read_text(regdump) if regdump else None)
    usb = (irq_digit(irq), UDC_STATE_DIGIT.get(st, 9) if st is not None else 9, regs_digit(regs))
    parts = [f"irq={'na' if irq is None else irq}", f"state={_word(st)}", f"speed={_word(sp)}"]
    for label, names in USBDIAG_REGS:
        parts.append(f"{label}=" + ":".join(regs.get(n, "na") for n in names))
    parts.append("f9=%d%d%d" % usb)
    return usb, " ".join(parts)


def collect(root, path, up, regdump=None):
    """Fields from a sysfs tree under `root` (same rules as init.sh lc_collect).
    Returns dict(path, flags, ndef, entries, up, usb, drivers, usbdiag)."""
    p = os.path.join(root, "sys/bus/platform/devices")
    flags, drivers = "", []
    for name, _ in FLAG_DEVS:
        d = os.path.join(p, name)
        if os.path.exists(os.path.join(d, "driver")):
            flags += "G"
            drivers.append(f"{name}={os.path.basename(os.readlink(os.path.join(d, 'driver')))}"
                           if os.path.islink(os.path.join(d, "driver")) else f"{name}=?")
        elif os.path.exists(d):
            flags += "B"
            drivers.append(f"{name}=-")
        else:
            flags += "R"
    try:
        with open(os.path.join(root, GADGET_UDC)) as f:
            bound = f.readline().rstrip("\n") != ""
    except OSError:
        bound = False
    udc = os.path.join(root, "sys/class/udc")
    present = os.path.isdir(udc) and any(not e.name.startswith(".") and os.path.exists(e.path)
                                          for e in os.scandir(udc))
    flags += "G" if bound else ("B" if present else "R")
    entries = []
    try:
        with open(os.path.join(root, DEBUGFS_DEFERRED), "rb") as f:
            data = f.read().decode("utf-8", "replace")
    except OSError:
        data = None
    if data is None:
        ndef = 99
    else:
        lines = data.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        ndef = min(len(lines), 98)
        for line in lines[:3]:
            name, _, reason = line.partition("\t")
            c, ss = reason_category(reason)
            entries.append((lookup_name(name), c, ss))
    up = min(int(up), 999)
    usb, usbdiag = usb_fields(root, regdump)
    return {"path": int(path), "flags": flags, "ndef": ndef, "entries": entries, "up": up,
            "usb": usb, "drivers": drivers, "usbdiag": usbdiag}


def _digits(colour, n, width):
    out = []
    for ch in str(n).zfill(width)[-width:]:
        d = int(ch)
        if d == 0:
            out.append((colour, ZERO_ON, SHORT_OFF))
        else:
            out += [(colour, SHORT_ON, SHORT_OFF)] * d
        out.append(("dark", 0, DIGIT_GAP))
    return out


def _sep():
    return [("white", SEP_ON, SEP_OFF)]


def checksum(path, flags, ndef, entries, up, usb):
    """F8 = (F1 + flag values + every digit of F3-F7 and F9) mod 10."""
    ent = (list(entries) + [(0, 0, 0)] * 3)[:3]
    s = path + sum(FLAG_VALUE[f] for f in flags)
    s += sum(int(c) for c in "%02d" % ndef)
    s += sum(int(c) for e in ent for c in "%02d%d%02d" % e)
    s += sum(int(c) for c in "%03d" % min(up, 999))
    s += sum(usb)
    return s % 10


def encode(path, flags, ndef, entries, up, usb):
    """Timeline [(colour, on_ms, off_ms)] of one cycle and its checksum (F8).
    v1 plus F9 (3 blue digits) before F8."""
    assert 0 <= path <= 9 and len(flags) == 12 and set(flags) <= set("GBR")
    assert 0 <= ndef <= 99 and len(entries) <= 3
    assert len(usb) == 3 and all(0 <= d <= 9 for d in usb)
    ent = (list(entries) + [(0, 0, 0)] * 3)[:3]
    up = min(up, 999)
    t = [("white", START_ON, START_OFF)]
    t += _digits("red", path, 1) + _sep()
    t += [(FLAG_COLOUR[f], FLAG_ON, FLAG_OFF) for f in flags] + _sep()
    t += _digits("blue", ndef, 2) + _sep()
    for dev, cat, sup in ent:
        t += _digits("green", dev, 2) + _digits("red", cat, 1) + _digits("blue", sup, 2) + _sep()
    t += _digits("green", up, 3) + _sep()
    t += _digits("blue", "%d%d%d" % tuple(usb), 3) + _sep()
    cs = checksum(path, flags, ndef, ent, up, usb)
    t += _digits("red", cs, 1) + [("dark", 0, END_OFF)]
    return t, cs


def encode_fields(f):
    return encode(f["path"], f["flags"], f["ndef"], f["entries"], f["up"], f["usb"])


def timeline_str(t):
    """The exact string the init's lc_encode builds (LC_TL)."""
    return " ".join(f"{c}:{a}:{b}" for c, a, b in t)


def cycle_seconds(t):
    return sum(a + b for _, a, b in t) / 1000


def fields_line(f, cs=None):
    """The kmsg line the init logs (after 'lvr-init: [Ns] ')."""
    ent = (list(f["entries"]) + [(0, 0, 0)] * 3)[:3]
    if cs is None:
        cs = checksum(f["path"], f["flags"], f["ndef"], ent, f["up"], f["usb"])
    d = ",".join("%02d.%d.%02d" % e for e in ent)
    usb = "%d%d%d" % tuple(f["usb"])
    return (f"ledcode v2 path={f['path']} flags={f['flags']} ndef={f['ndef']} d={d} up={f['up']} "
            f"usb={usb} sum={cs}")


def worst_cycle():
    """Upper bound for any cycle: every digit 9 (F8 included; flag timing is fixed)."""
    t, cs = encode(9, "B" * 12, 99, [(99, 9, 99)] * 3, 999, (9, 9, 9))
    return cycle_seconds(t) + (9 - cs) * (SHORT_ON + SHORT_OFF) / 1000


# --- decoder --------------------------------------------------------------------------
START_MIN = 1200      # white longer than this = START, shorter = SEP
LONG_MIN = 600        # a flash longer than this is a zero; a dark gap longer ends a digit


def runs_from_events(events, end=None, min_ms=100):
    """events: [(t_seconds, colour)], colour valid from t until the next event.
    Returns [(colour, start_ms, dur_ms)] with glitches (< min_ms, or not a known colour)
    merged into the previous run."""
    ev = sorted((float(t) * 1000, c) for t, c in events)
    if not ev:
        return []
    end_ms = float(end) * 1000 if end is not None else ev[-1][0]
    raw = []
    for i, (t, c) in enumerate(ev):
        nxt = ev[i + 1][0] if i + 1 < len(ev) else end_ms
        if nxt > t:
            raw.append([c if c in COLOURS else "?", t, nxt - t])
    out = []
    for c, t, d in raw:
        if out and (c == out[-1][0] or c == "?" or d < min_ms):
            out[-1][2] += d
        else:
            out.append([c, t, d])
    merged = []                   # re-merge neighbours that became equal
    for r in out:
        if merged and merged[-1][0] == r[0]:
            merged[-1][2] += r[2]
        else:
            merged.append(r)
    return [tuple(r) for r in merged]


def _groups(tokens):
    """Split a field's flashes into digits: a new digit at a colour change or a dark gap
    >= LONG_MIN. Returns [(colour, value or None)]."""
    groups = []
    for colour, start, dur, gap in tokens:
        if not groups or colour != groups[-1][0] or gap >= LONG_MIN:
            groups.append([colour, []])
        groups[-1][1].append(dur)
    out = []
    for colour, durs in groups:
        if len(durs) == 1 and durs[0] >= LONG_MIN:
            out.append((colour, 0))
        elif all(d < LONG_MIN for d in durs) and len(durs) <= 9:
            out.append((colour, len(durs)))
        else:
            out.append((colour, None))
    return out


def _number(groups, colour, width, errs, what):
    if len(groups) < width or any(c != colour or v is None for c, v in groups[:width]):
        errs.append(f"{what}: expected {width} {colour} digit(s), got {groups[:width]}")
        return None, groups[width:]
    return int("".join(str(v) for _, v in groups[:width])), groups[width:]


def decode(events, end=None, min_ms=100):
    """Decode every cycle that starts with a START marker. Returns a list of dicts
    (t_start, path, flags, ndef, entries, up, usb, check, check_ok, errors)."""
    runs = runs_from_events(events, end, min_ms)
    lit, gap = [], 0.0
    for c, t, d in runs:
        if c == "dark":
            gap += d
        else:
            lit.append((c, t, d, gap))
            gap = 0.0
    starts = [i for i, (c, _, d, _) in enumerate(lit) if c == "white" and d >= START_MIN]
    cycles = []
    for k, si in enumerate(starts):
        stop = starts[k + 1] if k + 1 < len(starts) else len(lit)
        fields, cur = [], []
        for tok in lit[si + 1:stop]:
            if tok[0] == "white":
                fields.append(cur)
                cur = []
            else:
                cur.append(tok)
        fields.append(cur)
        res = {"t_start": lit[si][1] / 1000, "errors": []}
        errs = res["errors"]
        if len(fields) != 9:
            errs.append(f"incomplete cycle: {len(fields)} field(s), want 9")
            fields += [[]] * (9 - len(fields))
        g = _groups(fields[0])
        res["path"], _ = _number(g, "red", 1, errs, "F1 path")
        fl = "".join({"green": "G", "blue": "B", "red": "R"}.get(c, "?") for c, *_ in fields[1])
        if len(fl) != 12 or "?" in fl or any(d >= LONG_MIN for _, _, d, _ in fields[1]):
            errs.append(f"F2 flags: got {fl!r}")
        res["flags"] = fl
        res["ndef"], _ = _number(_groups(fields[2]), "blue", 2, errs, "F3 ndef")
        ents = []
        for j in range(3):
            g = _groups(fields[3 + j])
            dd, g = _number(g, "green", 2, errs, f"F{4 + j} device")
            c, g = _number(g, "red", 1, errs, f"F{4 + j} reason")
            ss, g = _number(g, "blue", 2, errs, f"F{4 + j} supplier")
            ents.append((dd, c, ss))
        res["entries"] = ents
        res["up"], _ = _number(_groups(fields[6]), "green", 3, errs, "F7 up")
        u, _ = _number(_groups(fields[7]), "blue", 3, errs, "F9 usb")
        res["usb"] = tuple(int(c) for c in "%03d" % u) if u is not None else None
        res["check"], _ = _number(_groups(fields[8]), "red", 1, errs, "F8 check")
        ok = not errs and None not in (res["path"], res["ndef"], res["up"], res["usb"], res["check"])
        res["check_ok"] = bool(ok and checksum(res["path"], fl, res["ndef"], ents, res["up"], res["usb"])
                               == res["check"])
        cycles.append(res)
    return cycles


def describe(res):
    """Human-readable explanation of one decoded cycle."""
    names = {i: n for i, n, _ in TABLE}
    lines = [f"cycle at {res['t_start']:.2f} s: " +
             ("checksum OK" if res.get("check_ok") else "CHECKSUM/DECODE PROBLEM")]
    lines += ["  error: " + e for e in res.get("errors", [])]
    if res.get("path") is not None:
        lines.append(f"  F1 path {res['path']}: {PATHS.get(res['path'], '?')}")
    word = {"G": "bound", "B": "exists, no driver", "R": "absent"}
    for i, f in enumerate(res.get("flags", "")):
        if i < len(FLAG_NAMES):
            lines.append(f"  F2 flag {i + 1:2d} {FLAG_NAMES[i]:24s} {f} = {word.get(f, '?')}")
    nd = res.get("ndef")
    lines.append(f"  F3 deferred devices: {nd}" + (" (devices_deferred unreadable)" if nd == 99 else ""))
    for j, (dd, c, ss) in enumerate(res.get("entries", [])):
        if dd == 0 and c == 0 and ss == 0:
            lines.append(f"  F{4 + j} (no entry)")
            continue
        lines.append(f"  F{4 + j} {dd if dd is not None else '?'}={names.get(dd, 'unknown')} "
                     f"reason {c}={CATEGORIES.get(c, '?')} supplier "
                     f"{ss if ss is not None else '?'}={names.get(ss, 'none' if ss == 0 else 'unknown')}")
    lines.append(f"  F7 uptime at init start: {res.get('up')} s")
    u = res.get("usb")
    if u is not None:
        lines.append(f"  F9.1 dwc3 IRQ count: {u[0]} = {F9_IRQ.get(u[0], '?')}")
        lines.append(f"  F9.2 UDC state: {u[1]} = {F9_STATE.get(u[1], '?')}")
        lines.append(f"  F9.3 dwc3 regs: {u[2]} = {F9_REGS.get(u[2], '?')}")
        lines.append(f"  F9 reading: {usb_reading(u)}")
    return "\n".join(lines)


def usb_reading(u):
    """Plain-language reading of F9 = (irq, state, regs)."""
    a, b, c = u
    if c in (2, 3):
        return "AXI bus error on dwc3 DMA: H1, abort mode (GBUSERRADDR in the usbdiag line)"
    if (a, b, c) == (0, 0, 0):
        return "no event in memory, no IRQ: event-buffer DMA stalled in the SMMU (H1, stall mode)"
    if (a, b, c) == (0, 0, 1):
        return "events counted, no IRQ: IRQ not delivered (H2; GEVNTSIZ(0) bit 31, GIC line)"
    if a in (1, 2, 3) and b == 0:
        return "IRQs arrive but no reset event understood: events land elsewhere (H1, redirect)"
    if a in (2, 3) and b == 3 and c == 0:
        return "configured: the fix worked (expect the link)"
    if a in (1, 2, 3) and b in (1, 2) and c == 0:
        return "events work; ep0 data/TRB path or PHY (H3)"
    if 9 in u:
        return "no known reading (a source was unreadable: digit 9)"
    return "no known reading"


def classify_frames(rows, thr=None):
    """rows: [(t, r, g, b)] mean ROI colour per frame. Channel on if >= its threshold
    (default: midpoint of that channel's min and max over the clip)."""
    if thr is None:
        thr = []
        for k in (1, 2, 3):
            vals = [r[k] for r in rows]
            lo, hi = min(vals), max(vals)
            thr.append((lo + hi) / 2 if hi - lo > 20 else 1e9)
    names = {(0, 0, 0): "dark", (1, 0, 0): "red", (0, 1, 0): "green", (0, 0, 1): "blue",
             (1, 1, 1): "white"}
    ev, last = [], None
    for t, r, g, b in rows:
        c = names.get((int(r >= thr[0]), int(g >= thr[1]), int(b >= thr[2])), "?")
        if c != last:
            ev.append((t, c))
            last = c
    return ev


def video_frames(path, roi, fps=None):
    x, y, w, h = roi
    if fps is None:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=avg_frame_rate", "-of", "csv=p=0", path],
                             capture_output=True, text=True, check=True).stdout.strip()
        num, _, den = out.partition("/")
        fps = float(num) / float(den or 1)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vf",
                          f"crop={w}:{h}:{x}:{y},scale=1:1:flags=area", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return [(i / fps, raw[3 * i], raw[3 * i + 1], raw[3 * i + 2]) for i in range(len(raw) // 3)]


def _print_cycles(cycles):
    if not cycles:
        print("no START marker (2 s white) found")
    for c in cycles:
        print(describe(c))
        if c.get("check_ok"):
            print("  " + fields_line({k: c[k] for k in ("path", "flags", "ndef", "entries", "up", "usb")}))
    return 0 if any(c.get("check_ok") for c in cycles) else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("encode")
    e.add_argument("--path", type=int, required=True)
    e.add_argument("--flags", required=True)
    e.add_argument("--ndef", type=int, required=True)
    e.add_argument("--entries", default="", help="DD.C.SS,... (up to 3)")
    e.add_argument("--up", type=int, required=True)
    e.add_argument("--usb", required=True, help="F9: three digits, e.g. 000 or 999")
    c = sub.add_parser("collect")
    c.add_argument("root")
    c.add_argument("--path", type=int, default=1)
    c.add_argument("--up", type=int, default=0)
    d = sub.add_parser("decode")
    d.add_argument("events")
    fr = sub.add_parser("frames")
    fr.add_argument("csv")
    fr.add_argument("--thr", help="R,G,B thresholds (default: per-channel midpoint)")
    v = sub.add_parser("video")
    v.add_argument("clip")
    v.add_argument("--roi", required=True, help="X,Y,W,H of a box around the LED")
    v.add_argument("--fps", type=float)
    v.add_argument("--thr")
    v.add_argument("--dump", help="also write the per-frame CSV here")
    sub.add_parser("worst")
    a = ap.parse_args()
    if a.cmd in ("encode", "collect"):
        if a.cmd == "encode":
            ents = [tuple(int(x) for x in s.split(".")) for s in a.entries.split(",") if s]
            if not re.fullmatch(r"[0-9]{3}", a.usb):
                ap.error("--usb takes three digits")
            f = {"path": a.path, "flags": a.flags, "ndef": a.ndef, "entries": ents, "up": a.up,
                 "usb": tuple(int(x) for x in a.usb)}
        else:
            f = collect(a.root, a.path, a.up)
            print("drivers: " + " ".join(f["drivers"]))
            print("usbdiag: " + f["usbdiag"])
        t, cs = encode_fields(f)
        print(fields_line(f, cs))
        print(f"cycle {cycle_seconds(t):.2f} s, 3 cycles {3 * cycle_seconds(t):.2f} s")
        print(timeline_str(t))
        return 0
    if a.cmd == "worst":
        print(f"worst-case cycle {worst_cycle():.2f} s, 3 cycles {3 * worst_cycle():.2f} s")
        return 0
    thr = [float(x) for x in a.thr.split(",")] if getattr(a, "thr", None) else None
    if a.cmd == "decode":
        ev = []
        for line in open(a.events):
            line = line.split("#")[0].split()
            if len(line) >= 2:
                ev.append((float(line[0]), line[1].lower()))
        return _print_cycles(decode(ev))
    if a.cmd == "frames":
        rows = []
        for line in open(a.csv):
            parts = re.split(r"[,\s]+", line.strip())
            try:
                rows.append(tuple(float(x) for x in parts[:4]))
            except ValueError:
                continue
        return _print_cycles(decode(classify_frames(rows, thr), end=rows[-1][0] if rows else None))
    if a.cmd == "video":
        rows = video_frames(a.clip, [int(x) for x in a.roi.split(",")], a.fps)
        if a.dump:
            with open(a.dump, "w") as f:
                f.writelines(f"{t:.4f},{r},{g},{b}\n" for t, r, g, b in rows)
        return _print_cycles(decode(classify_frames(rows, thr), end=rows[-1][0] if rows else None))


if __name__ == "__main__":
    sys.exit(main())
