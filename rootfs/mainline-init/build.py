#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Pack the mainline init (init.sh), a static ARM64 BusyBox, a static kexec and optional Adreno
firmware into a reproducible gzip newc initramfs.

    python3 -I rootfs/mainline-init/build.py [--busybox PATH] [--kexec PATH]
        [--firmware-dir DIR] [--busybox-sha256 HEX] [--kexec-sha256 HEX] [--output PATH]

All inputs are explicit. BusyBox and kexec default to what build-static-tools.sh writes to
rootfs/out/static/ (BusyBox 1.36.1, kexec-tools 2.0.32); both must be static little-endian ARM64 ELF.
The optional sha256 flags pin them; the sha256 of every input is printed either way. Same inputs give
the same bytes: fixed inode numbers, uid/gid 0, mtime 0, gzip mtime 0.

Applet links are made here, not at boot: one `bin/<applet> -> busybox` symlink for every name in
busybox-applets.txt (the `busybox --list` of the BusyBox that build-static-tools.sh builds) except
STRIPPED. The shell has no standalone applet lookup, so a stripped name is "not found" from the telnet
shell. That is a speed bump (`/bin/busybox reboot` still works); tools/dev's denylist is the real guard.
The init never runs reboot: the return to Android goes through /proc/lvr_warm_reset or a watchdog bite.

Also in the archive: /sbin/kexec (run only by `tools/dev kexec-self`), insmod/rmmod links (modprobe stays
stripped, so nothing autoloads), and, with --firmware-dir, Adreno 650 firmware at the /lib/firmware paths
the kernel requests: a650_sqe.fw and a650_gmu.bin (linux-firmware or your own device) and a650_zap.elf
(signed for the device, so it has to come from your own headset). It is read from RAM at the first
render-node open after the GPU modules load. Without --firmware-dir no firmware is added, and no other
firmware ever is. An archive built with firmware must not be redistributed.
"""
import argparse
import gzip
import hashlib
import stat
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BUILD_HINT = " (build it with rootfs/mainline-init/build-static-tools.sh, or pass --busybox/--kexec)"
STATIC_DIR = ROOT / "rootfs" / "out" / "static"
DEFAULT_BUSYBOX = STATIC_DIR / "busybox"
DEFAULT_KEXEC = STATIC_DIR / "kexec"
# (path in the archive, source file name in --firmware-dir)
FIRMWARE = (
    ("lib/firmware/qcom/a650_sqe.fw", "a650_sqe.fw"),
    ("lib/firmware/qcom/a650_gmu.bin", "a650_gmu.bin"),
    ("lib/firmware/qcom/sm8250/oculus/hollywood/a650_zap.mbn", "a650_zap.elf"),
)
DEFAULT_OUTPUT = ROOT / "rootfs" / "out" / "mainline-init.cpio.gz"
MAX_SIZE = 0x200000  # payload staging layout of the boot vehicle: initrd at 0x93000000, <= 2 MiB
APPLETS = HERE / "busybox-applets.txt"
# Applets with no link in the initramfs: power state (PMIC PS_HOLD / shutdown), raw block and
# flash writers, /dev/mem, the userspace watchdog daemon (it would keep pinging and defeat the
# init's reset paths), RTC writers and suspend, I2C writers, and modprobe: with it gone, the
# kernel's request_module() (CONFIG_MODPROBE_PATH /sbin/modprobe) finds nothing, so no module
# ever autoloads. insmod and rmmod are linked (fixed `tools/dev mod-load`/`mod-unload` commands
# with an allowlist file; the denylist still refuses them in free-form commands).
STRIPPED = frozenset("""
    reboot poweroff halt
    dd devmem shred
    mkfs.ext2 mke2fs mkfs.vfat mkdosfs mkfs.minix mkswap fdisk fdformat blkdiscard hdparm
    fsfreeze freeramdisk nandwrite ubiattach ubidetach ubimkvol ubirmvol ubirsvol
    ubiupdatevol ubirename
    watchdog hwclock rtcwake i2cset i2ctransfer
    modprobe
