# Power, thermal, fan, charger, CPU clusters

Board "Hollywood" (Quest 2), SoC `qcom,kona` = Snapdragon XR2 / SM8250. This page records the hardware
facts the mainline device tree is built on.

Sources and confidence:

- **downstream DT** means the device tree of the stock downstream 4.19 kernel, as observed on the device
  after it booted (and in the public source: `oculus-linux-kernel: arch/arm64/boot/dts/oculus/hollywood/`
  and `arch/arm64/boot/dts/vendor/qcom/`). Public source repository:
  <https://github.com/facebookincubator/oculus-linux-kernel>.
- **project DTS** means `arch/arm64/boot/dts/qcom/sm8250-oculus-hollywood.dts`, shipped as a kernel patch
  in this repository.
- *confirmed* = seen on the device or in the DT it booted with. *strong* = directly implied by that.
  *inferred* = reasoning, flagged as such.
- Regulator "state" (on/off) was read from a downstream kernel that was started by kexec from Android. It
  reflects only that kernel's own votes, so a regulator shown as off is not proof it is physically off.
  Voltages and ranges are reliable; states are not.

## Headline facts

- **No secondary charger.** `smb1355` and `smb1390` are not present on this board: no DT node, no driver
  in the downstream config, and the SMB5 charger has `qcom,sec-charger-config = <0>`. (confirmed)
- **PM8009 is absent.** The downstream DT has blocks for it, but the kernel logs "could not find RPMh
  address" for every resource, so the project DTS does not describe it. (confirmed)
- **Downstream `pm8150a_*` regulators are the mainline `pm8150l` regulators** (RPMh resources `*c*`:
  ldoc1-11, smpc7/8, bobc1). Downstream `pm8150_*` are mainline `pm8150` (`*a*`). The arc "level"
  regulators (`cx/mx/mmcx/gfx/lcx/lmx/ebi.lvl`) are `rpmhpd` power domains in mainline.
- **The fan is a closed-loop RPM controller** in the downstream kernel (custom driver, `cooling-levels`
  are target RPM 0..7000, not PWM duty). Details below.
- **CPU numbering is a DT-order artefact.** In the downstream kernel logical CPU1 was MPIDR 0x700
  (prime), CPU2 0x400 (gold). Match cores by MPIDR, not by number.
- 44 of 110 downstream thermal zones use the `user_space` governor with 105-125 C trips for the stock
  userspace daemon; they are not needed to run Linux.

## 1. PMICs and buses

SPMI arbiter `qcom,spmi-pmic-arb` at 0xc440000 (standard mainline `spmi_bus` node). (confirmed)

| PMIC | SPMI IDs | Role on this board |
|---|---|---|
| pm8150 | 0, 1 | Primary PMIC: RTC, PON (power key, resin, system reset), temp-alarm, VADC, ADC_TM thermistors (xo, imu, rf-pa), GPIOs |
| pm8150b | 2, 3 | Charger/fuel-gauge PMIC: SMB5 charger, Type-C and PD PHY, FG-GEN4 gauge, BCL, VADC/ADC_TM (battery and three PCB thermistors), LPG and `hr_led` |
| pm8150l | 4, 5 | ID 4: BCL, VADC/ADC_TM (four camera thermistors), GPIOs. ID 5: **fan** (LPG PWM and tach supply), **LCDB** (display bias), RGB status LED (LPG channels 0-2), WLED/AMOLED blocks (unused) |
| pmxprairie | 8, 9 | Modem PMIC; only a PON node, no regulators used |
| pm8009 | 10, 11 | Not present (see above) |

RPMh: `rsc@18200000` (`qcom,rpmh-rsc`, label `apps_rsc`, DRV 2), the same as the mainline sm8250 node.
PMIC temp-alarm trips: pm8150 95 C, pm8150l 115 C, pm8150b 145 C.

## 2. Regulators (RPMh)

Voltage ranges from the downstream DT. Names on the left are the downstream names; the mainline names are
`vreg_*` in the project DTS. Only the rails relevant to bringing up the board are listed.

