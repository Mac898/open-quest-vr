#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Offline tests for tools/led_code.py: encoder against the symbol timings,
decoder round trips from exact events and from simulated 30 fps video frames, the F9
sources (/proc/interrupts, UDC state, dwc3 regdump). Host-only.

Run: python3 -I tools/test_led_code.py
"""
import os
import random
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import led_code as lc  # noqa: E402

FAILS = []


def check(cond, what):
    print(("PASS  " if cond else "FAIL  ") + what)
    if not cond:
        FAILS.append(what)


def events(t, t0=0.0, scale=1.0):
    """Exact (seconds, colour) events for a timeline."""
    ev, x = [], t0
    for c, a, b in t:
        if a:
            ev.append((x, c))
            x += a / 1000 * scale
        ev.append((x, "dark"))
        x += b / 1000 * scale
    return ev, x


def frames(ev, end, fps=30.0, rng=None, bleed=0.25, noise=12, phase=0.0):
    """Simulated camera: per-frame mean RGB of the LED box. Each frame integrates the
    LED over its exposure (here: point sample at mid-frame), red/green/blue bleed into
    each other, plus noise and a dim ambient level."""
    rng = rng or random.Random(0)
    base = {"dark": (0, 0, 0), "red": (1, 0, 0), "green": (0, 1, 0), "blue": (0, 0, 1),
            "white": (1, 1, 1)}
    rows, i, k = [], 0, 0
    while True:
        t = phase + i / fps
        if t >= end:
            break
        while k + 1 < len(ev) and ev[k + 1][0] <= t:
            k += 1
        r, g, b = base[ev[k][1]] if ev and ev[0][0] <= t else (0, 0, 0)
        mix = [r + bleed * (g + b), g + bleed * (r + b), b + bleed * (r + g)]
        rows.append((t, *[max(0, min(255, 30 + 200 * m + rng.uniform(-noise, noise))) for m in mix]))
        i += 1
    return rows


def main():
    # symbol timings.
    v2ex = (1, "GBRRRBBBBRRR", 10, [(7, 1, 3), (11, 0, 0), (14, 1, 3)], 63)
    t, cs = lc.encode(*v2ex, (9, 9, 9))
    check(t[0] == ("white", 2000, 1500) and t[-1] == ("dark", 0, 3000), "encode: START and END")
    check(("red", 250, 250) in t and ("blue", 1000, 250) in t and ("dark", 0, 750) in t
          and ("green", 400, 400) in t and ("white", 250, 1000) in t, "encode: digit, zero, flag, SEP symbols")
    # v1 reference example (checksum 4, 69.85 s) + F9 "999" (27 -> checksum 1; 3 x 5.25 s + SEP 1.25 s).
    check(cs == 1 and abs(lc.cycle_seconds(t) - (69.85 + 17.0 - 1.5)) < 1e-9,
          f"encode: spec example + F9 999: checksum 1, cycle 85.35 s ({cs}, {lc.cycle_seconds(t)})")
    t0, cs0 = lc.encode(*v2ex, (0, 0, 0))
    check(cs0 == 4 and abs(lc.cycle_seconds(t0) - (69.85 + 7.25)) < 1e-9,
          f"encode: F9 000 adds 7.25 s and nothing to the checksum ({cs0}, {lc.cycle_seconds(t0)})")
    # F9 sits between F7's SEP and F8: three blue digits then a SEP.
    f9 = lc._digits("blue", "012", 3) + lc._sep()
    t1, cs1 = lc.encode(2, "GGGGGGGGGGRG", 0, [], 1, (0, 1, 2))
    f7 = lc._digits("green", 1, 3) + lc._sep()
    i = next(k for k in range(len(t1)) if t1[k:k + len(f7)] == f7)
    check(t1[i + len(f7):i + len(f7) + len(f9)] == f9 and t1[i + len(f7) + len(f9):] ==
          lc._digits("red", cs1, 1) + [("dark", 0, 3000)], "encode: F9 = 3 blue digits + SEP, before F8")
    check(lc.fields_line({"path": 1, "flags": "GBRRRBBBBRRR", "ndef": 10,
                          "entries": [(7, 1, 3), (11, 0, 0), (14, 1, 3)], "up": 63, "usb": (9, 9, 9)})
          == "ledcode v2 path=1 flags=GBRRRBBBBRRR ndef=10 d=07.1.03,11.0.00,14.1.03 up=63 usb=999 sum=1",
          "fields_line: v2 line (spec example + usb=999)")
    check(abs(lc.worst_cycle() - 158.6) < 1e-9, f"worst-case cycle 158.60 s ({lc.worst_cycle()})")
    # Shortest element >= 250 ms, shortest meaningful dark gap >= 1000 ms (30 fps: 7.5 / 30 frames).
    check(min(a for _, a, _ in t if a) >= 250, "encode: every flash >= 250 ms")

    rng = random.Random(16)
    ok_exact = ok_video = ok_jitter = True
    for i in range(300):
        p = rng.randint(0, 9)
        fl = "".join(rng.choice("GBR") for _ in range(12))
        nd = rng.randint(0, 99)
        ents = [(rng.randint(0, 99), rng.randint(0, 9), rng.randint(0, 99)) for _ in range(rng.randint(0, 3))]
        up = rng.randint(0, 1200)
        usb = (rng.choice([0, 1, 2, 3, 9]), rng.choice([0, 1, 2, 3, 4, 9]), rng.choice([0, 1, 2, 3, 9]))
        t, cs = lc.encode(p, fl, nd, ents, up, usb)
        want = {"path": p, "flags": fl, "ndef": nd, "entries": (ents + [(0, 0, 0)] * 3)[:3], "up": min(up, 999),
                "usb": usb}
        ev, end = events(t + t)                          # two cycles back to back
        r = lc.decode(ev, end=end)
        ok_exact &= len(r) == 2 and all(x["check_ok"] and {k: x[k] for k in want} == want for x in r)
        if i < 60:
            # +-20 ms jitter on every edge, slow clock drift of 2 %.
            jev, x = [], 3.7
            for c, a, b in t:
                if a:
                    jev.append((x + rng.uniform(-0.02, 0.02), c))
                    x += a / 1000 * 1.02
                jev.append((x + rng.uniform(-0.02, 0.02), "dark"))
                x += b / 1000 * 1.02
            r = lc.decode(jev, end=x)
            ok_jitter &= len(r) == 1 and r[0]["check_ok"] and {k: r[0][k] for k in want} == want
            # Simulated 30 fps video, random phase, colour bleed and noise.
            vev, vend = events(t, t0=1.0)
            rows = frames(vev, vend + 1, rng=rng, phase=rng.random() / 30)
            r = lc.decode(lc.classify_frames(rows), end=rows[-1][0])
            ok_video &= len(r) == 1 and r[0]["check_ok"] and {k: r[0][k] for k in want} == want
    check(ok_exact, "decode: 300 random codes x 2 cycles round-trip from exact events")
    check(ok_jitter, "decode: 60 random codes with +-20 ms edge jitter and 2 % slow clock")
    check(ok_video, "decode: 60 random codes from simulated 30 fps frames (bleed, noise, random phase)")

    # A clip that starts mid-cycle: the partial cycle is skipped, the next one decodes.
    t, _ = lc.encode(2, "GGGGGGGGGGRG", 0, [], 61, (0, 0, 0))
    ev, end = events(t + t)
    cut = [(s, c) for s, c in ev if s >= 20.0]
    r = lc.decode(cut, end=end)
    check(len(r) == 1 and r[0]["check_ok"] and r[0]["path"] == 2,
          "decode: clip starting mid-cycle -> the next full cycle decodes")
    # A corrupted cycle (one flash lost) fails the checksum or the format, never passes silently.
    t, _ = lc.encode(*v2ex, (2, 1, 0))
    ev, end = events(t)
    last_green = max(j for j, e in enumerate(ev) if e[1] == "green")     # F7 "063" -> "062"
    lost = [e for j, e in enumerate(ev) if j != last_green]
    r = lc.decode(lost, end=end)
    check(len(r) == 1 and not r[0]["check_ok"], "decode: a lost flash is reported, not silently accepted")
    # describe() names the devices.
    r = lc.decode(events(t)[0], end=events(t)[1])
    d = lc.describe(r[0])
    check("100000.clock-controller" in d and "fw_devlink" in d and "checksum OK" in d
          and "F9.2 UDC state: 1 = default" in d and "ep0 data/TRB path or PHY (H3)" in d,
          "describe: names devices, reasons, F9 and its reading, and the checksum state")
    # A lost F9 flash: F9 "210" -> "110" fails the checksum.
    i = next(j for j, e in enumerate(ev) if e[1] == "blue" and ev[j - 1][1] == "dark"
             and j > last_green)
    r = lc.decode([e for j, e in enumerate(ev) if j not in (i, i + 1)], end=end)
    check(len(r) == 1 and not r[0]["check_ok"], "decode: a lost F9 flash is reported")
    # F9 readings.
    rd = {(0, 0, 0): "stalled", (0, 0, 1): "IRQ not delivered", (1, 0, 0): "redirect",
          (3, 0, 1): "redirect", (2, 1, 0): "H3", (1, 2, 0): "H3", (2, 3, 0): "fix worked",
          (3, 3, 0): "fix worked", (0, 0, 2): "abort mode", (3, 4, 3): "abort mode",
          (9, 9, 9): "unreadable", (1, 3, 0): "no known reading"}
    bad = {u: lc.usb_reading(u) for u, w in rd.items() if w not in lc.usb_reading(u)}
    check(not bad, f"usb_reading: reading table ({bad})")

    # F9 sources from a fixture tree.
    root = Path(tempfile.mkdtemp(prefix="led-code-"))
    (root / "proc").mkdir(parents=True)
    udc = root / lc.UDC_DIR
    udc.mkdir(parents=True)
    rdp = root / lc.REGDUMP
    rdp.parent.mkdir(parents=True)
    hdr = "           CPU0       CPU1       CPU2       CPU3       CPU4       CPU5       CPU6       CPU7\n"
    irqs = [("", None, "none"),
            (" 165:          0          0          0          0          0          0          0          0     GICv3 165 Level     dwc3\n", 0, "zero"),
            (" 165:        800        102          0          0          0          0          0          0     GICv3 165 Level     dwc3\n", 902, "902"),
            (" 165:          5          0     GICv3 165 Level     dwc3\n 166:  7  dwc3\n", 5, "first line wins"),
            (" 165:  1000  GICv3 165 Level  dwc3-x\n 9:\t0009\t3\tGICv3\tdwc3", 12, "tabs, leading zeros, no newline"),
            (" 165: 123456789012 99999999 GICv3 dwc3\n", lc.IRQ_CAP, "capped"),
            ("dwc3\n 1: 4 x\n", None, "lone token, last token not dwc3")]
    ok = True
    for body, want, what in irqs:
        got = lc.irq_count(hdr + body)
        ok &= got == want
        if got != want:
            print("   irq", what, got, want)
    check(ok, "irq_count: dwc3 line rules (last token, first line, numeric run, caps)")
    check([lc.irq_digit(n) for n in (None, 0, 1, 9, 10, 999, 1000, 10 ** 8)] == [9, 0, 1, 1, 2, 2, 3, 3],
          "irq_digit: 9 / 0 / 1-9 / 10-999 / >= 1000")
    reg = ("GSTS = 0x00000010\nGSTS = 0x00000000\nGEVNTCOUNT(0) = 0x00000004\nDCTL = 0x8c000000\n"
           "GBUSERRADDR0 = 0x9a000000\nGBUSERRADDR1 = 0x00000000\nDSTS = 0xzz\nX = 1 = 2\n")
    (root / "proc/interrupts").write_text(hdr + irqs[2][0])
    (udc / "state").write_text("not attached\n")
    (udc / "current_speed").write_text("high-speed\n")
    rdp.write_text(reg)
    usb, line = lc.usb_fields(str(root))
    check(usb == (2, 0, 3) and line == "irq=902 state=not_attached speed=high-speed GSTS=0x00000010 "
          "BUSERR=0x00000000:0x9a000000 EVADR=na:na EVSIZ=na EVCNT=0x00000004 DCTL=0x8c000000 DEVTEN=na "
          "DSTS=na f9=203", f"usb_fields: first valid value per register, hi:lo pairs ({usb}, {line})")
    for st, d in [("default", 1), ("addressed", 2), ("configured", 3), ("suspended", 4), ("powered", 0),
                  ("attached", 0), ("reconnecting", 9), ("", 9), ("  configured\t", 3)]:
        (udc / "state").write_text(st + "\n")
        ok &= lc.usb_fields(str(root))[0][1] == d
    (udc / "state").unlink()
    ok &= lc.usb_fields(str(root))[0][1] == 9
    check(ok, "usb_fields: UDC state digit (and 9 for missing/other)")
    rdp.write_text("GSTS = 0x00000000\n")
    check(lc.usb_fields(str(root))[0][2] == 9, "usb_fields: regdump without GEVNTCOUNT(0) -> 9")
    rdp.write_text("GSTS = 0x00000000\nGEVNTCOUNT(0) = 0x80000003\n")
    check(lc.usb_fields(str(root))[0][2] == 0, "usb_fields: GEVNTCOUNT(0) & 0xfffc (EHB, low bits ignored)")
    rdp.unlink()
    check(lc.usb_fields(str(root))[0][2] == 9 and lc.usb_fields(str(root), "")[1].endswith("f9=299"),
          "usb_fields: no regdump -> 9")
    shutil.rmtree(root, ignore_errors=True)
    print(f"\n{len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
