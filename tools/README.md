# tools/

Host-side tools for working with a Quest 2 that runs the mainline initramfs
(`rootfs/mainline-init/`). Everything here is Python 3 standard library only; nothing needs
the headset except `dev` (and only for the commands that talk to it). GPL-2.0-only.

Requirements: Python 3.9+, and for the tests that need them `dtc` (`dtb_delta`), `podman` +
`qemu-user-static`/binfmt for aarch64 (the `*_sim.py` tests; see `host/host-setup.sh`).

## dev: headset command-line client

`tools/dev` talks to the root telnet shell the initramfs starts on the USB-Ethernet link
(default `10.42.0.2:23`, override with `QKX_HOST`). Run `tools/dev --help` for the full command list.

```
tools/dev check 'CMD'            # offline: would CMD be refused?
tools/dev status                 # uptime, kernel, battery
tools/dev sh 'cat /proc/cpuinfo' # run a command (denylist-checked first)
tools/dev pull /tmp/x.log x.log  # copy a file off the headset
tools/dev heartbeat --loop 60    # refresh the init's dead-man timer
tools/dev return-android         # leave Linux (warm reset back to Android)
```

**Safety model.** Storage, PMIC and MCU interfaces on the headset can be damaged by a stray write, so every
command passes a denylist before it is sent. It refuses persistent-storage writers, flashing and MCU
programming, secure/physical memory access (`/dev/mem`, GPIO/pinctrl and IOMMU debugfs), watchdog and I2C
tools, writes into `/sys`, `/proc` and configfs, recursive `rm`, unmounting system mounts, `kill` without
explicit PIDs, and free-form reboot/kexec/insmod. The few legitimate writes are fixed, hash-pinned
subcommands that self-check against the denylist first (`fan-test`, `cpufreq-cap`, `syncboss-txlen`,
`fw-fallback`, `mod-load`/`mod-unload`, `kexec-self`, `heartbeat`, `return-android`). `tools/dev check`
runs the same test offline. It guards against slips and is not a sandbox: it cannot see through `eval` or
string-assembled command names.

Other subcommands: `s2-up`/`s2`/`s2-down` (fetch a stage-2 Alpine rootfs from `serve-usb.py`
into a tmpfs and chroot into it), `load-step` (bounded CPU load under a host-side thermal
guard), `drain` (spaced battery-gauge samples), `mod-fetch` (modules tarball into RAM),
`kexec-self` (same-device-tree kexec of a new kernel).

Configuration:

| Setting | Meaning |
|---|---|
| `QKX_HOST` | headset address, default `10.42.0.2` |
| `QUEST_KEXEC_DIR` / `return-android --kexec-dir DIR` | a [quest-kexec](https://github.com/Mac898/quest-kexec) checkout; only needed for `return-android` when the target runs the downstream quest-kexec boot (it calls that project's `tools/return-android.sh`). `dev` says so and sends nothing if it is unset |
| `QUEST_LOG_DIR` | where `load-step` and `drain` write logs when `--log` is not given (default `./logs`); `--log FILE` takes any path |

Module allowlist format for `mod-load`/`mod-unload`: see `examples/module-allowlist.txt`.

## Other tools

| File | What it does |
|---|---|
| `serve-usb.py` | read-only HTTP server for `rootfs/out/` (or `--dir`) bound to `10.42.0.1` only, so the headset can `wget` the stage-2 tarball, module tarballs, or a kexec Image + initramfs. Refuses to bind off the USB link |
| `syncboss_capture.py` | on the headset, inside the stage-2 chroot: powers the sensor MCU through the `quest2-syncboss` driver, sends only the two approved 4-byte IMU enable/disable packets, records the streams to a `.sbcap` file (`capture`); `parse` and `cues` run on the host |
| `led_code.py` | reference encoder/decoder for the front-LED diagnostic code the init blinks when USB does not come up (`encode`, `collect`, `decode`, `video`, `worst`) |
| `dtb_delta.py` | `dtc`-normalised diff of two DTBs; flags regulator, clock, IOMMU, storage, MCU, reserved-memory, secure, power, watchdog and similar changes |
| `kconfig_delta.py` | diff of two kernel `.config` files; flags storage, secure, reset/panic, kexec/modules, IOMMU, display/GPU and similar symbols |

## Tests

All offline; none contacts a headset.

```
python3 -I tools/test_dev_denylist.py      # denylist + every fixed command string (~2000 checks)
python3 -I tools/test_syncboss_capture.py  # fake driver, synthetic IMU records
python3 -I tools/test_led_code.py
python3 -I tools/test_dtb_delta.py         # needs dtc
python3 -I tools/test_kconfig_delta.py
# need podman + qemu-user binfmt, and rootfs/out/mainline-init.cpio.gz (see rootfs/README.md):
python3 -I tools/test_dev_telnet_sim.py    # tools/dev's real telnet transport against the target busybox
python3 -I tools/test_fan_sim.py           # fan-test and load-step under the target busybox
# also needs rootfs/out/stage2-latest.tar.gz (rootfs/build.sh) and the host address 10.42.0.1
# for the full flow (without it the fetch part is skipped):
python3 -I tools/test_stage2_sim.py
```
