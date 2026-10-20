# open-quest-vr

Mainline Linux on the Meta Quest 2 (Snapdragon XR2 / SM8250, board "hollywood").

A Linux 6.18 kernel with a Quest 2 device tree and a handful of drivers, booted from stock Android
through the [quest-kexec](https://github.com/Mac898/quest-kexec) loader into a RAM-only root
filesystem. Nothing is written to the headset's storage.

## Status

Works: all 8 CPU cores, cpufreq with thermal zones and the fan, USB gadget Ethernet with a root
shell, the syncboss sensor hub's IMU stream, a read-only battery gauge, the apps SMMU with USB
and the syncboss SPI bus attached, the status LED, Linux-to-Linux kexec and a warm return to
Android.

Not working yet: display, GPU, Wi-Fi/Bluetooth, cameras, controllers, audio, video decode,
charging. See [kernel/README.md](kernel/README.md#hardware-support) for details.

## Layout

| Directory | Contents |
|---|---|
| [kernel/](kernel/README.md) | Patch series against Linux v6.18.55 (device tree, drivers, config fragments), optional debug patches, `build.sh` |
| [rootfs/](rootfs/README.md) | Rootless (podman) builds of the initramfs and the optional stage-2 Alpine rootfs |
| [tools/](tools/README.md) | Host tools: `dev` (headset shell client with a command denylist), USB file server, LED-code decoder, syncboss IMU capture, DTB/config diff tools |
| [host/](host/host-setup.sh) | One-time host setup (qemu binfmt, podman, optional NFS share) |
| [examples/](examples/module-allowlist.txt) | Example module allowlist for `tools/dev mod-load` |
| [docs/](docs/README.md) | Booting, rootfs, design decisions, hardware reference |

## Getting started

1. Read [docs/safety.md](docs/safety.md) — some mistakes on this hardware can't be undone.
2. Build the kernel: [kernel/README.md](kernel/README.md).
3. Build the initramfs: [rootfs/README.md](rootfs/README.md).
4. Boot it with quest-kexec: [docs/booting.md](docs/booting.md).

## Licence

GPL-2.0 ([LICENSE](LICENSE)). The kernel patches follow the kernel's licensing; new device tree
and binding files carry their usual upstream dual-licence SPDX identifiers.

This project is not affiliated with or endorsed by Meta.
