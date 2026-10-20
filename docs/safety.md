# Risky areas

This project runs experimental kernels on a consumer headset with a lithium-ion battery, firmware that cannot
be restored from here, and no serial console. Most failures end in a reset. The areas below can do lasting
damage or are otherwise worth knowing about. How you use the project and your device is up to you; the
software is provided under GPL-2.0 with no warranty.

- **Device storage (UFS, SD).** The whole flow is RAM-only and the UFS and SD (`sdhc_2`) controllers are
  disabled in the device tree. A write to the storage can leave the headset unable to boot Android, and there
  is no recovery path from here. The shipped initramfs and stage-2 rootfs leave out the common tools that
  would write storage; that reduces accidents but is not a guarantee.
- **MCU and radio firmware.** The syncboss nRF52832 (IMU, controller radio) can be erased and rewritten over
  SWD by the downstream driver (a sysfs `update_firmware` write). The display backlight MCU (`oculus,blu`,
  UART `ttyHS13`) has its own flash driver. The QCA6390's non-volatile memory is written by Bluetooth
  bring-up through `hci_qca` (rampatch and NVM download over UART). A bad flash can permanently lose the IMU,
  the controllers, the backlight or the radio. The mainline syncboss driver here has no SWD path.
- **Charger and fuel gauge.** The charger PMIC (PM8150B) and its gauge are left disabled; the only battery
  access is the read-only bq27z561 gauge, which has no write path as long as its node has no
  `monitored-battery` property ([hardware/battery-gauge.md](hardware/battery-gauge.md)). Wrong charger or gauge
  settings on a lithium-ion cell can damage the battery.
- **No charging while Linux runs.** There is no charging stack in this kernel, so the battery only discharges
  until the headset is back in Android. `adb shell dumpsys battery` in Android and the gauge in Linux show the
  state.
- **Regulator voltages and panel power sequences.** Rail voltages and ranges, clock rates, pin configuration
  and the panel's power-on and power-off order can damage the panel or the SoC if they are wrong. The values in
  the device tree come from the downstream sources
  ([hardware/display-gpu-video.md](hardware/display-gpu-video.md)).
- **Secure and unowned memory.** Reading secure or arbitrary IOMMU-mapped memory (`/dev/mem`, `phys_to_virt`
  on addresses from secure or unknown IOMMU domains) hangs the system and reboots the device. The same goes
  for the pin-control debugfs files (`pinconf-pins`, `pinconf-groups`), which read registers without checking
  ownership, and for GPIO pins that firmware owns, so the device tree reserves GPIOs by default.
- **SMMU state.** The apps SMMU still holds Android's configuration after the jump. Writing its global
  registers, resetting it or attaching the storage controllers breaks live masters and can break storage
  ([design-decisions.md](design-decisions.md) section 4).
- **Reboot reasons and download mode.** `reboot bootloader/recovery/edl`, secure-monitor download-mode writes
  and PMIC reboot-reason writes can land the device in fastboot or EDL. `reboot`, `poweroff` and sysrq-`b`
  also do not return to Android on this platform; the warm-reset driver does ([booting.md](booting.md)).
- **The telnet shell is unauthenticated root.** The initramfs serves a root shell at `10.42.0.2:23` with no
  password and no encryption, on the USB link. Anyone who can reach that interface has full control of the
  device.
- **Host-tool filters are not a sandbox.** The deny-list in `tools/dev` and the applets omitted from the
  initramfs catch slips; they cannot see through `eval` or assembled command names.
