# Hardware reference

Facts about the Meta Quest 2 ("Hollywood", Snapdragon XR2 / SM8250, 6 GiB) as they matter for running
mainline Linux on it. Sources are the public downstream kernel
(<https://github.com/facebookincubator/oculus-linux-kernel>, cited as `oculus-linux-kernel: <path>`), this
project's own device tree (`arch/arm64/boot/dts/qcom/sm8250-oculus-hollywood.dts`, shipped as a kernel
patch), and simple observation of the device.

Each page says what is *confirmed*, *strong* (read from source) or *inferred*.

| Page | Contents |
|---|---|
| [power-thermal.md](power-thermal.md) | PMICs, regulators, thermal sensors and trips, fan, charger and battery hardware, CPU clusters, watchdog and reset |
| [cpufreq-thermal.md](cpufreq-thermal.md) | How the project kernel drives CPU frequency and CPU thermal throttling |
| [battery-gauge.md](battery-gauge.md) | The read-only TI bq27z561 fuel gauge |
| [display-gpu-video.md](display-gpu-video.md) | Panel, DSI, display controller, Venus video decoder (not working yet) |
| [gpu.md](gpu.md) | Adreno 650 and GMU: hardware, firmware, current status (not working yet) |
| [sensors-connectivity.md](sensors-connectivity.md) | syncboss sensor hub, cameras, USB, I2C devices, audio, input |
| [imu-axes.md](imu-axes.md) | IMU axis frame and conversion to OpenXR |
| [wifi.md](wifi.md) | QCA6390 Wi-Fi/Bluetooth: hardware, firmware, current status (not enabled) |

## What works on mainline today

USB Ethernet gadget and telnet shell, all 8 CPU cores, cpufreq and CPU thermal zones, the fan, the read-only
battery gauge, the syncboss IMU stream (about 987 Hz), and a return to Android. Display, GPU, Wi-Fi,
Bluetooth, cameras, audio and video decode do not work yet.
