# GPU: Adreno 650 (A650v2) and GMU

**Current status on mainline: the GPU does not work yet.** The GPU nodes (`gpu`, `gmu`, `gpucc`, and the
separate `adreno_smmu`) are present in the DTS, but the Adreno SMMU does not finish probing: it stays
deferred until the deferred-probe timeout and is reported as -ETIMEDOUT (-110). Nothing renders. No GPU
firmware is distributed with this project.

This page records the hardware and the firmware-loading facts so that work can resume.

## Hardware

| Item | Value |
|---|---|
| Chip | Adreno 650, chip id 0x06050002 (A650v2) |
| GPU registers | `kgsl_3d0_reg_memory` 0x3d00000 (0x40000); IRQ SPI 300 |
| GMU | `gmu@3d6a000` (0x30000), PDC at 0xb290000 and 0xb490000, RSC at 0x3de0000; IRQs SPI 304 (HFI) and 305 (GMU) |
| Clock controller | `gpucc@3d90000` (`qcom,sm8250-gpucc`); GPU CX and GX GDSCs. GX is GMU-controlled |
| GPU SMMU | `adreno_smmu@3da0000`, **separate from the apps SMMU**; it serves the GPU and GMU contexts |
| Memory | `gpu_mem`, a small no-map reserved region (8 KiB) that is the zap shader's destination; it is in the DTS `reserved-memory` |
| OPP table | 305, 400, 441.6, 490, 525, 587, 670 MHz. The top frequency depends on the unit's fused speed bin (bin 0 caps at 587, bins 1/2 at 670, bin 3 at 525); this unit's bin was never read |
| Speed bin | read from qfprom (`qfprom@784000`, cell at +0x19b); the project DTS enables the qfprom node read-only for this |

The mainline `sm8250.dtsi` already describes all of these nodes with the same OPP levels as downstream; the
board DTS only enables them and names the zap firmware. The downstream kernel drove this GPU with KGSL
(`/dev/kgsl-3d0`) and a separate `kgsl-smmu`; mainline uses msm DRM plus Mesa Freedreno/Turnip.

## Firmware

The kernel needs three files:

| File | Name the kernel asks for | Source |
|---|---|---|
| SQE microcode | `qcom/a650_sqe.fw` | `linux-firmware`, or your device's copy |
| GMU firmware | `qcom/a650_gmu.bin` | `linux-firmware`, or your device's copy |
| Zap shader | the DT `firmware-name`, `qcom/sm8250/oculus/hollywood/a650_zap.mbn` in the project DTS | **your own device** (see below) |

- The GPU firmware has to be supplied by you, either from `linux-firmware` or from your own headset's
  firmware. This repository does not contain any firmware.
- The zap shader is signed for the device. A generic `linux-firmware` zap image is expected not to
  authenticate on this retail-fused unit (strong, not tested), so take the zap image from your own
  headset's vendor firmware. The SQE and GMU images are not device-signed.
- The zap image on the device is a single ELF file (no `.b00..bNN` split files are needed); the file name
  in the DT is only a name.
- The firmware loader runs in the kernel's init namespace, so the files must be present in the initramfs
  `/lib/firmware` (or a path set with `firmware_class.path`). See [../rootfs.md](../rootfs.md) for how the
  initramfs builder takes optional firmware inputs.

## Headless DRM mode

With no display nodes enabled there is no DRM master, so the GPU alone is brought up with the `msm` module
parameter `separate_gpu_kms=1`. This registers a render-only DRM device (`/dev/dri/renderD128`). The
project's kernel configuration builds `DRM_MSM` as a module with the display parts (`MDP4/MDP5/DPU/DP/DSI/HDMI`)
turned off, and `SM_GPUCC_8250` and `ARM_SMMU` as modules, so the GPU is only touched when the modules are
loaded. The project's kernel also carries a small SCM driver patch so that the SCM probe never writes the
secure-download-mode setting (see [../design-decisions.md](../design-decisions.md)).

## Thermal

Upstream `sm8250.dtsi` gives the GPU zones passive 85 C, hot 90 C and critical 110 C trips; this project
adds the 70 C fan trips. Downstream used 95 C passive for the GPU max-step zone. See
[cpufreq-thermal.md](cpufreq-thermal.md).

## Not known

- Why the downstream KGSL stack did not bind in a kexec-started boot (no log line).
- This unit's speed bin and therefore its maximum GPU frequency.
- Whether a zap image from `linux-firmware` authenticates on this device.
