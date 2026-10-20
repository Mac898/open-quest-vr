#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Offline simulation of tools/dev's real telnet transport against the target's telnet shell.

Never contacts the headset. A rootless arm64 podman container runs the static busybox 1.36.1
packed in the mainline initramfs (rootfs/out/mainline-init.cpio.gz) as the device does:
`telnetd -F -l /bin/sh -p 23`, i.e. an interactive ash on
a pty with BusyBox line editing (CONFIG_FEATURE_EDITING_MAX_LEN=1024). Its port 23 is published
on 127.0.0.1 only. A fresh copy of tools/dev is used unmodified except that its `socket` module
is a shim that redirects the (HOST, 23) connection to that port, so the real `_send` (wrapping,
guard, marker parsing) and the real `fan_test()` run end to end. /sys/class is a tmpfs with a
fake hwmon/thermal tree (tools/test_fan_sim.py); a small loop plays the fan.

Checks: the line-length limit of the target shell (a wrapped line of 1023 bytes completes, 1024
hangs at PS2); the one-line fan-test hangs the same way and writes nothing,
and the guard refuses it before connecting; the multi-line fan-test runs to exit 0
and restores pwm1; a fan-test cut off mid-transmission (connection closed) writes nothing; the
fixed read-only commands complete over the telnet shell.
cpufreq-cap cap/uncap/precheck/mismatch/status on a fake cpufreq tree; mod-load/mod-unload
with fake insmod/rmmod in /sbin; mod-fetch's member check and unpack on local tarballs; kexec-self
load/exec/unload with a fake /sbin/kexec (exec: every refusal path, then the watchdog node open on
fd 3 of `kexec -e`).
What it can't model: the real pwm-fan driver, cpufreq-hw, insmod, kexec, USB-ethernet timing.
Run: python3 -I tools/test_dev_telnet_sim.py [--initramfs PATH]
"""
import argparse
import gzip
import importlib.machinery
import importlib.util
import io
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)



def work_base():
    """Parent of the per-run work dir: $TMPDIR (default /tmp)."""
    return tempfile.gettempdir()


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def run_part(which, initramfs, base):
    """One part in its own container: "transport" (line limit, one-line fan-test, fan-test, cut link,
    read-only commands) or "fixed" (cpufreq-cap, modules, mod-fetch, kexec-self)."""
    fsim = load("test_fan_sim", os.path.join(HERE, "test_fan_sim.py"))
    deny = load("test_dev_denylist_pins", os.path.join(HERE, "test_dev_denylist.py"))
    socket.create_connection = REAL_CONNECT        # test_dev_denylist blocks the network on import
    socket.socket.connect = REAL_SOCK_CONNECT
    dev = fsim.load_dev()
    port = free_port()
    conns = []

    def connect(addr, timeout=None):
        conns.append(addr)
        if addr != (dev.HOST, 23):
            raise OSError(f"unexpected address {addr}")
        return REAL_CONNECT(("127.0.0.1", port), timeout=timeout)
    dev.socket = types.SimpleNamespace(create_connection=connect, timeout=socket.timeout)

    work = tempfile.mkdtemp(prefix=f"telnetsim-{which}-", dir=base)
    sim = os.path.join(work, "root")
    for d in ("bin", "sbin", "proc", "sys", "dev", "tmp"):
        os.makedirs(os.path.join(sim, d))
    for tool, body in FIXED_FAKES.items():         # fakes only: never the real insmod/rmmod/kexec
        with open(os.path.join(sim, "sbin", tool), "w") as f:
            f.write(body)
        os.chmod(os.path.join(sim, "sbin", tool), 0o755)
    bb = os.path.join(sim, "bin", "busybox")
    with open(bb, "wb") as f:
        f.write(fsim.cpio_member(gzip.open(initramfs).read(), "bin/busybox"))
    os.chmod(bb, 0o755)
    name = f"telnetsim-{os.getpid()}-{which}"
    subprocess.run(["podman", "run", "-d", "--rm", "--name", name,
                    "--cap-add", "SYS_ADMIN", "--security-opt", "seccomp=unconfined",
                    "--security-opt", "apparmor=unconfined", "-p", f"127.0.0.1:{port}:23",
                    "--arch", "arm64", "--rootfs", sim, "/bin/busybox", "sh", "-c",
                    "/bin/busybox --install -s /bin; mount -t tmpfs none /sys/class; "
                    "mkdir -p /sys/devices /sys/kernel 2>/dev/null; mount -t tmpfs none /sys/devices; "
                    "mount -t tmpfs none /sys/kernel; "
                    "(while :; do telnetd -F -l /bin/sh -p 23; sleep 1; done) & "
                    "k=0; while [ $k -lt 900 ]; do sleep 1; k=$((k+1)); done"],
                   check=True, stdout=subprocess.DEVNULL)
    fails, total = 0, 0

    def px(cmd, timeout=60):
        p = subprocess.run(["podman", "exec", name, "/bin/sh", "-c", cmd],
                           capture_output=True, text=True, timeout=timeout)
        return p.stdout + p.stderr, p.returncode

    def expect(label, cond, detail=""):
        nonlocal fails, total
        fails += not cond
        total += 1
        print(f"{'ok  ' if cond else 'FAIL'} {label}" + (f"  [{str(detail).strip()[:400]}]" if detail and not cond else ""))

    def state(h=fsim.FAN):
        out, _ = px(f"echo $(cat {h}/pwm1_enable) $(cat {h}/pwm1)")
        return out.split()

    def fansim(on=True):
        px("if [ -f /tmp/fansim.pid ]; then kill $(cat /tmp/fansim.pid) 2>/dev/null; rm -f /tmp/fansim.pid; fi")
        if on:
            subprocess.run(["podman", "exec", "-d", name, "/bin/sh", "-c",
                            fsim.FANSIM.format(f=fsim.FAN, hi=6950, extra="")], check=True,
                           stdout=subprocess.DEVNULL)
            time.sleep(0.5)

    def unguarded(cmd, timeout):
        """The real _send with the line limit lifted (measurement only; control chars still refused)."""
        dev.LINE_MAX = 1 << 20
        try:
            return dev._send(cmd, timeout)
        finally:
            dev.LINE_MAX = LINE_MAX

    LINE_MAX = dev.LINE_MAX
    tag = "Q%d" % os.getpid()
    FANROW = (1, "pwmfan", "pwm-fan", 84, 1, 4340)
    GAUGE = (0, "bq27z561_0", "bq27xxx-battery", 0, 0, 0)
    try:
        for _ in range(60):                       # telnetd up under qemu
            try:
                dev._send("echo up", 3)
                break
            except (OSError, TimeoutError):
                time.sleep(1)
        out, rc = dev._send("busybox | head -1; tty; echo $-", 10)
        expect("telnet shell: target busybox 1.36.1, interactive ash on a pty",
               "v1.36.1" in out and "/dev/pts/" in out and "i" in out.split()[-1], out)

        if which == "transport":
            # 1. The limit: wrapped line (newline included) of 1023 bytes completes, 1024 hangs.
            res = {}
            for n in (999, 1022, 1023, 1024, 1025):
                pad = n - len(dev._wrap("echo ", tag))
                try:
                    o, rc = unguarded("echo " + "x" * pad, 5)
                    res[n] = "ok" if rc == 0 and o.strip() == "x" * pad else f"bad rc={rc}"
                except TimeoutError:
                    res[n] = "hang"
            expect("line editor limit: 999/1022/1023-byte lines complete, 1024/1025 hang at PS2",
                   res == {999: "ok", 1022: "ok", 1023: "ok", 1024: "hang", 1025: "hang"}, res)

            # 2. The one-line fan-test: hangs, writes nothing; the guard refuses it unsent.
            px(fsim.tree([GAUGE, FANROW], ["45000", "41000"]))
            fansim()
            oneline = deny.FAN_TEST_TEMPLATE_ONELINE.replace("@N@", "2")
            try:
                o, rc = unguarded(oneline, 8)
                r = f"completed rc={rc}"
            except TimeoutError:
                r = "hang"
            time.sleep(1)
            expect("one-line fan-test (1385 B): hangs at PS2 and nothing runs (pwm1 stays 84)",
                   r == "hang" and state() == ["1", "84"], f"{r} {state()}")
            n0 = len(conns)
            try:
                dev._send(oneline, 8)
                r = "sent"
            except SystemExit as e:
                r = str(e.code)
            expect("guard: one-line fan-test refused before connecting",
                   "refused before sending" in r and len(conns) == n0, r)

            # 3. The multi-line fan-test over the real transport: exit 0, restored.
            buf, real = io.StringIO(), sys.stdout
            sys.stdout = buf
            try:
                rc = dev.fan_test(6)
            except SystemExit as e:
                rc = e.code
            finally:
                sys.stdout = real
            out = buf.getvalue()
            expect("fan-test 6 over telnet: exit 0, 3 samples at 255, max rpm 6950",
                   rc == 0 and out.count("FAN_S ") == 3 and "max rpm 6950" in out, out)
            expect("fan-test: pwm1_enable 1, pwm1 84 afterwards; gauge hwmon untouched",
                   state() == ["1", "84"] and state("/sys/class/hwmon/hwmon0") == ["0", "0"], state())
            expect("fan-test: the reply starts at FAN_HWMON (echoed lines and PS2 prompts precede BEGIN)",
                   out.startswith("FAN_HWMON=/sys/class/hwmon/hwmon1 "), out[:200])
            c60 = dev.fan_test_cmd(60)
            expect("fan-test 60: every wrapped line < LINE_MAX",
                   dev.transport_refusal(c60) is None
                   and max(len(x) + 1 for x in dev._wrap(c60, dev.TAG_MAX).split("\n")[:-1]) < LINE_MAX)

            # 4. Cut mid-transmission (first 4 of 8 lines, then the link drops): nothing runs.
            wrapped = dev._wrap(dev.fan_test_cmd(6), tag)
            part = "\n".join(wrapped.split("\n")[:4]) + "\n"
            s = REAL_CONNECT(("127.0.0.1", port), timeout=5)
            s.sendall(part.encode())
            time.sleep(3)
            s.close()
            time.sleep(3)
            out, _ = px("ps | grep -c '[s]leep 2$'; echo .")
            expect("cut after 4 of 8 lines: nothing written (pwm1 84) and no sample loop left running",
                   state() == ["1", "84"] and out.split()[0] == "0", f"{state()} {out}")
            fansim(on=False)

            # 5. Fixed read-only commands complete over the telnet shell (no hang, markers found).
            for label, c in (("HEARTBEAT_CMD", dev.HEARTBEAT_CMD), ("LOAD_SAMPLE_CMD", dev.LOAD_SAMPLE_CMD),
                             ("RETURN_PROBE", dev.RETURN_PROBE), ("RETURN_POLL", dev.RETURN_POLL),
                             ("BATT", dev.BATT), ("s2_procs_cmd", dev.s2_procs_cmd())):
                try:
                    o, rc = dev._send(c, 20)
                    r = "ok"
                except TimeoutError:
                    r = "hang"
                expect(f"{label} completes over the telnet shell", r == "ok", r)

        # 6. fixed commands over the real transport and the target busybox (fake sysfs, fake
        #    insmod/rmmod/kexec in /sbin, a /dev/null bind as the watchdog node).
        if which == "fixed":
            fixed_cmd_sim(dev, px, expect, name, work, fsim)
    finally:
        subprocess.run(["podman", "rm", "-f", "-t", "0", name],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(work, ignore_errors=True)
    print(f"PART {which} {total - fails}/{total}")
    return fails


PARTS = ("transport", "fixed")


def main():
    """the two parts run in parallel, each in its own container (own port)."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--initramfs", default=os.path.join(ROOT, "rootfs/out/mainline-init.cpio.gz"))
    ap.add_argument("--part", choices=PARTS, help=argparse.SUPPRESS)
    ap.add_argument("--work", help=argparse.SUPPRESS)
    ap.add_argument("--jobs", "-j", type=int, default=len(PARTS), help="parts at once (1 = serial)")
    a = ap.parse_args()
    if a.part:
        sys.exit(1 if run_part(a.part, a.initramfs, a.work or work_base()) else 0)
    t0 = time.monotonic()
    procs, outs, pending = {}, {}, list(PARTS)
    while pending or procs:
        while pending and len(procs) < max(1, a.jobs):
            part = pending.pop(0)
            procs[part] = (subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), "--part", part,
                                             "--initramfs", a.initramfs, "--work", work_base()],
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
    print(f"\n{passed}/{total} passed")
    print(f"({time.monotonic() - t0:.0f} s)", file=sys.stderr)
    sys.exit(0 if passed == total else 1)