| Downstream name | RPMh | Range / boot value | Consumers that matter |
|---|---|---|---|
| pm8150_l2 | ldoa2 | 3.072 V | USB HS PHY 3.3 V rail, PD PHY |
| pm8150_l5 | ldoa5 | 0.880 V | USB HS PHY core, PCIe 0.9 V, DSI PHY 0.9 V, UFS PHY |
| pm8150_l6 | ldoa6 | 1.2 V | UFS vccq / ref clk |
| pm8150_l9 | ldoa9 | 1.2 V | PCIe 1.2 V rail, DSI controller 1.2 V, USB SS PHY core, CSI PHY |
| pm8150_l10 | ldoa10 | 1.8-2.96 V (2.96 V boot) | AS5510 Hall sensor (i2c), camera analog (3.0 V) |
| pm8150_l12 | ldoa12 | 1.8 V | USB HS PHY 1.8 V |
| pm8150_l14 | ldoa14 | 1.8-1.88 V | Display panel IO (`vddio`) |
| pm8150_l17 | ldoa17 | 2.496-3.008 V | UFS vcc |
| pm8150_l18 | ldoa18 | 0.8-0.92 V (0.912 V boot) | USB SS PHY |
| pm8150_s4 | smpa4 | 1.8 V | syncboss MCU and SWD core, UFS vccq2, QCA6390 IO, camera IO |
| pm8150_s5 | smpa5 | 1.824-2.04 V | QCA6390 rfa1 |
| pm8150_s6 | smpa6 | 0.6-1.128 V | QCA6390 aon and dig |
| pm8150a_bob | bobc1 | 3.008-3.96 V (3.312 V boot) | Fan supply |
| pm8150a_l7 | ldoc7 | 3.0 V | IMU (`meta,imu`) |
| pm8150a_l8 | ldoc8 | 1.8 V | Magnetometer (`meta,mag`) |
| pm8150a_l11 | ldoc11 | 3.104-3.304 V | Display backlight-unit MCU supply |
| pm8150a_s8 | smpc8 | 1.2-1.4 V | QCA6390 rfa2 |

Non-RPMh supplies:

- `display_panel_avdd`: fixed 5.5 V regulator on TLMM gpio61 (active high, 233 us ramp). Nothing in the
  downstream display driver requests it; it was left on by the bootloader.
- LCDB (pm8150l, 0xec00): LDO 5.7-5.8 V (110 mA limit), NCP 5.7-5.8 V (260 mA), boost 4.8-6.3 V (600 mA);
  these are the panel's `lab`/`ibb` supplies. There is no mainline driver for the Qualcomm LCDB block
  (inferred; not re-checked against 6.18).
- Arc levels (RPMh corner numbers: 16 retention, 64 min-svs, 128 low-svs, 256 svs, 384 nom, 416 turbo) become
  `rpmhpd` in mainline.

## 3. Thermal

### Sensors

- Two TSENS blocks (`tsens@c222000`, `tsens@c223000`, `qcom,tsens24xx`), matching mainline `tsens0/tsens1`.
  tsens0: aoss0, cpu-0-0..3 (silver), cpuss-0/1, cpu-1-0..7 (gold and prime), gpuss-0. tsens1: aoss-1, cwlan,
  video, ddr, q6-hvx, camera, cmpss, npu, gpuss-1. Prime-core zones use cooling device `cpu@700`, gold
  zones `cpu@400`. (confirmed from the zone properties; physical identity inferred from zone names)
- ADC_TM5 thermistors on pm8150 (xo, imu, rf-pa), pm8150b (battery, pcb1-3) and pm8150l (four camera
  thermistors), all ratiometric with 200 us settling.
- BCL (battery current limit) on pm8150b and pm8150l, consumed by the stock userspace; no mainline driver
  is used here.
- **Skin temperature** in the stock system is a virtual sensor (`oculus,virtual-sensor`) combining
  thermistors with a linear model; it was disabled on the captured boot and is not implemented here.

### Downstream trips that matter

| Zone class | Trips | Cooling |
|---|---|---|
| CPU, CPU subsystem and GPU "fan" zones (18) | 70 C (hyst 10), 80 C (7), 90 C (7) | fan states 4..5 / 4..6 / 4..7 |
| `batt-fan` | 45 / 50 / 55 C | fan; looks broken downstream (reports tenths of a degree against millidegree trips, so it cannot trip) |
| `fp-fan` (skin virtual sensor) | 40 / 44 / 50 C | fan |
| gold and prime CPU `-limit` zones | passive 90 C (hyst 5) | cpufreq clamp (gold state 0..10, prime 0..17) |
| all CPU `-step` zones | 75 C notify, 110 C core isolate | cpu-isolate (core hot-unplug) |
| video/camera/cwlan/ddr/... `-step` zones | passive 100 C | GPU, CDSP and NPU limits |

Downstream governors `single_step` and `low_limits_cap` are Qualcomm-only and do not exist in mainline. The
project DTS uses `step_wise` with the same trip temperatures; see [cpufreq-thermal.md](cpufreq-thermal.md).

## 4. Fan

