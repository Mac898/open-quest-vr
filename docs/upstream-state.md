# Upstream support for this hardware

A reference survey of what mainline Linux and the community already provide for the Quest 2's SoC and
peripherals. **As of 2026-10-11.** Code facts come from a Linux 6.18.55 tree and Mesa from 2026-10-10 and are
cited by file; web claims carry links. Status tags: MERGED / IN-REVIEW / OUT-OF-TREE / NONE. Web search returned
thin results in places; those are marked "unverified". Re-check before relying on a negative result.

## 1. Mainline SM8250 in 6.18.y

**Boards (merged).** `arch/arm64/boot/dts/qcom/`: `sm8250-hdk.dts`, `sm8250-mtp.dts`,
`sm8250-samsung-{r8q,x1q}` (+ common), `sm8250-sony-xperia-edo-pdx203/206` (+ `edo.dtsi`),
`sm8250-xiaomi-elish-{boe,csot}` (+ common), `sm8250-xiaomi-pipa`, and the base `sm8250.dtsi`. There is **no
Quest or Oculus/Meta board** (searched DTS and drivers). Useful references:

- `sm8250-mtp.dts`: the Qualcomm reference board (GPU/GMU with `a650_zap.mbn`, remoteprocs, Venus).
- `sm8250-xiaomi-elish-common.dtsi`: a dual-DSI panel pair and PCIe0 with a WLAN/pmu node.
- `sm8250-sony-xperia-edo.dtsi`: PCIe0/PCIe2 enablement.

XR2 is an SM8250 derivative, so `sm8250.dtsi` is the right base. A board still needs its own panel node(s),
TLMM/pinctrl, PMIC rails and regulators.

**DPU / DSI / DSC (MERGED).** `drivers/gpu/drm/msm/disp/dpu1/catalog/dpu_6_0_sm8250.h` defines `dsc_0..dsc_3`;
`dsi/dsi_host.c` populates DSC parameters (`dsi_populate_dsc_params`); the 7 nm DSI PHY (`dsi_phy_7nm.c`) covers
SM8250. `sm8250-mdss` is in `qcom_smmu_client_of_match` (`arm-smmu-qcom.c`). Dual-DSI is exercised by the
elish boards. DSC on SM8250 is mostly exercised by phone panels.

**UBWC.** `drivers/soc/qcom/ubwc_config.c` has `sm8250_data`, and the DPU honours UBWC.

**Adreno 650 + GMU (MERGED).** `adreno/a6xx_catalog.c`: chip id `0x06050002`, `a650_sqe.fw`, `a650_gmu.bin`,
`a650_protect`; `sm8250.dtsi` has `qcom,adreno-gmu-650.2` and an adreno SMMU. Firmware (SQE, GMU, zap) comes
from `linux-firmware` or from the device; the zap shader is signed for the device (see
[hardware/gpu.md](hardware/gpu.md)).

**Video: venus and iris (MERGED, both).** In 6.18 `qcom,sm8250-venus` is matched by **venus**
(`venus/core.c`, `sm8250_res`). **iris** also carries SM8250 support (`iris_platform_sm8250.c`) but is only
compiled when `CONFIG_VIDEO_QCOM_VENUS` is off, so one of the two must be chosen. The decoder needs the
Venus firmware (`qcom/vpu-1.0/venus.mbn` family; confirm the file name on the target).

**CAMSS (MERGED).** `camss.c`: `qcom,sm8250-camss` (`CAMSS_8250`, csid-gen2 + vfe-480 + csiphy). It provides a
raw RDI capture path; there is no ISP/3A upstream. Sensors need their own V4L2 sensor drivers; the OV7251 has
one in tree (`drivers/media/i2c/ov7251.c`), but its use on four syncboss-triggered cameras is untested. Not
verified for global-shutter mono sensors on any SM8250 board.

**ath11k QCA6390 over PCIe (MERGED).** `ath11k/pci.c` handles QCA6390 (`pci17cb,1101`) and WCN6855
(`pci17cb,1103`) per `Documentation/devicetree/bindings/net/wireless/qcom,ath11k-pci.yaml`. SM8250 PCIe0/PCIe2
are enabled on the xperia-edo and elish boards. It needs board data and `amss.bin`/`m3.bin` firmware. The
Quest 2's Wi-Fi/Bluetooth chip is a QCA6390 (see [hardware/wifi.md](hardware/wifi.md)).