FIXED_FAKES = {
    "insmod": '#!/bin/sh\necho "insmod $*" >> /tmp/fake.log\nexit 0\n',
    "rmmod": '#!/bin/sh\necho "rmmod $*" >> /tmp/fake.log\nexit 0\n',
    "kexec": ('#!/bin/sh\necho "kexec $* fd3=$(readlink /proc/$$/fd/3)" >> /tmp/fake.log\n'
              'case "$1" in -s) echo 1 > /sys/kernel/kexec_loaded;; -u) echo 0 > /sys/kernel/kexec_loaded;; esac\n'
              'exit 0\n'),
}
CF = "/sys/devices/system/cpu/cpufreq"


def cf_tree(rel4="4 5 6"):
    """Setup shell (never a device command): fake cpufreq policies as cpufreq-cap expects them."""
    s = [f"rm -rf {CF}"]
    for p, rel, cmax in ((0, "0 1 2 3", 1804800), (4, rel4, 2419200), (7, "7", 2841600)):
        d = f"{CF}/policy{p}"
        s += [f"mkdir -p {d}", f"echo '{rel}' > {d}/related_cpus", f"echo qcom-cpufreq-hw > {d}/scaling_driver",
              f"echo schedutil > {d}/scaling_governor", f"echo {cmax} > {d}/scaling_max_freq",
              f"echo {cmax} > {d}/cpuinfo_max_freq", f"echo {cmax} > {d}/cpuinfo_cur_freq",
              f"echo {cmax} > {d}/scaling_cur_freq"]
    return "; ".join(s)


