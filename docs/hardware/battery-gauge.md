# Battery gauge (TI bq27z561), read-only

This project reads the battery through the TI **bq27z561** fuel gauge on I2C, strictly read-only. There is no
charger or PMIC-gauge driver: the battery does not charge while Linux runs (see [../safety.md](../safety.md)).

## The hardware

| Fact | Value | Confidence |
|---|---|---|
| Device | `fuel-gauge@55`, `compatible = "ti,bq27z561"`, address 0x55; no interrupt, no GPIO, no supply in the DT | confirmed |
| Bus | `i2c@990000` = GENI serial engine 4 in QUP wrapper 0, 100 kHz, pins gpio8 (SDA) / gpio9 (SCL) function `qup4`. It is the only device on that bus | confirmed |
| Downstream driver | a customised `bq27xxx_battery` (`oculus-linux-kernel: drivers/power/supply/bq27xxx_battery.c`) with extra vendor sysfs paths | confirmed |
| Other gauge | the downstream `battery`/`bms` power supplies are the PMIC charger and FG-GEN4 gauge on pm8150b, not the bq27z561. Which gauge is "primary" is unknown | confirmed (gap) |

Observed on a downstream-kernel boot: the bq27z561 registered and reported a temperature (31.7 C), but its
`charge_now` read failed with -22 there.

## The mainline driver

The upstream `bq27xxx_battery` + `bq27xxx_battery_i2c` drivers support `ti,bq27z561`. Registers read: TEMP
0x06, VOLT 0x08, FLAGS 0x0a, RC 0x10, FCC 0x12, AI 0x14 (average current), TTE 0x16, TTF 0x18, AP 0x22 (average
power), CYCT 0x2a, SOC 0x2c, DCAP 0x3c. Properties: status, voltage, current, capacity, temperature, charge
now/full/design, cycle count, power, health. Units are uV, uA (negative = discharge), tenths of a degree C, uAh.

**There is no write path to the gauge in this configuration** (source audit of 6.18):

- Every write to the chip goes through the driver's unseal/config-update functions, reachable only from
  `bq27xxx_battery_settings()`. That function returns immediately unless the DT node has a
  `monitored-battery` property, and again if the chip's data-memory table is NULL, which it is for the
  z561. So the project DTS leaves `monitored-battery` off this node.
- The only bus writes are the one-byte register pointer before each read. The kernel config has
  `BATTERY_BQ27XXX_DT_UPDATES_NVM` off and `I2C_CHARDEV` off, so there is no userspace `/dev/i2c-*` path
  to the bus.
- Transfers are 1-2 bytes, so the serial engine uses FIFO mode; no DMA mapping is involved.

## How it is wired in the project DTS

- `gpio-reserved-ranges` for TLMM is a deny-by-default list: pins that nothing uses are marked reserved so gpiolib never
  reads their control registers, and gpio8 and gpio9 are carved out for this bus. The list is in the DTS.
- QUP wrapper 0 and `i2c4` are enabled with `clock-frequency = <100000>`; `dmas`, `power-domains`
  and the wrapper's `iommus` are deleted for FIFO-only operation. The serial engine keeps its `qup-core` and
  `qup-config` interconnect votes and has no memory path.
- The adapter is `i2c-4` (from the `i2c4` alias in `sm8250.dtsi`), device `4-0055`, power supply
  `bq27z561-0`.

Quick check on the device:

```sh
P=/sys/class/power_supply/bq27z561-0
cat $P/uevent
for f in status present voltage_now current_now capacity temp charge_now charge_full cycle_count; do
  echo "$f=$(cat $P/$f 2>&1)"; done
```

`i2cset`, `i2ctransfer` and writes to `power_supply` attributes are not part of this setup; the stage-2
rootfs and the initramfs leave the I2C tools out.

## What the numbers mean

- **Voltage, temperature and capacity** are meaningful.
- **`current_now` read 0** on mainline while USB powered the headset and the battery was full (and `status`
  read "Not charging"). The driver reads the same register (0x14) with the same scaling as downstream, and both
  AverageCurrent and AveragePower read 0, so this is what the gauge reported, not a parse error. Likely
  explanations, none confirmed: genuinely about zero net battery current on USB power at full charge; a small
  gauge dead-band (a few mA); or a gauge sleep state.
- Because USB is always connected during a session, true battery drain is masked. A real system-power figure
  needs a host-side USB power meter. The downstream kernel showed about -157 mA at idle on the PMIC gauge,
  although its status said "Charging" (an observed mismatch; the downstream status string is misleading).
