#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Offline simulation of `tools/dev fan-test` and `load-step` under the target's busybox.

Never contacts the headset. A rootless arm64 podman container (CAP_SYS_ADMIN, not
--privileged, no network) runs the static busybox 1.36.1 packed in the mainline initramfs
(rootfs/out/mainline-init.cpio.gz). /sys/class is covered by a tmpfs that holds a fake hwmon
and thermal tree; a small background loop plays the fan (fan1_input follows pwm1). tools/dev's
raw transport (`_send`) is swapped for `podman exec`, so the exact composed command that
`fan_test()` would send runs under the same busybox applets.

What it can't model: the real pwm-fan driver (pwm1 writes go to plain files here), the
interactive telnet shell (here `sh -c`; see tools/test_dev_telnet_sim.py), and a real pty hangup (HUP/TERM are sent to the
command's process group instead).

Checks: hwmon discovery by name + driver only (no match, two matches, wrong driver: nothing
written); mode/value gates (pwm1_enable != 1, pwm1 = 0: nothing written); normal run restores
pwm1/pwm1_enable and reports the rise; no rise -> exit 3; a hot zone or no readable zone
leaves pwm1 at 255 (exit 5); pwm1_enable changed during the test is written back; HUP and
TERM mid-run still restore (trap); SIGKILL leaves 255 (documented fail-safe).
load-step: real `yes` workers start on the listed CPUs, end by their own timeout, survive the
launching shell, and the guard kills them (explicit PIDs) when a zone crosses 78 C.
Run: python3 -I tools/test_fan_sim.py [--initramfs PATH]
"""
import argparse
import gzip
import importlib.machinery
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

FAN = "/sys/class/hwmon/hwmon1"
DEFAULT_INITRAMFS = os.path.join(ROOT, "rootfs/out/mainline-init.cpio.gz")


def work_base():
    """Parent of the per-run work dir: $TMPDIR (default /tmp)."""
    return tempfile.gettempdir()


def load_dev():
    loader = importlib.machinery.SourceFileLoader("dev", os.path.join(HERE, "dev"))
    spec = importlib.util.spec_from_loader("dev", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def cpio_member(data, want):
    off = 0
    while off < len(data):
        h = data[off:off + 110]
        assert h[:6] == b"070701", "not newc cpio"
        size, namesize = int(h[54:62], 16), int(h[94:102], 16)
        name = data[off + 110:off + 110 + namesize - 1].decode()
        off = (off + 110 + namesize + 3) & ~3
        if name == want:
            return data[off:off + size]
        if name == "TRAILER!!!":
            break
        off = (off + size + 3) & ~3
    raise SystemExit(f"{want} not in initramfs")


def tree(hwmons, zones):
    """Shell (setup only, never a device command) that rebuilds the fake /sys/class tree.
    hwmons: list of (index, name, driver, pwm1, pwm1_enable, rpm); zones: list of temp strings."""
    s = ["rm -rf /sys/class/hwmon /sys/class/thermal /fake", "mkdir -p /sys/class/hwmon /sys/class/thermal"]
    for i, name, drv, pwm, en, rpm in hwmons:
        h = f"/sys/class/hwmon/hwmon{i}"
        s += [f"mkdir -p {h} /fake/dev{i} /fake/drv/{drv}", f"ln -s /fake/drv/{drv} /fake/dev{i}/driver",
              f"ln -s /fake/dev{i} {h}/device", f"echo {name} > {h}/name", f"echo {pwm} > {h}/pwm1",
              f"echo {en} > {h}/pwm1_enable", f"echo {rpm} > {h}/fan1_input"]
    for j, t in enumerate(zones):
        z = f"/sys/class/thermal/thermal_zone{j}"
        s += [f"mkdir -p {z}", f"echo tz{j} > {z}/type", f"echo {t} > {z}/temp"]
    g = "/sys/class/power_supply/bq27z561-0"
    s += [f"rm -rf /sys/class/power_supply; mkdir -p {g}", f"echo 88 > {g}/capacity", f"echo 360 > {g}/temp",
          f"echo 4100000 > {g}/voltage_now", f"echo -900000 > {g}/current_now", f"echo 3000000 > {g}/charge_now"]
    return "; ".join(s)


FANSIM = ("echo $$ > /tmp/fansim.pid; while :; do p=$(cat {f}/pwm1 2>/dev/null); "
          "if [ \"$p\" = 255 ]; then echo {hi} > {f}/fan1_input; {extra} else echo 4340 > {f}/fan1_input; fi; "
          "sleep 0.2; done")


class Sim:
    """One stand-in container (its own root under the work dir) and the per-part helpers."""

    def __init__(self, work, part, initramfs, dev):
        self.dev, self.sent, self.fails, self.total = dev, [], 0, 0
        self.work, self.part = work, part
        self.sim = os.path.join(work, f"root-{part}")
        for d in ("bin", "proc", "sys", "dev", "tmp"):
            os.makedirs(os.path.join(self.sim, d))
        bb = os.path.join(self.sim, "bin", "busybox")
        with open(bb, "wb") as f:
            f.write(cpio_member(gzip.open(initramfs).read(), "bin/busybox"))
        os.chmod(bb, 0o755)
        self.name = f"fansim-{os.getpid()}-{part}"
        subprocess.run(["podman", "run", "-d", "--rm", "--name", self.name,
                        "--cap-add", "SYS_ADMIN", "--security-opt", "seccomp=unconfined",
                        "--security-opt", "apparmor=unconfined", "--network", "none",
                        "--arch", "arm64", "--rootfs", self.sim, "/bin/busybox", "sh", "-c",
                        "/bin/busybox --install -s /bin; mount -t tmpfs none /sys/class; "
                        # PID 1 reaps adopted orphans like the device's init shell does.
                        "k=0; while [ $k -lt 900 ]; do sleep 1; k=$((k+1)); done"],
                       check=True, stdout=subprocess.DEVNULL)
        dev._send = self.fake_send

    def px(self, cmd, timeout=60):
        p = subprocess.run(["podman", "exec", self.name, "/bin/sh", "-c", cmd],
                           capture_output=True, text=True, timeout=timeout)
        return p.stdout + p.stderr, p.returncode

    def fake_send(self, cmd, timeout):
        # The real _send's guard (line length, control characters) applies here too; the
        # interactive telnet shell itself is simulated in tools/test_dev_telnet_sim.py.
        why = self.dev.transport_refusal(cmd)
        if why:
            sys.exit(f"tools/dev: refused before sending ({why})")
        self.sent.append(cmd)
        return self.px(cmd, timeout)

    def expect(self, label, cond, detail=""):
        self.fails += not cond
        self.total += 1
        print(f"{'ok  ' if cond else 'FAIL'} {label}" + (f"  [{str(detail).strip()[:400]}]" if detail and not cond else ""))

    def fansim(self, on=True, hi=6950, extra=""):
        self.px("if [ -f /tmp/fansim.pid ]; then kill $(cat /tmp/fansim.pid) 2>/dev/null; rm -f /tmp/fansim.pid; fi")
        if on:
            subprocess.run(["podman", "exec", "-d", self.name, "/bin/sh", "-c",
                            FANSIM.format(f=FAN, hi=hi, extra=extra)], check=True,
                           stdout=subprocess.DEVNULL)
            time.sleep(0.5)

    def state(self, h=FAN):
        out, _ = self.px(f"echo $(cat {h}/pwm1_enable) $(cat {h}/pwm1)")
        return out.split()

    def quiet(self, fn, *args):
        buf, real = io.StringIO(), sys.stdout
        sys.stdout = buf
        n0 = len(self.sent)
        try:
            rc = fn(*args)
        except SystemExit as e:
            rc = e.code
        finally:
            sys.stdout = real
        return rc, buf.getvalue(), self.sent[n0:]

    def run_ft(self, seconds):
        return self.quiet(self.dev.fan_test, seconds)

    def close(self):
        subprocess.run(["podman", "rm", "-f", "-t", "0", self.name],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(os.path.join(self.work, f"sent-{self.part}.txt"), "w") as f:
            f.write("\n".join(self.sent) + "\n")
        shutil.rmtree(self.sim, ignore_errors=True)


FANROW = (1, "pwmfan", "pwm-fan", 84, 1, 4340)
GAUGE = (0, "bq27z561_0", "bq27xxx-battery", 0, 0, 0)


def part_fan(x):
    """Cases 1-3: normal run, no rise, small rise."""
    dev, px, expect, state, fansim, run_ft = x.dev, x.px, x.expect, x.state, x.fansim, x.run_ft
    out, _ = px("busybox | head -1")
    expect("stand-in runs target busybox 1.36.1", "v1.36.1" in out, out)

    # 1. Normal run: rise, restored, exactly one command sent.
    px(tree([GAUGE, FANROW], ["45000", "41000"]))
    fansim()
    rc, out, cmds = run_ft(6)
    expect("normal: exit 0", rc == 0, out)
    expect("normal: one command sent, the composed fan-test", cmds == [dev.fan_test_cmd(6)], cmds)
    expect("normal: 3 samples at pwm 255, max rpm 6950", out.count("FAN_S ") == 3 and "max rpm 6950" in out, out)
    expect("normal: hwmon found by name+driver (hwmon1, not the gauge's hwmon0)",
           "FAN_HWMON=/sys/class/hwmon/hwmon1 " in out, out)
    expect("normal: pwm1_enable 1, pwm1 84 afterwards", state() == ["1", "84"], state())
    expect("normal: gauge hwmon untouched", state("/sys/class/hwmon/hwmon0") == ["0", "0"])
    expect("normal: summary says restored", "restored" in out.splitlines()[-1], out)

    # 2. No rise -> exit 3, still restored.
    px(tree([GAUGE, FANROW], ["45000"]))
    fansim(on=False)
    rc, out, _ = run_ft(4)
    expect("no rise: exit 3 (abort the test)", rc == 3 and "DID NOT RISE" in out, out)
    expect("no rise: restored to 1 84", state() == ["1", "84"], state())

    # 3. Small rise below the threshold -> exit 3.
    px(tree([GAUGE, FANROW], ["45000"]))
    fansim(hi=4600)
    rc, out, _ = run_ft(4)
    expect("rise of 260 rpm only: exit 3", rc == 3, out)
    expect("small rise: restored to 1 84", state() == ["1", "84"], state())
    fansim(on=False)


def part_fan_hot(x):
    """Cases 4-7: hot or unreadable zones, start at 255, enable changed mid-test."""
    px, expect, state, fansim, run_ft = x.px, x.expect, x.state, x.fansim, x.run_ft
    # 4. A zone >= 65 C at restore: pwm1 left at 255 (fail-safe), exit 5.
    px(tree([GAUGE, FANROW], ["45000", "65000"]))
    fansim()
    rc, out, _ = run_ft(4)
    expect("hot zone: exit 5, FAN_LEFT_255", rc == 5 and "FAN_LEFT_255 tmax=65000" in out, out)
    expect("hot zone: pwm1 stays 255, enable 1", state() == ["1", "255"], state())

    # 5. No readable zone: left at 255 (exit 5).
    px(tree([GAUGE, FANROW], ["", "abc"]))
    fansim()
    rc, out, _ = run_ft(4)
    expect("no readable zone: exit 5, zones=0", rc == 5 and "zones=0" in out, out)
    expect("no readable zone: pwm1 stays 255", state() == ["1", "255"], state())

    # 6. Original pwm1 255 + hot: restore writes 255 (no LEFT line); no rise -> exit 3.
    px(tree([GAUGE, (1, "pwmfan", "pwm-fan", 255, 1, 6950)], ["70000"]))
    fansim()
    rc, out, _ = run_ft(4)
    expect("start at 255: restored 255 without FAN_LEFT_255", "FAN_LEFT_255" not in out
           and state() == ["1", "255"], out)

    # 7. pwm1_enable changed during the test: the trap writes the recorded 1 back.
    px(tree([GAUGE, FANROW], ["45000"]))
    fansim(extra=f"echo 2 > {FAN}/pwm1_enable;")
    rc, out, _ = run_ft(4)
    expect("enable changed mid-test: restored to 1 84, exit 0", rc == 0 and state() == ["1", "84"],
           f"{rc} {state()} {out}")
    fansim(on=False)


def part_gates_signals(x):
    """Cases 8-15: the gates (nothing written), HUP/TERM mid-run (restored), KILL (left at 255)."""
    dev, px, expect, state, fansim, run_ft = x.dev, x.px, x.expect, x.state, x.fansim, x.run_ft
    # 8-12. Gates: nothing written.
    gates = [
        ("no pwmfan hwmon", [GAUGE], FAN.replace("hwmon1", "hwmon0"), ["0", "0"]),
        ("name pwmfan but another driver", [GAUGE, (1, "pwmfan", "gpio-fan", 84, 1, 4340)], FAN, ["1", "84"]),
        ("driver pwm-fan but another name", [GAUGE, (1, "pwm_fan", "pwm-fan", 84, 1, 4340)], FAN, ["1", "84"]),
        ("two pwmfan hwmons", [FANROW, (2, "pwmfan", "pwm-fan", 84, 1, 4340)], FAN, ["1", "84"]),
        ("pwm1_enable 0 (fan hard off)", [GAUGE, (1, "pwmfan", "pwm-fan", 84, 0, 0)], FAN, ["0", "84"]),
        ("pwm1_enable 2", [GAUGE, (1, "pwmfan", "pwm-fan", 84, 2, 4340)], FAN, ["2", "84"]),
        ("pwm1 0", [GAUGE, (1, "pwmfan", "pwm-fan", 0, 1, 0)], FAN, ["1", "0"]),
        ("pwm1 not a number", [GAUGE, (1, "pwmfan", "pwm-fan", "x", 1, 0)], FAN, ["1", "x"]),
    ]
    fansim(on=False)
    for label, hw, h, want in gates:
        px(tree(hw, ["45000"]))
        rc, out, _ = run_ft(4)
        expect(f"gate {label}: exit 2, no FAN_SET, files unchanged",
               rc == 2 and "FAN_SET" not in out and state(h) == want, f"{rc} {state(h)} {out}")

    # 13-14. HUP / TERM mid-run (process group, as a hangup reaches the session): restored.
    cmd20 = dev.fan_test_cmd(20)
    for sig in ("HUP", "TERM"):
        px(tree([GAUGE, FANROW], ["45000"]))
        fansim()
        px("rm -f /tmp/ft.out; echo " + "'" + cmd20.replace("'", "'\\''") + "' > /tmp/ft.sh")
        t0 = time.time()
        out, _ = px("setsid sh /tmp/ft.sh > /tmp/ft.out 2>&1 & P=$!; sleep 3; "
                    f"echo mid=$(cat {FAN}/pwm1); kill -{sig} -$P; sleep 1; "
                    "k=0; while [ $k -lt 75 ] && ! grep -q FAN_RESTORED /tmp/ft.out; do sleep 0.2; k=$((k+1)); done; "
                    "cat /tmp/ft.out", 60)
        dt = time.time() - t0
        expect(f"{sig} mid-run: pwm1 was 255 before the signal", "mid=255" in out, out)
        expect(f"{sig} mid-run: FAN_RESTORED, pwm1_enable 1, pwm1 84, ended early ({dt:.0f} s)",
               "FAN_RESTORED enable=1 pwm=84" in out and state() == ["1", "84"] and dt < 15,
               f"{state()} {out}")

    # 15. SIGKILL cannot be trapped: pwm1 stays 255 (documented fail-safe direction).
    px(tree([GAUGE, FANROW], ["45000"]))
    fansim()
    out, _ = px("setsid sh /tmp/ft.sh > /tmp/ft.out 2>&1 & P=$!; sleep 3; kill -KILL -$P; sleep 1; cat /tmp/ft.out")
    expect("KILL mid-run: no restore line; pwm1 left at 255 (safe direction)",
           "FAN_RESTORED" not in out and state() == ["1", "255"], f"{state()} {out}")
    fansim(on=False)


def part_load_timeout(x):
    """Case 16: load-step 2 x 30 s, workers end by their own timeout."""
    dev, px, expect, fansim = x.dev, x.px, x.expect, x.fansim
    px(tree([GAUGE, FANROW], ["45000", "41000"]))
    fansim()
    t0 = time.time()
    rc, out, _ = x.quiet(dev.load_step, 2, 30)
    ps, _ = px("pidof yes; echo .")
    expect("load-step 2 x 30 s: exit 0, workers ended by their timeout, none left",
           rc == 0 and "workers ended by their timeout" in out and ps.split() == ["."], f"{rc} {ps} {out}")
    expect("load-step: 2 workers seen in the samples (yes=2)", "yes=2" in out, out)
    expect(f"load-step: took about S + 3 s ({time.time() - t0:.0f} s)", 30 <= time.time() - t0 < 60)
    fansim(on=False)


def part_load_guard(x):
    """Case 17: a zone crosses 78 C mid-step: the guard kills the real workers at once."""
    dev, px, expect = x.dev, x.px, x.expect
    px(tree([GAUGE, FANROW], ["45000", "41000"]))
    subprocess.run(["podman", "exec", "-d", x.name, "/bin/sh", "-c",
                    "sleep 20; echo 78500 > /sys/class/thermal/thermal_zone1/temp"], check=True,
                   stdout=subprocess.DEVNULL)
    t0 = time.time()
    rc, out, _ = x.quiet(dev.load_step, 8, 120)
    ps, _ = px("pidof yes; echo .")
    expect(f"load-step 8 cores, zone 78.5 C at ~20 s: exit 10, killed, none left ({time.time() - t0:.0f} s)",
           rc == 10 and "STOP (zone)" in out and "still running: none" in out and ps.split() == ["."]
           and time.time() - t0 < 60, f"{rc} {ps} {out}")
    kills = [c for c in x.sent if c.startswith("kill ")]
    expect("load-step: the stop was one `kill` of the 8 started PIDs",
           len(kills) == 1 and len(kills[0].split()) == 9, kills)


def part_load_orphan(x):
    """Case 18: workers survive the launching shell (nohup) and end by themselves (timeout)."""
    dev, px, expect = x.dev, x.px, x.expect
    out, _ = px(dev.load_start_cmd(1, 30))
    pid = [w[4:] for w in out.split() if w.startswith("pid=")]
    time.sleep(2)
    ps1, _ = px("pidof yes; echo .")
    time.sleep(31)
    ps2, _ = px("pidof yes; echo .")
    expect("a worker outlives its launching shell and ends at its timeout with no host action",
           len(pid) == 1 and ps1.split() == pid + ["."] and ps2.split() == ["."], f"{pid} {ps1} {ps2}")


# each part has its own container, so the parts run in parallel (load-step's 30 s minimum
# and the worker timeouts are real seconds in tools/dev: they cannot be scaled, only overlapped).
PARTS = {"fan": part_fan, "fan_hot": part_fan_hot, "gates_signals": part_gates_signals, "load_timeout": part_load_timeout,
         "load_guard": part_load_guard, "load_orphan": part_load_orphan}


def run_part(part, initramfs, work):
    dev = load_dev()
    x = Sim(work, part, initramfs, dev)
    try:
        PARTS[part](x)
    finally:
        x.close()
    print(f"PART {part} {x.total - x.fails}/{x.total}")
    return x.fails


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--initramfs", default=DEFAULT_INITRAMFS)
    ap.add_argument("--part", choices=sorted(PARTS), help=argparse.SUPPRESS)
    ap.add_argument("--work", help=argparse.SUPPRESS)
    ap.add_argument("--jobs", "-j", type=int, default=len(PARTS), help="parts at once (1 = serial)")
    a = ap.parse_args()
    if a.part:
        sys.exit(1 if run_part(a.part, a.initramfs, a.work) else 0)
    work = tempfile.mkdtemp(prefix="fansim-", dir=work_base())
    t0 = time.monotonic()
    procs, outs, pending = {}, {}, list(PARTS)
    while pending or procs:
        while pending and len(procs) < max(1, a.jobs):
            part = pending.pop(0)
            procs[part] = (subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), "--part", part,
                                             "--initramfs", a.initramfs, "--work", work],
                                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True),
                           time.monotonic())
        done = None
        while done is None:
            done = next((p for p, (pr, _) in procs.items() if pr.poll() is not None), None)
            if done is None:
                time.sleep(0.2)
        pr, ts = procs.pop(done)
        outs[done] = (pr.stdout.read(), pr.returncode, time.monotonic() - ts)
    passed = total = 0
    for part in PARTS:
        text, rc, dt = outs[part]
        print(f"-- part {part} ({dt:.0f}s)")
        print(text, end="")
        m = re.search(r"^PART \S+ (\d+)/(\d+)$", text, re.M)
        p, n = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        passed, total = passed + p, total + n
        if rc != 0 and p == n:                     # crashed part (no FAIL line of its own)
            total += 1
            print(f"FAIL part {part}: exit {rc}")
    print(f"\n{passed}/{total} passed (commands sent: {work}/sent-*.txt; {time.monotonic() - t0:.0f} s)")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