def fixed_cmd_sim(dev, px, expect, name, work, fsim):
    import hashlib
    import tarfile

    def quiet(fn, *a):
        buf, real = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            r = fn(*a)
        except SystemExit as e:
            r = e.code
        finally:
            sys.stdout = real
        return r, buf.getvalue()

    def cat(path):
        return px(f"cat {path} 2>&1")[0].strip()

    # cpufreq-cap: cap, uncap, precheck failure (no write), status.
    px(cf_tree())
    r, out = quiet(dev.cpufreq_cap, "cap")
    expect("cpufreq-cap cap over telnet: exit 0; policy4 1478400, policy7 1075200, policy0 untouched",
           r == 0 and cat(f"{CF}/policy4/scaling_max_freq") == "1478400" and cat(f"{CF}/policy7/scaling_max_freq")
           == "1075200" and cat(f"{CF}/policy0/scaling_max_freq") == "1804800", out)
    r, out = quiet(dev.cpufreq_cap, "uncap")
    expect("cpufreq-cap uncap over telnet: exit 0; back to 2419200 / 2841600",
           r == 0 and cat(f"{CF}/policy4/scaling_max_freq") == "2419200"
           and cat(f"{CF}/policy7/scaling_max_freq") == "2841600", out)
    px(cf_tree(rel4="4 5"))
    r, out = quiet(dev.cpufreq_cap, "cap")
    expect("cpufreq-cap with policy4 related_cpus '4 5': exit 2, nothing written",
           r == 2 and cat(f"{CF}/policy4/scaling_max_freq") == "2419200"
           and cat(f"{CF}/policy7/scaling_max_freq") == "2841600", out)
    px(cf_tree() + f"; rm {CF}/policy7/scaling_max_freq; mkdir {CF}/policy7/scaling_max_freq")
    r, out = quiet(dev.cpufreq_cap, "cap")
    expect("cpufreq-cap with an unwritable policy7 attribute: exit 3 (readback mismatch)", r == 3, out)
    px(cf_tree())
    r, out = quiet(dev.cpufreq_cap, "status")
    expect("cpufreq-cap status completes, three policies", r == 0 and out.count("CF_POLICY") == 3, out)

    # Modules: insmod/rmmod via the fakes (PATH puts /sbin before /bin; checked first).
    o, _ = dev._send("command -v insmod; command -v rmmod", 10)
    path_ok = o.split() == ["/sbin/insmod", "/sbin/rmmod"]
    expect("telnet shell resolves insmod/rmmod to the test fakes", path_ok, o)
    if path_ok:
        rel = px("uname -r")[0].strip()
        host_mod = (px("head -n 1 /proc/modules")[0].split() or ["none_loaded"])[0]
        ko = "kernel/drivers/test/lvr_test.ko"
        px(f"mkdir -p /lib/modules/{rel}/kernel/drivers/test && echo x > /lib/modules/{rel}/{ko}; rm -f /tmp/fake.log")
        allow = os.path.join(work, "allow.txt")
        with open(allow, "w") as f:
            k = "sha256:" + hashlib.sha256(b"x\n").hexdigest()
            f.write(f"release {rel}\nlvrtest {ko} {k} p=1 q=a,b\nhostmod kernel/drivers/x/{host_mod}.ko {k}\n"
                    f"needy {ko} {k} loaded:{host_mod} bound:3da0000.iommu\nbadsha {ko} sha256:{'0' * 64}\n")
        r, out = quiet(dev.mod_op, "load", "lvrtest", allow)
        log = cat("/tmp/fake.log")
        expect("mod-load over telnet: one insmod of the listed file with the listed params, rc 0",
               r == 0 and log == f"insmod /lib/modules/{rel}/{ko} p=1 q=a,b" and "MOD_INSMOD lvr_test rc=0" in out,
               f"{r} {log!r} {out[-200:]!r}")
        n0 = len(cat("/tmp/fake.log").splitlines())
        r, out = quiet(dev.mod_op, "load", "badsha", allow)
        expect("mod-load with a .ko that does not match its sha256 pin: exit 14, no insmod",
               r == 14 and "MOD_SHA_BAD" in out and len(cat("/tmp/fake.log").splitlines()) == n0, out)
        r, out = quiet(dev.mod_op, "load", "needy", allow)
        expect("mod-load with an unmet bound: precondition: exit 13, no insmod",
               r == 13 and "MOD_NEED_MISSING bound:3da0000.iommu" in out
               and len(cat("/tmp/fake.log").splitlines()) == n0, out)
        r, out = quiet(dev.mod_op, "unload", "lvrtest", allow)
        expect("mod-unload of a module not in /proc/modules: exit 10, no rmmod",
               r == 10 and "rmmod" not in cat("/tmp/fake.log") and "MOD_NOT_LOADED" in out, out)
        if host_mod != "none_loaded":
            r, out = quiet(dev.mod_op, "unload", "hostmod", allow)
            expect("mod-unload of a loaded name: exactly `rmmod NAME` (fake), rc 0",
                   r == 0 and cat("/tmp/fake.log").splitlines()[-1] == f"rmmod {host_mod}", out)
        bad = f"release 6.18.55-00019-gce462cdadbad\nlvrtest {ko} sha256:{'0' * 64}\n"
        with open(allow, "w") as f:
            f.write(bad)
        n0 = len(cat("/tmp/fake.log").splitlines())
        r, out = quiet(dev.mod_op, "load", "lvrtest", allow)
        expect("mod-load with a release != uname -r: exit 10 on the device, no insmod",
               r == 10 and len(cat("/tmp/fake.log").splitlines()) == n0 and "MOD_REL_BAD" in out, out)

    # mod-fetch members + extract steps on local tarballs (the fetch/verify steps are s2-up's pattern).
    relm = "6.18.55-00019-gce462cdadbad"

    def mktar(path, extra=None):
        with tarfile.open(path, "w:gz", format=tarfile.GNU_FORMAT) as tf:
            for d in ("lib", "lib/modules", f"lib/modules/{relm}", f"lib/modules/{relm}/kernel"):
                ti = tarfile.TarInfo(d); ti.type = tarfile.DIRTYPE; ti.mode = 0o755; tf.addfile(ti)
            for n in (f"lib/modules/{relm}/modules.dep", f"lib/modules/{relm}/kernel/a.ko") + ((extra,) if extra else ()):
                data = b"x\n"
                ti = tarfile.TarInfo(n); ti.size = len(data); ti.mode = 0o644; tf.addfile(ti, io.BytesIO(data))
    steps = dict((lab, c) for lab, c, _ in dev.mod_fetch_steps(relm, "0" * 64, 8000))
    for label, extra, want_ok in (("good", None, True), ("member outside", "etc/evil", False),
                                  ("traversal", f"lib/modules/{relm}/../../../etc/evil", False)):
        tgz = os.path.join(work, "m.tar.gz")
        mktar(tgz, extra)
        px(f"rm -rf /lib/modules/{relm} /tmp/kx /etc/evil; mkdir -p /tmp/kx")
        subprocess.run(["podman", "cp", tgz, f"{name}:/tmp/kx/modules.tar.gz"], check=True)
        o1, rc1 = dev.run(steps["members"], 60)
        if want_ok:
            o2, rc2 = dev.run(steps["extract"], 60)
            expect("mod-fetch members+extract (good tarball): OUTSIDE=0, unpacked to /lib/modules/REL, tgz removed",
                   rc1 == 0 and "OUTSIDE=0" in o1 and rc2 == 0 and cat(f"/lib/modules/{relm}/modules.dep") == "x"
                   and "No such file" in cat("/tmp/kx/modules.tar.gz") and "No such file" in cat("/tmp/kx/x"),
                   f"{o1!r} {o2 if want_ok else ''!r}")
        else:
            expect(f"mod-fetch members ({label}): MEMBERS_BAD, non-zero, tarball removed, nothing unpacked",
                   rc1 != 0 and "MEMBERS_BAD" in o1 and "No such file" in cat("/tmp/kx/modules.tar.gz")
                   and "No such file" in cat(f"/lib/modules/{relm}/modules.dep") and "No such file" in cat("/etc/evil"),
                   o1)

    # kexec-self: load, exec (watchdog on fd 3 of kexec -e), refusals, unload.
    img, ird = b"IMAGE" * 1000, b"INITRD" * 500
    for fname, data in (("Image", img), ("initrd", ird)):
        p = os.path.join(work, fname)
        with open(p, "wb") as f:
            f.write(data)
        px("mkdir -p /tmp/kx")
        subprocess.run(["podman", "cp", p, f"{name}:/tmp/kx/{fname}"], check=True)
    isha, rsha = hashlib.sha256(img).hexdigest(), hashlib.sha256(ird).hexdigest()
    wd = "/sys/devices/platform/soc@0/17c10000.watchdog"
    px(fsim.tree([(1, "pwmfan", "pwm-fan", 84, 1, 4340)], ["45000", "41000"]) +
       f"; rm -f /tmp/fake.log /tmp/lvr-reset; echo 0 > /sys/kernel/kexec_loaded; mkdir -p {wd} "
       f"/sys/class/watchdog/watchdog0; ln -sfn {wd} /sys/class/watchdog/watchdog0/device; "
       f"touch /dev/watchdog0 && mount --bind /dev/null /dev/watchdog0")
    cmdline = "rdinit=/init clk_ignore_unused panic=0 log_buf_len=4M lvr_kexec=1"
    r, out = quiet(dev.kexec_self, "load", isha, "f" * 64, cmdline)
    expect("kexec-self load with a wrong initrd sha: refused on the device, kexec never run",
           r == 2 and "KX_SHA_BAD" in out and "No such file" in cat("/tmp/fake.log"), out)
    r, out = quiet(dev.kexec_self, "load", isha, rsha, cmdline)
    expect("kexec-self load: kexec -s -l with the pinned files and the exact cmdline; loaded=1",
           r == 0 and cat("/tmp/fake.log").startswith(
               f"kexec -s -l /tmp/kx/Image --initrd=/tmp/kx/initrd --command-line={cmdline} fd3=")
           and cat("/sys/kernel/kexec_loaded") == "1", f"{out!r} {cat('/tmp/fake.log')!r}")
    r, out = quiet(dev.kexec_self, "load", isha, rsha, cmdline)
    expect("kexec-self load twice: KX_ALREADY_LOADED, exit 2", r == 2 and "KX_ALREADY_LOADED" in out, out)
    r, out = quiet(dev.kexec_self, "status")
    expect("kexec-self status completes", r == 0 and "KX_STATUS" in out and "loaded=1" in out, out)
    for setup, undo, why in (("echo 70000 > /sys/class/thermal/thermal_zone1/temp",
                              "echo 41000 > /sys/class/thermal/thermal_zone1/temp", "tmax=70000"),
                             ("touch /tmp/lvr-reset", "rm -f /tmp/lvr-reset", "reset-armed"),
                             ("rm /sys/class/watchdog/watchdog0/device", f"ln -s {wd} /sys/class/watchdog/watchdog0/device",
                              "no-wdt"),
                             ("echo 0 > /sys/kernel/kexec_loaded", "echo 1 > /sys/kernel/kexec_loaded", "not-loaded"),
                             ("for z in /sys/class/thermal/thermal_zone*/temp; do echo x > $z; done",
                              "echo 45000 > /sys/class/thermal/thermal_zone0/temp; "
                              "echo 41000 > /sys/class/thermal/thermal_zone1/temp", "tmax=0")):
        px(setup)
        r, out = quiet(dev.kexec_self, "exec")
        time.sleep(3)
        expect(f"kexec-self exec refused ({why}): exit 2, no kexec -e, watchdog not opened",
               r == 2 and f"KX_REFUSED {why}" in out and "-e" not in cat("/tmp/fake.log"), f"{out!r} {cat('/tmp/fake.log')!r}")
        px(undo)
    r, out = quiet(dev.kexec_self, "exec")
    time.sleep(4)
    log = cat("/tmp/fake.log").splitlines()
    expect("kexec-self exec: KX_ARMED; ~2 s later kexec -e runs with the watchdog node open on fd 3",
           r == 0 and "KX_ARMED wdt=/dev/watchdog0" in out and log[-1].startswith("kexec -e fd3=")
           and log[-1] != "kexec -e fd3=", f"{out!r} {log[-1:]!r}")
    r, out = quiet(dev.kexec_self, "unload")
    expect("kexec-self unload: kexec -u, loaded=0", r == 0 and cat("/sys/kernel/kexec_loaded") == "0"
           and cat("/tmp/fake.log").splitlines()[-1].startswith("kexec -u"), out)
    # the diagnostic cmdline (watchdog.open_timeout=90, panic=10, initcall_debug, ignore_loglevel)
    # reaches kexec -s -l verbatim through the real shell; a watchdog variant is refused before sending.
    diag = ("rdinit=/init clk_ignore_unused pd_ignore_unused regulator_ignore_unused printk.devkmsg=on panic=10 "
           "log_buf_len=4M msm.separate_gpu_kms=1 watchdog.open_timeout=90 initcall_debug ignore_loglevel lvr_kexec=1")
    r, out = quiet(dev.kexec_self, "load", isha, rsha, diag)
    expect("kexec-self load with the diagnostic cmdline: kexec -s -l gets it verbatim; loaded=1",
           r == 0 and cat("/tmp/fake.log").splitlines()[-1].startswith(
               f"kexec -s -l /tmp/kx/Image --initrd=/tmp/kx/initrd --command-line={diag} fd3=")
           and cat("/sys/kernel/kexec_loaded") == "1", f"{out!r} {cat('/tmp/fake.log')!r}")
    r, out = quiet(dev.kexec_self, "unload")
    n_log = len(cat("/tmp/fake.log").splitlines())
    r2, out2 = quiet(dev.kexec_self, "load", isha, rsha, diag.replace("open_timeout=90", "open_timeout=0"))
    expect("kexec-self load with watchdog.open_timeout=0: refused on the host, kexec never run",
           r == 0 and isinstance(r2, str) and len(cat("/tmp/fake.log").splitlines()) == n_log
           and cat("/sys/kernel/kexec_loaded") == "0", f"{r2!r} {cat('/tmp/fake.log')!r}")
    # The same cmdline with reboot=warm is refused on the host, as is reboot=cold; kexec never runs.
    # The diagnostic cmdline above still loads.
    rebootwarm = diag.replace(" lvr_kexec=1", " reboot=warm lvr_kexec=1")
    for c, what in ((rebootwarm, "the cmdline with reboot=warm"), (rebootwarm.replace("reboot=warm", "reboot=cold"), "reboot=cold")):
        n_log = len(cat("/tmp/fake.log").splitlines())
        r2, out2 = quiet(dev.kexec_self, "load", isha, rsha, c)
        expect(f"kexec-self load with {what}: refused on the host, kexec never run",
               isinstance(r2, str) and "not allowed" in r2 and len(cat("/tmp/fake.log").splitlines()) == n_log
               and cat("/sys/kernel/kexec_loaded") == "0", f"{r2!r} {cat('/tmp/fake.log')!r}")
    px("umount /dev/watchdog0; rm -f /dev/watchdog0")


REAL_CONNECT = socket.create_connection
REAL_SOCK_CONNECT = socket.socket.connect

if __name__ == "__main__":
    main()
