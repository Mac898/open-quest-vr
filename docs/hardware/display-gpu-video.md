# Display and video decoder hardware

Facts about the panel, the DSI links, the display controller and the Venus video decoder, taken from the
downstream device tree and kernel source (`oculus-linux-kernel`, public) and from observation of a booted
downstream kernel. For the GPU see [gpu.md](gpu.md).

**Current status on mainline:** none of the display or video nodes is enabled in this project's DTS. The
display does not work yet. This page is a hardware reference only.

Confidence labels: *confirmed* = in the booted DT or observed; *strong* = read from downstream source that
parses the DT; *inferred* = reasoning.

Board identity: model `"Hollywood (DVT)"`, `qcom,msm-id = <0x164 0x20001>` (SM8250 v2.1),
`qcom,board-id = <0x40 0x11>`. (confirmed)

## Summary of surprises

1. **There is no `lt9611uxc` bridge.** DSI goes straight to the panel. The `lt9611uxc.cont_splash_en=1`
   argument seen on the downstream command line is a leftover from the Qualcomm reference board. (confirmed
   absence; origin inferred)
2. **The panel is a dual-DSI, DSC 3:1, 1920x3664 portrait video-mode LCD**, 120 Hz native with dynamic
   refresh down to 60 Hz. DT timing numbers are per DSI link (960 pixels wide); the DRM mode is twice that.
3. **The backlight is a strobed (low-persistence) LED unit controlled entirely by a vendor DCS command
   (0xB9)** sent to the panel over DSI. There is no PWM, WLED or I2C backlight IC in the display path. A
   separate 3.3 V "blu-mcu" rail powers an MCU on the backlight unit; it has its own firmware-flash driver
   downstream, which is a risky area (see [../safety.md](../safety.md)).
4. **The panel's on-command sends display-on (0x29) before sleep-out (0x11)**, each followed by 120 ms.
   This is verbatim from the DT.
5. **AVDD (5.5 V, TLMM gpio61) has no consumer downstream.** It is a boot-on fixed regulator that the
   bootloader leaves on. The downstream kernel disabled it as unused about 32 s after boot, so a mainline
   panel driver must own it explicitly.
6. About 21 panel variants (Sharp, BOE, JDI, JDI-NVT at several rates) are described in the downstream DT.
   The one in use is selected by the bootloader command line (`msm_drm.dsi_display0=...sharp_4k_120Hz_dsc_dvt_video`).
   The DT's built-in default is a different (JDI) panel, so a given unit's panel identity is only known from
   the boot arguments. Retail units may carry another vendor's panel. (confirmed for the one DVT unit)

## 1. DSI links, PHY and PLL

| Item | Value |
|---|---|
| Controllers | `mdss_dsi_ctrl0@ae94000`, `ctrl1@ae96000` (`qcom,dsi-ctrl-hw-v2.4`) |
| PHYs | `mdss_dsi_phy0@ae94400`, `phy1@ae96400` (`qcom,dsi-phy-v4.1`, 7 nm D-PHY) |
| PLLs | 7 nm v4.1 PLLs at 0xae94900 / 0xae96900, spread-spectrum enabled, down-spread |
| Mode | Dual-DSI **bonded** pair; ctrl0/PLL0 is the clock master. Commands without a unicast flag are broadcast to both links |
| Lanes | 4 data lanes per link, D-PHY, RGB888 |
| Traffic | Non-burst, sync-event video mode, virtual channel 0, LP during BLLP/EOF |
| Supplies | controller 1.2 V from pm8150_l9, PHY 0.9 V from pm8150_l5 (880000 uV), refgen regulator |
| Bit clock (computed) | about 4.586 Gb/s per link, about 1.147 Gb/s per lane, byte clock about 143.3 MHz. Not read from a live clock (inferred) |
| Dynamic refresh | by vertical-front-porch stretch (`dfps_immediate_porch_mode_vfp`), 120-60 Hz |

## 2. Panel (`mdss_dsi_sharp_4k_120Hz_dsc_dvt_video`)

### Timings (per DSI link)

| Field | Value |
|---|---|
| Width x height | 960 x 3664 per link (DRM mode 1920 x 3664) |
| H front porch / pulse / back porch | 56 / 12 / 44 (per link; 112 / 24 / 88 for the DRM mode) |
| V front porch / pulse / back porch | 14 / 1 / 7 |
| Refresh | 120 Hz, min 60 Hz |
| Pixel clock (derived) | about 948.3 MHz uncompressed (2144 x 3686 x 120) |

The panel is physically portrait; the Quest 2 eye buffers are 1832x1920, so 3664 = 2 x 1832 lines, one eye
per half along the long axis.

### DSC

8 bits per component, 8 bits per pixel (3:1), block prediction on, slice 480 x 16, 2 slices per packet,
topology 2 layer mixers / 2 DSC encoders / 2 interfaces. The panel receives its DSC parameter set through a
vendor register (0xE6) as part of the on-command; the PPS declares a picture width of 1920 (full width).
(confirmed from the DT; decoded by hand)

