# Root filesystems: the initramfs and the stage-2 rootfs

The headset runs entirely from RAM. There are two layers:

| Layer | What it is | Size | Built by |
|---|---|---|---|
| **initramfs** (`mainline-init`) | A tiny gzip newc cpio: BusyBox, a static `kexec`, the init script, optional firmware. Gets the USB link and a shell up and keeps the device recoverable | at most 2 MiB (the loader's staging limit) | `rootfs/mainline-init/build-static-tools.sh` then `build.py` |
| **stage-2 rootfs** | A full Alpine Linux (edge, aarch64) userland: Python, debugging tools, optionally Mesa/Turnip, pulled over USB into a tmpfs and entered with `chroot` | about 26 MB compressed, about 83 MB unpacked | `rootfs/build.sh` (`build-in-container.sh` inside the container) |

Nothing is written to device storage. Both builds run **rootless** (podman and qemu user-mode emulation for
arm64); no host `sudo` is needed after a one-time host setup (`host/host-setup.sh`).

## The initramfs

### Contents

- `/init` (`mainline-init/init.sh`): the only program that runs as PID 1. It is a BusyBox `ash` script.
- `/bin/busybox` plus one symlink per applet. The applet list is `busybox-applets.txt`; a few applets are
  **left out on purpose** because they could change power state or write storage or hardware: `reboot`,
  `poweroff`, `halt`, `dd`, `devmem`, `shred`, `mkfs.*`, `mke2fs`, `mkswap`, `fdisk`, `blkdiscard`, `hdparm`,
  `fsfreeze`, flash and UBI writers, `watchdog`, `hwclock`, `rtcwake`, `i2cset`, `i2ctransfer` and `modprobe`.
  (`modprobe` is omitted so the kernel's module auto-loading finds nothing and nothing loads by itself; `insmod`
  and `rmmod` are present.) This is a speed bump, not a barrier: `/bin/busybox <applet>` for an omitted
  applet may still work, so the host tools' deny-list is the real guard (see [safety.md](safety.md)).
- `/sbin/kexec`: a static kexec-tools (2.0.32), used only for Linux-to-Linux kernel reloads
  (see [booting.md](booting.md)).
- Optional firmware under `/lib/firmware` (GPU only; see below).
- Minimal `/etc/passwd` (root, no password), device nodes `console`, `kmsg`, `null`.

### What the init does

1. Mounts proc, sysfs, devtmpfs, devpts, configfs and tmpfs, and takes over the SoC watchdog (open, magic
   close), so a boot that stalled before this point has already been reset by the watchdog's open-timeout.
2. Starts the front-LED stage worker and the dead-man timer (resets the device if the host stops sending
   heartbeats, default 600 s).
3. Creates the USB ECM gadget through configfs (VID:PID `1d6b:0105`, fixed MACs), waits up to 60 s for a UDC,
   brings up `usb0` at `10.42.0.2`, runs a DHCP server (single lease, `10.42.0.1`) and an **unauthenticated
   telnet root shell** on port 23.
4. If no UDC appears in 60 s, or the host does not configure the gadget within 120 s, plays the LED code
   ([led-diag-code.md](led-diag-code.md)) and resets through the watchdog.
5. Handles return-to-Android requests by writing to `/proc/lvr_warm_reset` ([booting.md](booting.md)).

`mainline-init/test_init.py` runs the script as an ordinary process against a fake root (a marker file is
required; the script refuses to run as anything but PID 1 or a marked test root), so the init's reset
logic, timers and LED code can be tested on a host without a device.

### Building it

1. Build the static tools (BusyBox 1.36.1 and kexec-tools 2.0.32, both from the upstream release tarballs with
   pinned SHA-256) in a rootless arm64 Alpine container:

   ```sh
   rootfs/mainline-init/build-static-tools.sh      # writes rootfs/out/static/{busybox,kexec,...}
   ```

   BusyBox is configured statically with `busybox.config.fragment` on top of `defconfig`.
2. Pack the initramfs:

   ```sh
   python3 -I rootfs/mainline-init/build.py        # writes rootfs/out/mainline-init.cpio.gz
   ```

   The archive is reproducible (fixed inode numbers, uid/gid 0, mtime 0, gzip mtime 0). The script checks that
   the binaries are static ARM64 ELF files and that the result is under 2 MiB.
3. **GPU firmware is an optional, user-supplied input** (see [hardware/gpu.md](hardware/gpu.md)): if you pass
   a firmware directory containing `a650_sqe.fw`, `a650_gmu.bin` and the zap shader image from your device,
   `build.py` places them at `/lib/firmware/qcom/...` in the archive. Without it the archive has no
   firmware. The resulting archive then contains device firmware and is not redistributable.

## The stage-2 rootfs

The stage-2 rootfs is built from Alpine edge packages by `rootfs/build.sh`:

```sh
rootfs/build.sh                # rootfs/out/stage2-YYYYMMDD.tar.gz (+ .sha256, .manifest.txt)
```

- It is a `.tar.gz`, not `.tar.zst`: the initramfs BusyBox's `tar` handles gzip only.
- The package set is `rootfs/packages.txt`: base system, Python 3, `strace`, `evtest`, `i2c-tools`, `usbutils`,
  `pciutils`, `iproute2`, `tcpdump`, `dtc`, `dropbear`, selected `util-linux` sub-packages, and (for the GPU,
  once it works) `mesa-vulkan-freedreno`, `vulkan-loader`, `vulkan-tools`, `drm_info`.
- Packages are installed into a clean root with `apk --root`, then **every tool that can write hardware or
  storage is deleted again**: `mkfs*`, `fdisk`/`sfdisk`/`parted`, `wipefs`, `losetup`, flash tools, `fsck`,
  `hdparm`, `i2cset`/`i2ctransfer`, `setpci`, serial tools (the display backlight MCU is on a UART),
  `watchdog`/`wdctl`, `fbset`, `vkcube` (it can modeset through `VK_KHR_display`), `hwclock`/`rtcwake`,
  `switch_root`/`pivot_root`. `util-linux` is installed per sub-package because the meta-package would pull
  in the partition tools.
- A small headless Vulkan compute smoke test, `/usr/local/bin/vk-compute-smoke` (source in `rootfs/vkcompute`),
  is built in the container and run once against software Vulkan (lavapipe) there. By default it refuses to
  run on a non-Qualcomm device.

### Using it on the headset

Because the initramfs is limited to 2 MiB, stage 2 is pulled over the USB link after boot:

1. On the host, `tools/serve-usb.py` serves the tarball over HTTP on the USB interface (bound to
   `10.42.0.1`).
2. `tools/dev s2-up` fetches it into a size-limited tmpfs at `/stage2`, verifies its SHA-256, extracts it,
   bind-mounts `/proc`, `/sys`, `/dev` and `/tmp` into it; `tools/dev s2` runs commands in the chroot and
   `tools/dev s2-down` tears it down again. The exact steps are in `tools/dev`.

Things to know:

- Peak RAM use is about 110 MB; the headset has about 5.3 GiB available.
- The dead-man timer is shorter than a stage-2 session (set-up alone can take several minutes), so the
  heartbeat has to keep running ([booting.md](booting.md)).
- `/sys` is bind-mounted, so anything mounted at `/sys/kernel/debug` shows up inside the chroot. Reading
  pin-control and GPIO debugfs files on this SoC can reset the device, and so can walking `/sys` recursively
  into such files. The init mounts debugfs read-only only on its failure paths.
- `/tmp` is shared with the init, including its state files; a chroot program that created `/tmp/lvr-reset`
  would arm a watchdog reset.
- `host/host-setup.sh --nfs` can export an optional NFS work share to the USB link (off by default). The
  kernel has an NFS client, but BusyBox has no NFS mount helper and the share is untested from the headset;
  files move over HTTP.
