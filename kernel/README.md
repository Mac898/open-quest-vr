# Kernel

Mainline Linux for the Oculus Quest 2 (codename "hollywood", Qualcomm XR2 / SM8250), as a patch series on
top of Linux **v6.18.55** (linux-stable).

The kernel is started from the running stock Android system by the
[quest-kexec boot loader](https://github.com/Mac898/quest-kexec). It relies on the state Android, the
secure firmware and the remote processors leave behind, and leaves most of that hardware alone.

## Layout

| Path | Contents |
|---|---|
| `patches/` | The main series, `git format-patch` output, applied in the order of `patches/series`. |
| `patches-debug/` | Optional diagnostic series on top of the main one (`patches-debug/series`). Not for normal use. |
| `build.sh` | Applies the series to v6.18.55, configures and builds `Image`, the DTB and the modules. |

## The main series

| # | Patch | What it does |
|---|---|---|
| 01 | dt-bindings: arm: qcom: Add Oculus Quest 2 (hollywood) | `oculus` vendor prefix and the board compatible. |
| 02 | dt-bindings: misc: Add Oculus Quest 2 syncboss MCU | Binding for the sensor-hub MCU on SPI (no programming interface). |
| 03 | dt-bindings: iommu: arm,smmu: Add Oculus Quest 2 apps SMMU inherit mode | `oculus,hollywood-apps-smmu-inherit` compatible and the `oculus,inherit-attach-sids` allowlist. |
| 04 | dt-bindings: power: reset: Add Oculus Quest 2 warm reset | Binding for the PS_HOLD warm reset node. |
| 05 | spmi: pmic-arb: Optionally mask PMIC interrupts inherited across kexec | Stops PMIC interrupts Android left enabled from storming before their drivers probe. |
| 06 | firmware: qcom_scm: Make no SCM calls the device firmware does not expect | No download-mode write without `qcom,dload-mode`, WAITQ only if available, QTEE probe only with `QCOMTEE`. |
| 07 | soc: qcom: geni-se: Allow a node without the DDR path | Lets an SE vote only its register paths; warns when the QUP core clock is not voted. |
| 08 | spi: spi-geni-qcom: Keep the SE interrupt off while runtime suspended | No SE register access with the clocks off; acks stale status left by the previous owner. |
| 09 | usb: dwc3: qcom-legacy: Quiesce the controller on shutdown | Stops dwc3 DMA into the old kernel's memory across kexec. |
| 10 | drm/msm/a6xx: Add zap_preloaded module parameter | Optionally reuse a zap shader the secure world already holds. |
| 11 | iommu/arm-smmu: Add inherit mode for an SMMU left live across kexec | Binds the live apps SMMU without resetting it; allowlisted masters take over their inherited stream map entry. |
| 12 | iommu/arm-smmu: Add KUnit tests for inherit mode | Register-access rules, probe, attach and release on a fake register window. |
| 13 | misc: Add Oculus Quest 2 syncboss SPI link driver | `/dev/syncboss*`: IMU stream; userspace can send only IMU start/stop; no MCU programming path. With KUnit tests. |
| 14 | misc: Add Oculus Quest 2 warm reset driver | Warm reset back to Android (`/proc/lvr_warm_reset`, optional on panic) and an optional boot-stall detector. With KUnit tests. |
| 15 | arm64: dts: qcom: Add Oculus Quest 2 (hollywood) | The board device tree. |
| 16 | arm64: configs: Add Oculus Quest 2 config fragments | `quest2.config`, `quest2-iter.config`, `quest2-gpu.config`, `quest2-smmu.config`. |

The debug series adds an early-boot watchdog-timing beacon (`CONFIG_QUEST2_DIAG_WDT_BEACON`, off by default;
it adds about 84 s to every boot) and `quest2-diag.config` (lockup and hung-task panics, watchdog pretimeout
panic, dynamic debug).

## Building

```sh
# clone linux-stable v6.18.55 (shallow), apply, build into ./out
./build.sh --fetch ~/src/linux-quest2 --out out

# or use an existing linux-stable checkout that has the v6.18.55 tag
./build.sh --src ~/src/linux-stable --out out --jobs 16

# or the release tarball from kernel.org
./build.sh --tarball ~/src/linux-6.18.55 --out out
```

Options: `--debug` also applies `patches-debug/` and adds `quest2-diag.config`; `--out DIR`; `--jobs N`;
`--branch NAME` (branch created in the source tree); `--no-apply` (build a tree that already carries the
series). The cross compiler defaults to `aarch64-linux-gnu-`; set `CROSS_COMPILE=...` for another one, or
`LLVM=1` to build with clang. No root is needed.

To do the same by hand:

```sh
git checkout -b quest2 v6.18.55
git am /path/to/kernel/patches/*.patch
make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- O=../build \
     defconfig quest2.config quest2-iter.config quest2-gpu.config quest2-smmu.config
make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- O=../build \
     Image qcom/sm8250-oculus-hollywood.dtb modules
```

Later fragments win. `quest2-iter.config` (kexec, no NVMEM writer modules) is part of the normal build;
`quest2-diag.config` goes right after `quest2.config`.

KUnit tests run on x86_64 under QEMU or on UML:

```sh
./tools/testing/kunit/kunit.py run --arch=x86_64 --kunitconfig=drivers/iommu/arm/arm-smmu
printf 'CONFIG_KUNIT=y\nCONFIG_QUEST2_SYNCBOSS_KUNIT_TEST=y\nCONFIG_QUEST2_WARM_RESET_KUNIT_TEST=y\n' \
     > /tmp/quest2.kunitconfig
./tools/testing/kunit/kunit.py run --kunitconfig=/tmp/quest2.kunitconfig
```

## Using the outputs

The quest-kexec loader takes three inputs:

- `out/Image`: the kernel;
- `out/sm8250-oculus-hollywood.dtb`: the device tree; the loader adds the command line, the initramfs location
  and its own memory reservations at run time;
- an initramfs, built by `rootfs/` in this repository.

Everything needed to boot is built in; `out/modules/` holds the loadable modules (mainly the headless GPU
stack). The command line and the boot procedure are in [docs/booting.md](../docs/booting.md), the loader's
own steps in its [repository](https://github.com/Mac898/quest-kexec).

## Hardware support

Works:

- USB 2.0 peripheral: USB gadget Ethernet (CDC ECM) for a network shell to the host;
- syncboss sensor hub: IMU stream through `/dev/syncboss0`, `/dev/syncboss_stream0`, `/dev/syncboss_control0`;
- CPU frequency scaling (cpufreq-hw), thermal zones with CPU passive trips, fan (open loop, two speeds);
- battery gauge (bq27z561), read-only;
- apps SMMU in inherit mode, with USB and the QUP2 wrapper (syncboss SPI DMA) attached;
- front RGB status LED; ramoops; warm reset back to Android.

Not working yet: display, GPU (the nodes and modules are present, nothing probes at boot), Wi-Fi, Bluetooth,
audio, cameras, controllers, internal storage (UFS is disabled on purpose), charging control (no charger
driver), and everything else not listed above.

## Risky areas

The kernel shares the hardware with firmware that was not reset, and some mistakes are not recoverable:

- **Internal storage.** UFS is disabled in the device tree, so nothing in this kernel can write to it.
- **MCU firmware.** The syncboss driver has no programming path and its DT node no SWD interface; reflashing
  the MCU risks losing the IMU and controllers.
- **Charging.** There is no charger driver; the gauge is read-only and its node has no `monitored-battery`.
  The battery does not charge while this kernel runs.

More in [docs/safety.md](../docs/safety.md).

## Licence

The patches are licensed under GPL-2.0, like the Linux kernel. New files carry their own SPDX identifiers:
the device tree is BSD-3-Clause and the bindings are `GPL-2.0-only OR BSD-2-Clause`, as is usual upstream.
`build.sh` is GPL-2.0.