""".split())


def archive(entries):
    data = bytearray()
    for ino, (name, mode, contents, major, minor) in enumerate(entries, 1):
        name = name.encode() + b"\0"
        fields = (ino, mode, 0, 0, 1, 0, len(contents), 0, 0, major, minor, len(name), 0)
        data += b"070701" + "".join(f"{x:08x}" for x in fields).encode() + name
        data += b"\0" * (-len(data) % 4)
        data += contents
        data += b"\0" * (-len(data) % 4)
    data += b"\0" * (-len(data) % 512)
    return bytes(data)


def read_input(path, want=None, what="input", hint=""):
    """File bytes; want = optional pinned sha256; hint = what to do if the file is missing."""
    if not path.is_file():
        raise SystemExit(f"{path}: {what} not found{hint}")
    b = path.read_bytes()
    digest = hashlib.sha256(b).hexdigest()
    if want is not None and digest != want.lower():
        raise SystemExit(f"{path}: sha256 {digest} != pinned {want}")
    print(f"  {what}: {path} sha256 {digest}", file=sys.stderr)
    return b


def check_static_arm64(path, b):
    if b[:6] != b"\x7fELF\x02\x01" or struct.unpack_from("<H", b, 18)[0] != 183:
        raise SystemExit(f"{path}: not a little-endian ARM64 ELF")
    off = struct.unpack_from("<Q", b, 32)[0]
    size, count = struct.unpack_from("<HH", b, 54)
    if any(struct.unpack_from("<I", b, off + i * size)[0] == 3 for i in range(count)):
        raise SystemExit(f"{path}: must be statically linked")
    return b


def firmware_entries(fwdir):
    """Directory entries (parents first) and 0644 file entries for the FIRMWARE files found in fwdir."""
    present = [(dest, src) for dest, src in FIRMWARE if (fwdir / src).is_file()]
    if not present:
        raise SystemExit(f"{fwdir}: none of {', '.join(src for _, src in FIRMWARE)} found")
    for dest, src in FIRMWARE:
        if (dest, src) not in present:
            print(f"warning: {fwdir / src} not found; the GPU will not probe without it", file=sys.stderr)
    dirs, files = [], []
    for dest, src in present:
        parts = dest.split("/")
        for i in range(1, len(parts)):
            d = "/".join(parts[:i])
            if d not in dirs:
                dirs.append(d)
        files.append((dest, stat.S_IFREG | 0o644, read_input(fwdir / src, what=f"firmware {src}"), 0, 0))
    return [(d, stat.S_IFDIR | 0o755, b"", 0, 0) for d in dirs] + files


def applet_links():
    """Sorted applet names to link: busybox-applets.txt minus STRIPPED."""
    names = APPLETS.read_text().split()
    bad = [n for n in names if not all(c.isalnum() or c in "_.[-" for c in n)]
    if bad or len(set(names)) != len(names):
        raise SystemExit(f"{APPLETS}: bad or duplicate applet names {bad[:3]}")
    unknown = sorted(STRIPPED - set(names))
    if unknown:
        raise SystemExit(f"STRIPPED names that are not applets: {unknown}")
    return sorted(set(names) - STRIPPED)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--busybox", type=Path, default=DEFAULT_BUSYBOX,
                    help="static aarch64 BusyBox (default: %(default)s, from build-static-tools.sh)")
    ap.add_argument("--kexec", type=Path, default=DEFAULT_KEXEC,
                    help="static aarch64 kexec (default: %(default)s, from build-static-tools.sh)")
    ap.add_argument("--firmware-dir", type=Path, default=None,
                    help="optional directory with a650_sqe.fw, a650_gmu.bin, a650_zap.elf (default: none, no firmware)")
    ap.add_argument("--busybox-sha256", help="refuse unless the BusyBox has this sha256")
    ap.add_argument("--kexec-sha256", help="refuse unless the kexec has this sha256")
    ap.add_argument("--init", type=Path, default=HERE / "init.sh")
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    a = ap.parse_args()

    init = a.init.read_bytes()
    if not init.startswith(b"#!/bin/busybox sh\n"):
        raise SystemExit(f"{a.init}: must start with #!/bin/busybox sh")
    busybox = check_static_arm64(a.busybox, read_input(a.busybox, a.busybox_sha256, "busybox", BUILD_HINT))
    kexec = check_static_arm64(a.kexec, read_input(a.kexec, a.kexec_sha256, "kexec", BUILD_HINT))
    dirs = ["bin", "sbin", "dev", "dev/pts", "proc", "sys", "config", "etc", "root", "tmp"]
    entries = [(d, stat.S_IFDIR | 0o755, b"", 0, 0) for d in dirs]
    entries += [("bin/busybox", stat.S_IFREG | 0o755, busybox, 0, 0)]
    entries += [(f"bin/{n}", stat.S_IFLNK | 0o777, b"busybox", 0, 0) for n in applet_links()]
    entries += [("sbin/kexec", stat.S_IFREG | 0o755, kexec, 0, 0)]
    if a.firmware_dir is not None:
        entries += firmware_entries(a.firmware_dir)
    entries += [
        ("init", stat.S_IFREG | 0o755, init, 0, 0),
        ("dev/console", stat.S_IFCHR | 0o600, b"", 5, 1),
        ("dev/kmsg", stat.S_IFCHR | 0o600, b"", 1, 11),
        ("dev/null", stat.S_IFCHR | 0o666, b"", 1, 3),
        ("etc/passwd", stat.S_IFREG | 0o644, b"root::0:0:root:/root:/bin/sh\n", 0, 0),
        ("etc/group", stat.S_IFREG | 0o644, b"root:x:0:\n", 0, 0),
        ("TRAILER!!!", 0, b"", 0, 0),
    ]
    data = gzip.compress(archive(entries), compresslevel=9, mtime=0)
    if len(data) > MAX_SIZE:
        raise SystemExit(f"initramfs is {len(data)} bytes; the staging layout allows {MAX_SIZE}")
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    a.output.with_name(a.output.name + ".sha256").write_text(f"{digest}  {a.output.name}\n")
    print(f"{a.output}: {len(data)} bytes, sha256 {digest}, {len(entries) - 1} entries")


if __name__ == "__main__":
    main()
