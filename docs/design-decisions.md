# Design decisions

Short records of the decisions that shaped the kernel and init, each as context, decision and consequence.

## 1. Returning to Android from mainline

**Context.** The downstream kernel returns to Android with a "warm" reset that keeps RAM: it sets the PMIC
reset type to warm over the SPMI bus, then drops `PS_HOLD`. Mainline has no such hook. A hardware-watchdog
bite always gets back to Android but is a cold reset, which wipes RAM and with it the pstore/ramoops console
log. The upstream route (`reboot`, then PSCI `SYSTEM_RESET2` warm) did not return on this platform.

**Decision.** A small warm-reset driver (`quest2-warm-reset`) performs the downstream write sequence: same
registers and values, one write each, ending in the `PS_HOLD` drop, with a short hardware-watchdog backstop
armed beforehand. It reads the PMIC power-on registers through a minimal path that does not enable the PM8150
child drivers. The init's plain watchdog reset remains as a fallback. The driver does not set reboot-mode
reasons (bootloader/EDL/recovery) and makes no secure-monitor download-mode or debug-image calls.

**Consequence.** A return keeps RAM, so the console log survives and can
be read from Android. Details and usage are in [booting.md](booting.md). The mechanism writes only volatile
PMIC control registers (never non-volatile memory, charger or gauge registers).

## 2. syncboss on mainline over SPI with serial-engine DMA

**Context.** The syncboss MCU exchanges fixed 248-byte frames over SPI. That is more than the serial
engine's FIFO, so the stock `spi-geni-qcom` driver switches to the engine's DMA. That DMA is issued by the
QUP wrapper's SMMU stream (SID 0x63), which Android left translating after the jump: the same failure class
as the USB controller's DMA (section 4).

**Decision.** Run syncboss in DMA mode with the unmodified `spi-geni-qcom` DMA path, giving the DMA stream
its own kernel-managed SMMU context through the attach described in section 4. Port the driver with no MCU-programming path (no SWD, no `update_firmware`), and with
userspace limited to an allowlist of IMU enable/disable frames. On last close the driver sends the MCU's
graceful shutdown command itself, then resets it, as the downstream driver does; userspace cannot send that
command.

**Consequence.** The mainline driver `drivers/misc/quest2-syncboss.c` streams the IMU at about 987 Hz with no
framing errors and no SPI errors, and the MCU firmware is never touched.

## 3. Read-only battery gauge

**Context.** The battery should be observable while Linux runs without changing charger or gauge behaviour
(lithium-ion hazard).

**Decision.** Enable only the TI bq27z561 gauge on a FIFO-only I2C bus: QUP wrapper 0, serial engine 4, no
IOMMU, DMA or power-domain properties and no memory interconnect path; carve gpio8/9 out of the reserved GPIO ranges; build
`BATTERY_BQ27XXX(_I2C)`; keep `I2C_CHARDEV` off; and leave `monitored-battery` off the node. Without
that property the driver has no write path.

**Consequence.** Voltage, temperature and capacity are readable; no charging or gauge configuration is
touched. Details: [hardware/battery-gauge.md](hardware/battery-gauge.md).

## 4. Apps SMMU "inherit mode"

**Context.** An SMMU (the IOMMU between DMA-capable devices and RAM) is configured by *stream-matching entries*
(which device stream maps to which *context bank*) and by *context banks* (page-table base, control bits). When
the loader jumps from Android into the new kernel, none of that is reset: the SMMU still holds Android's
configuration, including streams that belong to firmware or secure software, devices that are still
running (display, DSPs) and the boot storage. The stock mainline driver does the opposite of what is safe
here. At probe it rewrites every stream entry, disables every context bank and recomputes the global control
register. That breaks live masters and can break storage.

Leaving the SMMU alone and disabling the driver is not safe either: a device the kernel brings up (USB, the
QUP serial engine for syncboss) then issues addresses that Android's stale tables translate, so its DMA
faults or lands in RAM that the new kernel now owns. The loader has a per-device workaround that flips one named
device's inherited context bank to an identity map. That works only for simple single-stream devices.

**Decision.** A kernel patch adds an opt-in "inherit" mode to `arm-smmu`, selected by a dedicated compatible
(`oculus,hollywood-apps-smmu-inherit`) on the `apps_smmu` node:

- The driver **reads** the hardware (ID registers, every stream-matching entry, its context-bank assignment)
  and seeds its bookkeeping from that: which stream entries and which context banks are already in use. It
  **writes nothing** at probe. A runtime check on every register accessor refuses writes to the global
  registers, to inherited entries and to inherited context banks, and refuses any stream entry type other
  than "translate".
- A device is attached only if its stream ID is on an allowlist in the DT (`oculus,inherit-attach-sids`) and
  its entry matches exactly (no stream-ID mask). Currently the allowlist is the USB controller (SID 0x0) and the
  QUP wrapper 2 that carries the syncboss SPI engine (SID 0x63); each has `iommus` in the DT.
- On attach the kernel takes a **fresh context bank from the unused end of the pool**, programs it (the bank's
  registers, then the device's own stream entry, 14 register writes in all), and points that one stream entry
  at the new bank. Android's bank for that device is left unreferenced but untouched.
- Every other stream keeps running on its inherited bank. Devices without `iommus` are never attached. The
  storage controllers (UFS and the SD controller) have `iommus` deleted in the DTS on purpose and are not
  attached.
- The loader's identity-map workaround stays available as a safety floor for USB only: if the attach is
  refused, USB keeps working through the loader's identity map instead of dying with a stale translation.

**Consequence.**

- With no device attached, a dump of the SMMU registers from Android just before the jump and another from
  the new kernel after probe are identical (every stream entry and every inherited context bank), apart from
  the contexts the loader itself switched to identity.
- With the attach in place, USB and the syncboss SPI DMA both run through kernel-managed translation (14
  writes, none refused, DMA works, IMU at about 987 Hz).
- Most inherited banks are still referenced by streams for hardware this kernel does not drive. They are
  left alone. Further devices (PCIe/Wi-Fi, video, camera) would be attached the same way.
- The SMMU of the GPU is a separate instance and is not covered by this mode.

## 5. The kernel avoids the secure download-mode setting

**Context.** The mainline SCM (secure-monitor call) driver writes a download-mode setting when it probes. On
this device the stock system already has its own value in place and we have no reason to change it.

**Decision.** A small patch to `qcom_scm.c` makes the driver skip that write unless the DT carries
`qcom,dload-mode`, which this DTS does not. The remaining boot-time calls are limited to ones the downstream
kernel makes too.

**Consequence.** The setting stays as Android left it. The SCM driver is built in and probes at boot
(`driver_async_probe=qcom_scm` moves the probe to a worker thread so a hang there is caught and logged).
