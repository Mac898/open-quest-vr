# rootfs/

Two things are built here, both rootless (podman with an emulated arm64 container via qemu
binfmt) and both GPL-2.0-only:

1. the **initramfs** (`mainline-init/`): the RAM-only root the mainline kernel boots into, with a
   USB-Ethernet gadget, a telnet shell and self-recovery to the watchdog;
2. the **stage-2 rootfs** (`build.sh`): an Alpine (stable, pinned branch) aarch64 userland (Python, strace, tcpdump,
   Mesa Turnip, Vulkan tools, ...) that `tools/dev s2-up` unpacks into a tmpfs and chroots into.

Requirements: `podman`, `qemu-user-static`/`qemu-user-binfmt` with an aarch64 binfmt handler,
Python 3, network access to the Alpine mirror, kernel.org and busybox.net. `host/host-setup.sh`
installs and checks the host side. The boot vehicle that loads the kernel and initramfs is a
separate project: <https://github.com/Mac898/quest-kexec> (GPL-2.0, a fork of
<https://github.com/YusufOzmen01/quest-kexec>); `mainline-init/init.sh` is derived from its
initramfs init script.

## Initramfs

```
rootfs/mainline-init/build-static-tools.sh     # static BusyBox + kexec -> rootfs/out/static/
python3 -I rootfs/mainline-init/build.py       # -> rootfs/out/mainline-init.cpio.gz (+ .sha256)
python3 -I rootfs/mainline-init/test_init.py   # offline test of init.sh in the built archive
```

`build-static-tools.sh [OUTDIR]` downloads the upstream tarballs (sha256-checked) and builds, in an
arm64 Alpine container, **BusyBox 1.36.1** (`defconfig` + static, `busybox.config.fragment`; its
`--list` is exactly `busybox-applets.txt`, 401 applets) and **kexec-tools 2.0.32** (static). Output:
`busybox`, `kexec`, `busybox.applets`, `busybox.config`, `build-info.txt` (versions and sha256).
Env: `BUILD_IMAGE` (default `docker.io/library/alpine:3.24`), `BUILD_JOBS`; the apk repositories are pinned to that release's `vX.Y` branch (`ALPINE_BRANCH` overrides). It takes several minutes
under qemu. The binaries embed a build date, so their hashes differ between builds; `build-info.txt`
records them.

`build.py` packs `init.sh`, BusyBox, kexec and the applet symlinks into a deterministic gzip newc
archive (same inputs, same bytes; at most 2 MiB, the payload staging limit). All inputs are explicit:

| Option | Default |
|---|---|
| `--busybox PATH` | `rootfs/out/static/busybox` |
| `--kexec PATH` | `rootfs/out/static/kexec` |
| `--firmware-dir DIR` | none (no firmware) |
| `--busybox-sha256 HEX`, `--kexec-sha256 HEX` | no pin; the sha256 of every input is printed |
| `--output PATH` | `rootfs/out/mainline-init.cpio.gz` |

Applets that could write hardware state (`reboot`, `dd`, `devmem`, `watchdog`, `i2cset`, `modprobe`,
mkfs/fdisk, ...) get no symlink; `insmod`/`rmmod` stay for `tools/dev mod-load`. This is a speed bump,
`tools/dev`'s denylist is the real guard.

**Adreno firmware is optional and never part of this repository.** With `--firmware-dir DIR` the
archive also carries `a650_sqe.fw` and `a650_gmu.bin` (from your own device, or from the
linux-firmware package) under `/lib/firmware/qcom/`, and `a650_zap.elf` as
`/lib/firmware/qcom/sm8250/oculus/hollywood/a650_zap.mbn`. The zap shader is signed for the device
and has to come from your own headset. Without the files the GPU modules cannot bring the GPU up.
An archive built with firmware contains device firmware and is not redistributable.

`test_init.py` runs the archive's own `init` and BusyBox (via qemu-user) as an ordinary process over
a fake root and checks the reset paths, dead-man heartbeat, LED code, return-to-Android request,
applet links and build reproducibility. Options: `--initramfs PATH`, `--scale S`, `--jobs N`,
`--full` (real-time, serial), `--only NAME`, `--keep`. About a minute on a quiet machine; the LED-code
scenarios are timing-sensitive and can fail on a heavily loaded host.

## Stage-2 rootfs

```
rootfs/build.sh [NAME]      # -> rootfs/out/NAME.tar.gz (+ .sha256, .manifest.txt), stage2-latest.tar.gz link
```

`build.sh` runs `build-in-container.sh` in an arm64 `alpine:3.23` container (`STAGE2_IMAGE`/`BUILD_IMAGE`, `ALPINE_BRANCH` and
`ALPINE_MIRROR` override the image, repository branch and mirror; 3.24 fails the lavapipe self-test under qemu): installs `packages.txt` with `apk --root`, removes
tools that can write hardware state (mkfs, partitioning, flash, `i2cset`, `setpci`, watchdog, `fbset`,
`vkcube`, ... see `STRIP`), builds the headless Vulkan compute check `/usr/local/bin/vk-compute-smoke`
(`vkcompute/`, tested on lavapipe in the container), and writes a manifest with the package versions.
The tarball is `.tar.gz` because the target's BusyBox 1.36.1 `tar` has no zstd support. Serve it with
`tools/serve-usb.py` and fetch it with `tools/dev s2-up`.
