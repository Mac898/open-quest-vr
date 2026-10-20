# LED diagnostic code

When the headset boots Linux but the USB link never comes up, nobody can reach a shell. The initramfs init
(`rootfs/mainline-init/init.sh`, see [rootfs.md](rootfs.md)) then plays a **blink code on the front status LED**
that reports what the kernel managed to bring up, and finally resets the device. You film the LED and decode
the video with `tools/led_code.py` (host side, Python).

The code, its timings and the decoder were exercised in tests (fake sysfs trees, the init under qemu-user).
The device names it reads were derived from the DT; they were not all seen on a device when this was written
(strong). The raw names are also logged to the kernel log so you can check them.

## 1. Front LED stages (always on)

| LED | Meaning |
|---|---|
| blue heartbeat | kernel is alive (DT default trigger, set by the LED driver) |
| red solid | init running, waiting for a USB device controller (UDC) |
| green solid | gadget bound to a UDC, waiting for the host to configure it |
| green heartbeat | host configured the gadget: telnet is ready |
| red fast blink (100/100 ms) | a reset is armed; the watchdog bites about 30 s later |
| red slow blink (1 s) | a reset is wanted but there is no watchdog device: long-press power |

LED writes are done by a separate background process, so a failing or stuck LED write can never delay init
or a reset.

## 2. When the code plays

The code plays only when nobody can reach the shell:

| Path | When | Then |
|---|---|---|
| 1 | no UDC 60 s after the UDC wait started | 3 cycles of the code, then the usual reset (red fast blink, bite about 30 s later) |
| 2 | UDC bound but the host never configured it within 120 s | same |
| 4 | a reset is wanted but there is no `qcom_wdt` | the code alternates with a 20 s slow red blink until you long-press power |
| 3 | dead-man timer expired (no heartbeat from the host) | **no code**; the reset is immediate |

There is no code when the link comes up. If there is no red/green/blue LED, or every LED write fails, the
code is skipped and the reset is not delayed. Whenever the player runs (paths 1, 2, 4) it also logs one
`usbdiag:` line to the kernel log (section 5).

## 3. Filming

The code needs a video of the LED with the camera's exposure and focus fixed. It starts about 60 s after the
LED turns red (init start) on the "no UDC" path and 120 s after the green LED appears on the "not configured"
path; a typical code takes about 4-4.5 minutes in total (at most 8.5). Three cycles are played so one can be
lost; one complete cycle is enough to decode.

## 4. Symbols and fields

All channels run at half brightness (255 of 511) during the code. **White** = red + green + blue.

| Symbol | Looks like | Meaning |
|---|---|---|
| START | white 2 s, then 1.5 s dark | new cycle |
| SEP | short white (0.25 s), then 1 s dark | end of a field |
| digit 1-9 | that many short flashes (0.25 s on/off) in the field colour | the digit |
| digit 0 | one long flash (1 s) | zero |
| between digits | at least 1 s dark, or a colour change | next digit |
| FLAG | one 0.4 s flash: **green** = device bound to a driver, **blue** = device exists, no driver (deferred or failed), **red** = no such device | |
| END | 3 s dark | then START again |

A cycle: `START F1 SEP F2 SEP F3 SEP F4 SEP F5 SEP F6 SEP F7 SEP F9 SEP F8 END` (about 76-89 s).

| Field | Colour | Content |
|---|---|---|
| F1 | red, 1 digit | path (section 2) |
| F2 | 12 flags | 1 cmd-db, 2 rpmh-rsc, 3 RPMh clocks, 4 RPMh regulators (PM8150), 5 BCM voter, 6 GCC, 7 aggre1 NoC, 8 HS PHY `88e3000.phy`, 9 dwc3 glue `a6f8800.usb`, 10 dwc3 core `a600000.usb`, 11 QMP PHY (disabled, red expected), 12 UDC (green = bound, blue = a UDC exists but binding failed, red = none) |
| F3 | blue, 2 digits | number of deferred devices (capped at 98; 99 = could not read) |
| F4, F5, F6 | green 2 digits, red 1 digit, blue 2 digits | the first three deferred devices: device index, reason category, supplier index (`00 0 00` = none) |
| F7 | green, 3 digits | whole seconds of uptime when the init started (capped at 999) |
| F9 | blue, 3 digits | USB discriminators (section 4.1) |
| F8 | red, 1 digit | checksum: (F1 + flag values [red 0, green 1, blue 2] + every digit of F3-F7 and F9) mod 10 |

Device and supplier index (99 = not in the table, 00 = none): 01 cmd-db, 02 rpmh-rsc, 03 RPMh clocks,
04 RPMh regulators-0 (PM8150), 05 regulators-1 (BOB), 06 BCM voter, 07 GCC, 08 config NoC, 09 system NoC,
10 mc_virt, 11 aggre1 NoC, 12 dc NoC, 13 gem NoC, 14 HS PHY, 15 dwc3 glue, 16 dwc3 core, 17 QMP PHY, 18 fan,
19 TLMM, 20 PDC, 21 SPMI, 22 PM8150 GPIO, 23 LPG, 24/25 tsens, 26 watchdog, 27 ramoops, 28 psci. The full
names are in `tools/led_code.py` (`TABLE`).