### On-command sequence (from the DT, in low-power mode)

| # | Type | Wait | Payload | Meaning |
|---|---|---|---|---|
| 1 | generic short | - | `b0 00` | unlock command page |
| 2-5 | generic long | - | `f1 00`, `ee 04`, `f1 12`, `c6 40` | vendor registers (undocumented) |
| 6 | generic long | - | `b9 0c f4 01 27 00 00 00 00 05 b4 01 27 00 00 00 00` | backlight strobe timing (right start 3316, period 295; left start 1460, period 295) |
| 7-8 | generic long | - | `c0 47 86 0d 00 0e 50 00 09`, `d6 00` | vendor registers |
| 9 | generic long | - | `e6` + 124 bytes | DSC parameter set written into the panel |
| 10 | DCS long | - | `35 00` | tear effect on (V-blank) |
| 11 | DCS long | - | `03 01` | vendor |
| 12 | DCS short | 120 ms | `29` | display on |
| 13 | DCS short | 120 ms | `11` | exit sleep |
| 14 | generic short | - | `b0 03` | lock command page |

Semantic labels of vendor registers are inferred (no panel datasheet). Off-command: `28` (display off,
120 ms), `34` (tear off), `10` (sleep in, 120 ms), in high-speed mode. Before the off-command the backlight
level is written as 0.

### Reset, enable and power sequence

- Reset GPIO TLMM 6: low for 5 ms, then high for 25 ms. Enable GPIO ("disp_en") TLMM 135. Tear-effect input
  gpio66 (function `mdp_vsync`).
- Supply enable order (downstream driver, disable is the reverse): `vddio` (pm8150_l14, 1.8 V, 20 ms after) ->
  `ibb` (LCDB NCP, 5.7 V, 20 ms after) -> `lab` (LCDB LDO, 5.7 V) -> `blu-mcu` (pm8150a_l11, 3.3 V).
- Power-on flow: enable supplies in that order, set pin states, raise `disp_en`, run the reset sequence, bring
  up the DSI link in LP mode, send the on-command, then write the backlight command.
- Where AVDD (gpio61) sits relative to these is not defined by the downstream driver (inferred: the
  bootloader leaves it on).

### Backlight / strobe

- Control type `bl_ctrl_jdi` (also on the Sharp panel). Range 800..1200, default duty 80, max scanline offset
  1200.
- Mechanism (downstream `techpack/display/msm/dsi/dsi_panel.c`): from the level, duty, total lines and
  porches it computes a start line and a pulse length for each eye and sends them as DCS write 0xB9 (eight
  big-endian 16-bit values) to **both** links. At 120 Hz and level 1000 the illumination is about 294 lines
  (about 8 % of the 3686-line frame). A refresh-rate change must resend the timing.
- The mapping from the 0..255 sysfs brightness to the 800..1200 level was not checked. (open)

## 3. Display controller (MDSS / SDE = DPU 6.0)

`mdss_mdp@ae00000` (`qcom,sde-kms`, hardware revision 0x60000000, matches mainline `qcom,sm8250-dpu`).

| Item | Value |
|---|---|
| Layer mixers | 6, 11 blend stages |
| Source pipes | 8 (4 VIG + 4 DMA), max line width 4096 |
| DSC | 4 blocks at 0x81000-0x81c00 |
| Interfaces | 4 (0x6b000 DP, 0x6b800 DSI, 0x6c000 DSI, 0x6c800 DP), same as the mainline sm8250 catalogue |
| Other | UBWC 4.0, highest bank bit 3, core clock max 460 MHz |

A reserved 35 MiB continuous-splash region and a 1 MiB dynamic-fps region exist in the downstream DT. When the downstream kernel was kexec-started the splash was not adopted, and the display
rails (LCDB) were off.

## 4. Video decoder (Venus, "kona-vidc")

| Item | Value |
|---|---|
| Node | `vidc@aa00000`, IRIS2 / VPU 2, 4 VPP pipes |
| Clocks and power | `gcc_video_axi0`, videocc core and vcodec clocks, `mvs0` and `mvs0c` GDSCs; allowed rates 240 / 338 / 366 / 444 MHz |
| Firmware | Remote-processor image named `venus` (a 5 MiB reserved region). The blob is device-signed and is **not distributed here** |
| Decode | H.264, HEVC, VP8, VP9, MPEG-2; up to 8192x8192, 138,240 macroblocks per frame, up to 960 fps, 220 Mb/s; UBWC output |
| Encode | H.264, HEVC, VP8 |

Upstream, `qcom,sm8250-venus` is handled by the `venus` driver (see [../upstream-state.md](../upstream-state.md)).
No decoder works on mainline here yet.

## 5. Open questions

- Sequencing of AVDD against the other panel supplies; panel datasheet for the vendor registers.
- Actual DSI bit clock (only computed).
- Which panel vendor and revision retail units carry.
- The sysfs brightness mapping.