| Item | Fact |
|---|---|
| PWM | pm8150l LPG block at 0xbc00, channel index 1, period 40000 ns (25 kHz). pm8150l gpio10, function 1 |
| Tach | TLMM gpio64, input with pull-up, rising-edge interrupt. Two pulses per revolution (strong, derived from the driver) |
| Supply | pm8150l BOB (3.312 V). The downstream driver enables the supply and waits 100 ms before driving PWM |
| Speed model | downstream `max-rpm` 7000; `cooling-levels` {0..7000 in steps of 1000} are target RPM |
| Control | downstream driver is closed-loop on the tach (`oculus-linux-kernel: drivers/staging/oculus/pwm-tach-fan.c`); cold-start PWM 84/255, clamps 15..255 |
| Display gating | downstream the fan is held at 0 RPM whenever the panel is off (`oculus,silent-rpm` is absent from the DT) |
| Thermal binding | Downstream the fan never goes below state 4 (4000 RPM) once any zone is registered; trips above |

Observed on the device: the stock fan runs at about 4000 RPM at idle with the display on. On mainline the
fan is driven from the LPG PWM with tach readback and a thermal binding; at full duty it reported about
8.7 krpm. (observed)

## 5. Charger, gauge and battery

| Device | Facts |
|---|---|
| SMB5 charger (pm8150b) | Downstream driver `qcom,qpnp-smb5`. Fast-charge current max 2.0 A, USB input 3.0 A, termination -180 mA, step charging and JEITA on, auto-recharge at 98 %, thermal mitigation steps 3.0 / 1.5 / 1.0 / 0.5 A. No secondary charger |
| FG-GEN4 (pm8150b) | Downstream battery gauge, five-pin battery, cutoff 3.4 V, empty 2.9 V, five battery-ID profiles (15 / 25 / 50 / 100 / 124 kohm, 3636-3710 mAh nominal, 4.4 V max). Observed: 100 % at 4.32 V, full-charge capacity 3732 mAh, design 3743 mAh |
| Qnovo5 | Pulse-charging block in pm8150b |
| USB PD PHY (pm8150b) | `qpnp-pdphy`; default sink capabilities 5 V 3 A and 9 V 3 A (order unverified) |
| TI bq27z561 | I2C `i2c@990000`, address 0x55. See [battery-gauge.md](battery-gauge.md) |
| Dock / external battery | Vendor DT nodes `oculus,charging_dock` and `oculus,external_battery`; purpose inferred (strap or dock accessories) |

This project does not run any charging software. **The battery does not charge while Linux runs.** See
[../safety.md](../safety.md).

## 6. CPU clusters

| Cluster | MPIDR | Core | Max kHz | Steps | capacity-dmips-mhz |
|---|---|---|---|---|---|
| silver | 0x000-0x300 | Kryo 4xx silver (A55-derived) | 1,804,800 | 17 (300000 ... 1804800) | 1024 |
| gold | 0x400-0x600 | Cortex-A77 class (r1p0) | 2,419,200 | 18 (710400 ... 2419200) | 1894 |
| prime | 0x700 | Cortex-A77 class | 2,841,600 | 20 (844800 ... 2841600) | 1894 |

All use PSCI (`arm,psci-1.0`, method `smc`). Frequency is controlled by the hardware cpufreq block
(`qcom,cpufreq-hw-epss` at 0x18591000/0x18592000/0x18593000, dcvsh interrupts SPI 30/31/19). Per-step
voltages are in the hardware lookup table, not in the DT. Idle states downstream (`qcom,lpm-levels`):
cluster `l3-wfi` and `llcc-off` (PSCI 0xc24), CPU `wfi` and `rail-pc` (PSCI 0x4). The downstream capture used
`lpm_levels.sleep_disabled=1`.

## 7. Watchdog, reset and power key

- SoC watchdog `qcom,wdt@17c10000` (bark SPI 0, bite SPI 1; downstream bark 12 s, pet 9.36 s). The kexec
  loader leaves it in a state the kernel can adopt; see [../booting.md](../booting.md).
- Power key: PON on pm8150 (`kpdpwr` = KEY_POWER, `resin` = KEY_VOLUMEDOWN), plus a `volume_up` GPIO key.
- SoC reset reason can be read from the PMIC PON registers (reported after a return to Android; see
  [../booting.md](../booting.md)).
- `PS_HOLD` is at 0xc264000. The mainline return-to-Android driver uses it; see
  [../booting.md](../booting.md).

## 8. What is not known

- The RPM-to-PWM curve of the fan (only cold-start PWM 84 and full duty 255 are known).
- The skin-temperature model coefficients.
- Whether bq27z561 is the primary gauge or only the external-battery gauge (the downstream `battery`
  supply is the PMIC gauge).
- Which regulators Android leaves on before the jump (state caveat above).