Reason category: 0 empty, 1 waits for a DT supplier, 2 waits for a device-link supplier (supplier index = that
device), 3 regulator/supply, 4 clock, 5 PHY, 6 interconnect, 7 power domain, 8 Command DB/RPMh, 9 other.

### 4.1 F9: USB discriminators

Read-only sources: the first `/proc/interrupts` line ending in `dwc3`, `/sys/class/udc/a600000.usb/state`,
and the dwc3 `regdump` debugfs file (read once per boot, with a 5 s limit).

| Digit | Value |
|---|---|
| F9.1 dwc3 IRQ count | 0 = 0; 1 = 1-9; 2 = 10-999; 3 = 1000 or more; 9 = no `dwc3` line or unreadable |
| F9.2 UDC state | 0 = not attached / attached / powered; 1 = default; 2 = addressed; 3 = configured; 4 = suspended; 9 = unreadable |
| F9.3 dwc3 registers | 0 = event count 0 and no bus error; 1 = events counted, no bus error; 2 = bus error, count 0; 3 = both; 9 = regdump unreadable |

| F9 | Reading |
|---|---|
| `x x 2` or `x x 3` | AXI bus error on dwc3 DMA; the `BUSERR=` field of the `usbdiag:` line is the failing address |
| `0 0 0` | no event in memory, no IRQ: the event-buffer DMA stalled (the signature of a stale SMMU translation for the USB controller, see [design-decisions.md](design-decisions.md)) |
| `0 0 1` | events counted but no IRQ delivered |
| `1 or more, 0, x` | IRQs arrive but no reset event was understood: events landed somewhere else |
| `1 or more, 1-2, 0` | events work; endpoint-0 data path or PHY problem |
| `2 or more, 3, 0` | configured (you would not see a code then) |
| any 9 | that source was unreadable; on path 1 (no UDC) `9 9 9` is normal |

### 4.2 The `usbdiag:` line

```
lvr-init: [122s] usbdiag: t=122 irq=<n|na> state=<s> speed=<s> GSTS=<hex> BUSERR=<hi:lo> EVADR=<hi:lo> EVSIZ=<hex> EVCNT=<hex> DCTL=<hex> DEVTEN=<hex> DSTS=<hex> f9=<F9> regdump=<ok|missing|timeout|error|busy>
```

`na` means unavailable. `EVSIZ` bit 31 = event interrupt masked; `DCTL` bit 31 = run/stop; `DSTS` bits [21:18] link
state and [2:0] speed.

## 5. Example

A device on path 2 with everything bound and no USB events (`F9 = 0 0 0`):

```
WHITE 2 s                                  START
red ..                                     F1 = 2   UDC bound, host never configured it
G G G G G G G G G G R G                    F2       all bound; QMP PHY absent (expected)
blue - blue -                              F3 = 00  nothing deferred
(green - green - red - blue - blue -) x3   F4-F6 = 00 0 00
green - green - green .                    F7 = 001 init started at uptime 1 s
blue - blue - blue -                       F9 = 000
red ....                                   F8 = 4   checksum
dark 3 s                                   END
```

(`.` short flash, `-` long flash; each field is followed by a SEP.) Checksum: 2 + (10 x 1 + 0 + 1) + 0 + 0 + 0 +
1 + 0 = 14, so F8 = 4. The kernel log carries the same information:

```
lvr-init: [122s] ledcode v2 path=2 flags=GGGGGGGGGGRG ndef=0 d=00.0.00,00.0.00,00.0.00 up=1 usb=000 sum=4
```

## 6. Decoding a video

```
python3 -I tools/led_code.py video CLIP.mp4 --roi X,Y,W,H [--dump frames.csv]
```

`--roi` is a box around the LED in pixels. The tool uses ffmpeg to get the mean colour of that box for every
frame, sets a threshold per channel halfway between its darkest and brightest value (override with
`--thr R,G,B`), classifies each frame as dark/red/green/blue/white, and decodes every cycle that starts with
a START. It prints each field with device names and `checksum OK` when F8 matches. If colours come out wrong
(camera white balance), look at `--dump` and set `--thr`.

Without video, write colour changes by hand (seconds, colour from then on) and run
`tools/led_code.py decode EVENTS.txt`:

```
0.00 white
2.00 dark
3.50 red
3.75 dark
```

`tools/led_code.py encode --path 1 --flags GBRRRBBBBRRR --ndef 10 --entries 07.1.03,11.0.00,14.1.03 --up 63 --usb 999`
prints the exact timeline for any code, for comparison.

## 7. Timing

The code delays the reset by at most 510 s, and never later than 60 s before the dead-man deadline
(`lvr_deadman`, default 600 s, minimum 120 s). With `lvr_deadman=120` there is no time for the code and the
reset comes as before. Typical reset times on a USB-only boot (init at uptime 0-2 s): about uptime 351-356 s
on the "not configured" path (bite 30 s later), about 314 s on the "no UDC" path.

The init reads only read-only sources: platform device `driver` links, `/sys/class/udc`, `/proc/interrupts`,
and the `devices_deferred` and dwc3 `regdump` files on a read-only debugfs mount. It never reads
`/sys/kernel/debug/pinctrl` or `/sys/kernel/debug/gpio`, or any other dwc3 debugfs file (some of those write
debug-mux registers on read).
