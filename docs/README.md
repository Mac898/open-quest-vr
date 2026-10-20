# Documentation

Documentation for mainline Linux on the Meta Quest 2 (SM8250, board "hollywood"). It describes what exists
today: how to boot the kernel, how the root filesystems are built, and the hardware facts the device tree is
based on. [safety.md](safety.md) lists the risky areas of the hardware.

| Page | What it covers |
|---|---|
| [safety.md](safety.md) | Risky areas: storage, MCU and radio firmware, charger and gauge, regulators and panel power, secure memory, the unauthenticated root shell |
| [booting.md](booting.md) | Booting with quest-kexec (inputs, what the kernel expects), the USB Ethernet link and telnet shell, returning to Android |
| [rootfs.md](rootfs.md) | The initramfs and the stage-2 rootfs: contents, how they are built, how to use them |
| [led-diag-code.md](led-diag-code.md) | The init's LED blink code for boots with no USB link (decoder: `tools/led_code.py`) |
| [design-decisions.md](design-decisions.md) | Why the return path, syncboss DMA, the read-only gauge, the apps SMMU inherit mode and the SCM patch look the way they do |
| [upstream-state.md](upstream-state.md) | Survey of upstream (mainline and community) support for this hardware, as of 2026-10-11 |
| [hardware/](hardware/README.md) | Hardware reference, one page per topic |

Hardware pages:

| Page | Topic |
|---|---|
| [hardware/power-thermal.md](hardware/power-thermal.md) | PMICs, regulators, thermal, fan, charger, CPU clusters, reset |
| [hardware/cpufreq-thermal.md](hardware/cpufreq-thermal.md) | CPU frequency control and throttling in the project kernel |
| [hardware/battery-gauge.md](hardware/battery-gauge.md) | Read-only TI bq27z561 gauge |
| [hardware/display-gpu-video.md](hardware/display-gpu-video.md) | Panel, DSI, display controller, Venus decoder |
| [hardware/gpu.md](hardware/gpu.md) | Adreno 650: hardware, firmware, status |
| [hardware/sensors-connectivity.md](hardware/sensors-connectivity.md) | syncboss, cameras, USB, I2C, audio, input |
| [hardware/imu-axes.md](hardware/imu-axes.md) | IMU axis frame |
| [hardware/wifi.md](hardware/wifi.md) | QCA6390 Wi-Fi/Bluetooth |

## Status in one paragraph

The mainline 6.18 kernel boots on the headset through quest-kexec and gives a USB Ethernet link with a
BusyBox shell, all 8 CPU cores, cpufreq with thermal zones, the fan, a read-only battery gauge, the syncboss
IMU stream (about 987 Hz) through kernel-managed SMMU translation, Linux-to-Linux kexec and a RAM-preserving
return to Android. The display, GPU, Wi-Fi/Bluetooth, cameras, audio and video decode do not work yet.

## Conventions

- Hardware facts are labelled *confirmed* (seen on the device or in the device tree it booted with), *strong*
  (read from source) or *inferred* (reasoning). An inferred fact is never presented as confirmed.
- Downstream references of the form `oculus-linux-kernel: <path>` point into the public downstream kernel,
  <https://github.com/facebookincubator/oculus-linux-kernel>. The project's own device tree is
  `arch/arm64/boot/dts/qcom/sm8250-oculus-hollywood.dts`, shipped as a kernel patch.
- The boot vehicle is published separately (GPL-2.0): <https://github.com/Mac898/quest-kexec> (fork of
  <https://github.com/YusufOzmen01/quest-kexec>).
