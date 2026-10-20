#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Offline end-to-end test of `tools/dev s2-up / s2 / s2-down`.

Never contacts the headset. It stands up a stand-in for the device in a rootless
arm64 podman container (CAP_SYS_ADMIN, not --privileged) whose root holds only the
*target's own* static busybox (pulled out of the built initramfs), starts tools/serve-usb.py on
10.42.0.1 (host side of the USB link, needs the link address present on the host),
and swaps tools/dev's raw telnet transport (`_send`) for `podman exec`. The exact command strings
tools/dev would send to the device then run under the same busybox applets.

Checks: s2-up refuses with debugfs mounted (precheck) and stops with BINDFAIL debugfs if
the /sys rbind carried one; fetch + sha256 + extract + binds; commands run inside the chroot; s2-down
refuses while a chroot process lives; teardown leaves /proc, /dev and /tmp intact
(a /tmp canary survives) and /stage2 is gone.
Run: python3 -I tools/test_stage2_sim.py [--initramfs PATH] [--port 8765]
"""
import argparse
import gzip
import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_INITRAMFS = os.path.join(ROOT, "rootfs", "out", "mainline-init.cpio.gz")


def load_dev():
    loader = importlib.machinery.SourceFileLoader("dev", os.path.join(HERE, "dev"))
    spec = importlib.util.spec_from_loader("dev", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def cpio_member(data, want):
    """Return file contents of `want` from a newc cpio archive."""
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


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--initramfs", default=DEFAULT_INITRAMFS)
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()

    dev = load_dev()
    work = tempfile.mkdtemp(prefix="s2sim-")
    sim = os.path.join(work, "root")
    for d in ("bin", "proc", "sys", "dev", "tmp", "etc"):
        os.makedirs(os.path.join(sim, d))
    bb = os.path.join(sim, "bin", "busybox")
    with open(bb, "wb") as f:
        f.write(cpio_member(gzip.open(a.initramfs).read(), "bin/busybox"))
    os.chmod(bb, 0o755)

    name = f"s2sim-{os.getpid()}"
    subprocess.run(["podman", "run", "-d", "--rm", "--name", name,
                    # Not --privileged: that would expose host block devices in /dev.
                    "--cap-add", "SYS_ADMIN", "--security-opt", "seccomp=unconfined",
                    "--security-opt", "apparmor=unconfined",
                    "--network", "host", "--arch", "arm64", "--rootfs", sim,
                    "/bin/busybox", "sh", "-c",
                    "/bin/busybox --install -s /bin; exec sleep 900"],
                   check=True, stdout=subprocess.DEVNULL)
    srv = subprocess.Popen([sys.executable, "-I", os.path.join(HERE, "serve-usb.py"),
                            "--port", str(a.port)], stdout=subprocess.PIPE, text=True)
    sent = []

    def fake_run(cmd, timeout=30.0):
        sent.append(cmd)
        p = subprocess.run(["podman", "exec", name, "/bin/sh", "-c", cmd],
                           capture_output=True, text=True, timeout=timeout)
        return p.stdout + p.stderr, p.returncode

    # Swap only the raw transport: dev.run() still denylist-checks every command.
    # fake_run() itself is used for probes of the stand-in that the device never sees.
    dev._send = fake_run
    fails = 0

    def expect(label, cond, detail=""):
        nonlocal fails
        fails += not cond
        print(f"{'ok  ' if cond else 'FAIL'} {label}" + (f"  [{detail.strip()[:300]}]" if detail and not cond else ""))

    link = True
    try:
        print(srv.stdout.readline().rstrip())
        time.sleep(0.5)
        out, _ = fake_run("busybox | head -1; echo canary > /tmp/canary")
        expect("stand-in runs target busybox 1.36.1", "v1.36.1" in out, out)
        out, _ = fake_run("ls /dev; grep -cE ' /dev/(sd|nvme|mmcblk|loop|dm-)' /proc/self/mountinfo")
        expect("no host block devices visible in the stand-in",
               not any(x in out.split() for x in ("sda", "nvme0n1", "loop0")), out)

        # The container's /sys is the host's sysfs tree, which carries the host's debugfs (masked
        # by podman, still listed in /proc/mounts), so s2-up's debugfs precheck would rightly
        # refuse. Replace it with a tmpfs stand-in /sys (s2 only needs something to rbind).
        out, rc = fake_run("umount -l /sys && mount -t tmpfs -o mode=0755 simsys /sys && "
                           "mkdir -p /sys/kernel/debug /sys/class && grep -c debugfs /proc/mounts")
        expect("stand-in /sys without debugfs", out.strip() == "0", out)
        # a stand-in "debugfs" (a tmpfs at /sys/kernel/debug: real debugfs
        # can't be mounted in a user namespace; the guards match the mount point too).
        stub = "mkdir -p /sys/kernel/debug && mount -t tmpfs dbgstub /sys/kernel/debug"
        print("--- s2-up with debugfs mounted must refuse at the precheck")
        out, rc = fake_run(stub + " && grep -c ' /sys/kernel/debug ' /proc/mounts")
        expect("debugfs stand-in mounted", rc == 0 and out.strip() == "1", out)
        try:
            dev.s2_up("stage2-latest.tar.gz", a.port)
            expect("s2-up refused (debugfs mounted)", False)
        except SystemExit as e:
            expect("s2-up refused (debugfs mounted)", "debugfs" in str(e), str(e))
        out, _ = fake_run("[ -e /stage2 ] && echo PRESENT || echo ABSENT")
        expect("nothing mounted after the precheck refusal", out.strip() == "ABSENT", out)
        fake_run("umount /sys/kernel/debug")

        # Steps run one by one (no fetch: works without the USB link); the bind step binds onto
        # the empty tmpfs exactly as in s2-up.
        steps = dict((lab, (c, tmo)) for lab, c, tmo in dev.s2_up_steps("x.tar.gz", a.port, "0" * 64))
        # the clean bind path must exit 0 (the guard's `grep -q ... && echo` once returned
        # grep's 1 when nothing was found).
        print("--- bind step without debugfs: rc 0, no BINDFAIL (runs without the USB link)")
        out, rc = dev.run(*steps["tmpfs"])
        expect("tmpfs step", rc == 0, out)
        out, rc = dev.run(*steps["bind"])
        expect("clean bind step exits 0", rc == 0, f"rc={rc} {out}")
        expect("clean bind step prints no BINDFAIL", "BINDFAIL" not in out, out)
        out, _ = fake_run("grep -c ' /stage2/' /proc/mounts; grep -c ' /stage2/sys/kernel/debug ' /proc/mounts")
        expect("binds present, nothing at /stage2/sys/kernel/debug",
               out.split()[:1] != ["0"] and out.split()[-1:] == ["0"], out)
        try:
            dev.s2_down()
            expect("s2-down after the clean bind step", True)
        except SystemExit as e:
            expect("s2-down after the clean bind step", False, str(e))
        out, _ = fake_run("grep -c stage2 /proc/mounts; [ -e /stage2 ] && echo PRESENT || echo ABSENT")
        expect("/stage2 gone after the clean bind step", out.split() == ["0", "ABSENT"], out)

        print("--- debugfs appearing before the bind step: BINDFAIL debugfs, s2-down cleans up")
        out, rc = dev.run(*steps["tmpfs"])
        expect("tmpfs step", rc == 0, out)
        fake_run(stub)
        out, rc = dev.run(*steps["bind"])
        expect("bind step reports BINDFAIL debugfs", "BINDFAIL debugfs" in out and
               out.count("BINDFAIL") == 1, out)
        out, _ = fake_run("grep -c ' /stage2/sys/kernel/debug ' /proc/mounts")
        expect("the copy was carried (guard had something to catch)", out.strip() == "1", out)
        try:
            dev.s2_down()
            expect("s2-down after BINDFAIL debugfs", True)
        except SystemExit as e:
            expect("s2-down after BINDFAIL debugfs", False, str(e))
        out, _ = fake_run("grep -c ' /sys/kernel/debug ' /proc/mounts; grep -c stage2 /proc/mounts; "
                          "[ -e /stage2 ] && echo PRESENT || echo ABSENT")
        expect("init-namespace mount untouched, /stage2 gone", out.split() == ["1", "0", "ABSENT"], out)
        fake_run("umount /sys/kernel/debug")
        if srv.poll() is not None:
            link = False
            raise StopIteration("no USB link")

        print("--- s2-up")
        try:
            dev.s2_up("stage2-latest.tar.gz", a.port)
            expect("s2-up completed", True)
        except SystemExit as e:
            expect("s2-up completed", False, str(e))
        out, rc = dev.run(dev.s2_wrap("cat /etc/alpine-release; python3 -c 'print(6+36)'; "
                                       "strace -V | head -1; ls /dev/null /tmp/canary"))
        expect("s2 runs inside the chroot", rc == 0 and "42" in out and "/tmp/canary" in out, out)
        out, rc = fake_run(dev.s2_wrap("which i2cset setpci mkfs fdisk watchdog"))
        expect("stripped tools absent", "i2cset" not in out and "mkfs" not in out, out)
        out, _ = fake_run("[ -e /stage2/.stage2.tar.gz ] && echo PRESENT || echo ABSENT")
        expect("tarball deleted after extract", out.strip() == "ABSENT", out)
        out, _ = fake_run("grep ' /stage2' /proc/mounts")
        expect("dev/pts reached via rbind", " /stage2/dev/pts " in out, out)

        print("--- s2-up again must refuse")
        try:
            dev.s2_up("stage2-latest.tar.gz", a.port)
            expect("second s2-up refused", False)
        except SystemExit as e:
            expect("second s2-up refused", "already exists" in str(e), str(e))

        print("--- s2-down with a live chroot process must refuse")
        dev.run(dev.s2_wrap("setsid sleep 300 </dev/null >/dev/null 2>&1 &"))
        try:
            dev.s2_down()
            expect("s2-down refused while busy", False)
        except SystemExit as e:
            expect("s2-down refused while busy", "still run inside" in str(e), str(e))
        out, _ = dev.run(dev.s2_procs_cmd())
        pids = [ln.split()[0] for ln in out.splitlines() if ln.strip()]
        expect("chroot process detected", len(pids) == 1, out)
        dev.run("kill " + " ".join(pids))
        time.sleep(1)

        print("--- s2-down")
        try:
            dev.s2_down()
            expect("s2-down completed", True)
        except SystemExit as e:
            expect("s2-down completed", False, str(e))
        out, _ = fake_run("[ -e /stage2 ] && echo PRESENT; grep -c stage2 /proc/mounts; "
                          "cat /tmp/canary; ls /dev/null; head -1 /proc/version | cut -c1-5")
        expect("/stage2 gone, no stage2 mounts",
               "PRESENT" not in out and out.splitlines()[0].strip() == "0", out)
        expect("/tmp, /dev, /proc intact", "canary" in out and "/dev/null" in out and "Linux" in out, out)
    except StopIteration:
        print("--- full s2-up/s2/s2-down flow SKIPPED: serve-usb.py needs the USB link (host 10.42.0.1)")
    finally:
        srv.terminate()
        subprocess.run(["podman", "rm", "-f", "-t", "0", name],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(os.path.join(work, "sent.txt"), "w") as f:
            f.write("\n".join(sent) + "\n")
    verdict = f"{fails} FAILED" if fails else ("PASS" if link else "PASS (partial; full flow SKIPPED, exit 2)")
    print(f"\ncommands sent: {work}/sent.txt\n{verdict}")
    sys.exit(1 if fails else (0 if link else 2))


if __name__ == "__main__":
    main()