**fastrpc (MERGED).** `drivers/misc/fastrpc.c` plus fastrpc nodes for adsp, cdsp and slpi in `sm8250.dtsi`.

**kexec-related notes.**

- `arm-smmu.c: arm_smmu_device_shutdown` puts the SMMU into bypass on shutdown; a long-standing question asks
  why ([lore thread](https://lore.kernel.org/lkml/ZfNKv70oqqwMwIeS@sequoia/T/)). When the first kernel is
  Android (vendor), expect stale stream-match and context-bank state and in-flight DMA when the new kernel's
  `arm-smmu` probes; the `disable_bypass` default faults unknown stream IDs.
- Bootloader-mapping handover series by Bjorn Andersson (2020, tested on SM8250 with a splash screen):
  [LKML](https://lkml.iu.edu/hypermail/linux/kernel/2007.2/01304.html),
  [RFC 2019](https://lkml.iu.edu/1906.1/05084.html). In current mainline the Qualcomm implementation handles
  inherited mappings through the qcom impl, not a generic skip-init. This project's own approach is in
  [design-decisions.md](design-decisions.md) section 4.

## 2. Existing Quest / headset Linux work

**No mainline or postmarketOS port of the Quest 1/2/Pro/3 found (NONE).** Searches of postmarketOS devices, the
kernel DTS tree and the web found no device page. (Web search is limited; re-check the pmOS wiki and
linux-arm-msm lore directly before claiming novelty.)

Out-of-tree work:

- **Quest KeXec (QKX)**: a kernel module on a rooted headset that kexecs into another kernel without unlocking
  the bootloader. Upstream: [YusufOzmen01/quest-kexec](https://github.com/YusufOzmen01/quest-kexec); the fork
  used by this project: [Mac898/quest-kexec](https://github.com/Mac898/quest-kexec); another fork:
  [machachu56/quest-kexec-q2](https://github.com/machachu56/quest-kexec-q2) (name suggests a Quest 2 variant,
  not inspected). The upstream README describes testing on the Quest Pro.
- **Windows PE on Quest 2** (June 2024): a UEFI image from Mu-Silicium booted from USB on a bootloader-unlocked
  Quest 2; display/GPU not described ([akersys.com](https://akersys.com/posts/running-windows-pe-natively-on-meta-vr-headset/)).
  The post calls the SoC "rebranded SM8150/855"; the Adreno 650 and the device tree say SM8250-class, so treat
  that claim as wrong.

## 3. Mesa Turnip on Adreno 650

- **Conformance.** Turnip (A618) was certified Vulkan 1.1 in 2021
  ([Phoronix](https://www.phoronix.com/news/TURNIP-Vulkan-1.1-Conformance),
  [Igalia](https://igalia.com/2021/12/04/Open-Source-Adreno-Driver-Achieves-Vulkan-1.1-Conformance.html)).
  Collabora (10 Sep 2026) says Turnip on A6xx is now officially Vulkan 1.3 conformant, including a run on
  Android, but does not name the A650
  ([Collabora](https://www.collabora.com/news-and-blog/news-and-events/mesa-on-android-how-we-brought-turnip,-skiavk,-and-cuttlefish-to-ci.html)).
  Check the Khronos Conformant Products list for A650 specifically. In the local Mesa tree, CI has dEQP lanes for
  a618, a660, a702 and a750 only (`src/freedreno/ci/`): there is no a650 lane, so the A650 is less tested than
  its neighbours.
- **UBWC / modifiers.** `tu_image.cc` handles `DRM_FORMAT_MOD_QCOM_COMPRESSED` and `DRM_FORMAT_MOD_LINEAR` for
  explicit-modifier images. For KMS the DPU must advertise the same modifier (not checked).
- **Display WSI.** `VK_KHR_display` and `EXT_acquire_drm_display` are present when Turnip is built with the
  display platform enabled.
- **GMU/firmware.** Needs `a650_sqe.fw`, `a650_gmu.bin` and the zap shader.

## Summary

Every SM8250 SoC driver this hardware needs is already in 6.18.55; the missing pieces are the board device tree,
the panel driver, regulator and power wiring, and sensor/peripheral drivers specific to the Quest 2
(syncboss, cameras, audio codecs). The main kexec-specific risk is inherited SMMU state, not missing drivers.
