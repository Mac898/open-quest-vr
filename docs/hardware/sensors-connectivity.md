# Sensors, syncboss, cameras, USB, audio and other peripherals

Sources: the downstream device tree as observed on the device, and the public downstream source
(`oculus-linux-kernel`: `arch/arm64/boot/dts/oculus/hollywood/`, `arch/arm64/boot/dts/vendor/qcom/`,
`drivers/staging/oculus/`). Confidence: *confirmed* = seen in the booted DT or observed, *strong* = read
from source, *inferred* = reasoning.

For Wi-Fi/Bluetooth see [wifi.md](wifi.md); for the fan, charger and gauge see
[power-thermal.md](power-thermal.md) and [battery-gauge.md](battery-gauge.md).

## 1. syncboss: the sensor hub

**syncboss is a Nordic nRF52832 MCU on SPI.** It hosts the IMU, magnetometer and proximity sensor, times the
camera exposure, and is the radio link to the Touch controllers (the nRF52832 is the only radio in the
system other than Wi-Fi/Bluetooth; that role is inferred). The IMU is not on an SoC bus: there are no IIO
IMU devices.

DT identity (confirmed): node `syncboss_nrf@0` with `compatible = "meta,syncboss-spi"`,
`meta,swd-flavor = "nrf52832"`, `meta,fw-path = "syncboss.bin"`.

| Item | Value |
|---|---|
| Bus | `spi@894000` = GENI serial engine 19 in QUP wrapper 2 (`8c0000`), pins gpio0-3 function `qup19`, 6 mA |
| SPI | Mode 3, up to 8 MHz, at least 500 us between transactions |
| Frame | Fixed-size full-duplex transactions of 248 bytes with a magic word and a checksum. This is larger than the serial engine's FIFO, so the kernel's `spi-geni-qcom` uses the engine's DMA (see [../design-decisions.md](../design-decisions.md)) |
| Reset | gpio5, active low, driven low at probe. The MCU is held in reset until it is opened; measured about 137 ms from reset release to awake |
| Ready | gpio4 (input, pull-down), rising edge = MCU awake / data ready |
| nsync | gpio66, falling edge; per-frame sync for cameras and display (vsync) |
| SWD | gpio52 (clock) and gpio53 (I/O). **Left floating on mainline**; see below |
| Supplies | `meta,syncboss-supply` = pm8150_s4 (1.8 V), `meta,imu-supply` = pm8150a_l7 (3.0 V), `meta,mag-supply` = pm8150a_l8 (1.8 V) |
| Rate | **IMU at about 987 Hz** (firmware default), observed on mainline with no framing errors |

Axes of the IMU are documented in [imu-axes.md](imu-axes.md).

**Flashing the MCU is risky.** The downstream driver has an SWD programmer (`meta_swd`, a bit-banged SWD driver
that can erase and rewrite the nRF52832 flash; trigger is a write to a sysfs `update_firmware` attribute).
Nothing flashes automatically at boot, but a bad flash can brick the IMU and the controllers. The mainline
driver `drivers/misc/quest2-syncboss.c` in this project has no SWD path at all, keeps gpio52/53 as floating
inputs, and only accepts a small allowlist of command frames from userspace (IMU enable/disable). On close it
sends the MCU's graceful shutdown command itself, then resets it. See [../safety.md](../safety.md).

## 2. Tracking cameras

- Four `qcom,cam-sensor` nodes on two CCI buses (`ac4f000`, `ac50000`), four CSI PHYs (0-3), MCLK 24 MHz,
  analog supply pm8150a_l10 3.0 V, IO supply pm8150_s4 1.8 V. There is no reset or power-down GPIO in the
  nodes; the only GPIO is the MCLK pin. (confirmed)
- The downstream Spectra camera stack identifies sensors from userspace data, so the DT contains no part
  numbers or I2C addresses. The cameras are believed to be four OmniVision global-shutter monochrome
  sensors of the OV7251 family, triggered by syncboss (inferred; the downstream tree names OV7251 on an
  older board). Mainline has an `ov7251` driver.
- Camera frame sync (`cam-fsync`) is disabled downstream (`CONFIG_CAM_FSYNC` unset); sync arrives through
  syncboss nsync.
- **Status on mainline:** no camera node is enabled; nothing is captured.

## 3. USB

- One DWC3 controller, `ssusb@a600000` (`snps,dwc3`, dual-role, super-speed capable). HS PHY `hsphy@88e3000`,
  SS PHY `ssphy@88e8000`. The headset's Type-C port is on this controller.
- Rails: HS PHY 0.88 V (pm8150_l5), 1.8 V (pm8150_l12), 3.072 V (pm8150_l2); SS PHY 1.2 V (pm8150_l9) and
  0.912 V (pm8150_l18).
- Power delivery is handled by a PD PHY on pm8150b with default sink capabilities 5 V 3 A / 9 V 3 A.
  Vendor-defined PD messages (`meta,usbvdm-qtipd`) are used by the dock and external battery.
- In this project the controller runs in **peripheral mode, high speed only**, as a USB Ethernet (ECM) gadget
  (`1d6b:0105`). See [../booting.md](../booting.md).

## 4. I2C buses

| Bus | Controller | Device |
|---|---|---|
| i2c (SE8) | `a80000.i2c`, gpio24/25 | `ak4331` audio DAC / headphone amp (address 0x10); `cm710x` DSP codec (0x2c, firmware loaded over SPI) |
| i2c (SE4) | `990000.i2c`, gpio8/9, 100 kHz | `bq27z561` gauge (0x55); see [battery-gauge.md](battery-gauge.md) |
| i2c (SE13) | `a94000.i2c`, gpio36/37, 100 kHz | `as5510` 10-bit linear Hall sensor (0x56), most likely the IPD / lens-separation sensor (inferred); supply pm8150_l10 |
| i2c (SE15) | `884000.i2c`, gpio44/45 | children disabled (FM radio, `fsa4480`) |

IMU, magnetometer and proximity are not on I2C (they are behind syncboss).

## 5. Audio

Codecs: `ak4331` (I2C) and `cm710x` (I2C + SPI mode 3 up to 20 MHz; LDO enable gpio147, reset gpio148,
package-select gpio144), plus a `max98502` speaker amp with only an enable pin (gpio145). Audio transport is
the Qualcomm ADSP/APR stack. No sound card is bound on the downstream kexec-started kernel. Neither codec has
a mainline driver; audio does not work on mainline here.

## 6. Input, haptics and other devices

- Input: PMIC power key and `volume_up` GPIO key (gpio of pm8150, pin 6, active low). No touch.
- Haptics: LRA actuator with a boost regulator on pm8150b gpio5. No mainline driver in use.
- `oculus,blu` (`reset-gpios` gpio89, `boot-gpios` gpio88, UART `ttyHS13` = `994000`, gpio14/15): the
  display backlight MCU, with its own firmware-flash driver downstream; flashing it is as risky as flashing syncboss.
- Three PMIC ADCs (`vadc@3100`) carry the thermistors used by the thermal zones.
- `oculus,external_battery`, `oculus,charging_dock`, and virtual temperature sensors exist in the vendor DT.

## 7. Open items

- Camera sensor part numbers and I2C addresses, CSI lane configuration, trigger wiring from syncboss.
- Which DAI each audio codec uses; speaker amp control.
