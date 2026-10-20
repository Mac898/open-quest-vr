# Wi-Fi and Bluetooth (QCA6390)

**Current status on mainline: not enabled.** The project's DTS does not enable PCIe, and the kernel
configuration does not build PCI, `ath11k`, QRTR or the Bluetooth stack. Wi-Fi and Bluetooth do not work.
The only network link today is the USB Ethernet gadget (see [../booting.md](../booting.md)).

This page records the hardware facts.

## Hardware

- The combined Wi-Fi/Bluetooth chip is a **Qualcomm QCA6390**. Evidence in the downstream sources: DT node
  `qcom,cnss-qca6390@b0000000`, `CONFIG_CNSS_QCA6390=y`, Bluetooth SLIMbus codec `qca6390`. (confirmed)
- It sits on PCIe root complex 0 (`qcom,pcie@1c00000`). The downstream kernel trained the link at
  Gen2 x1 (5 GT/s); the endpoint is `0000:01:00.0` `[17cb:1101]` behind a root port `[17cb:010b]`. (observed)
- GPIOs: PERST gpio79, CLKREQ gpio80, WAKE gpio81. Chip enables: `WLAN_EN` gpio20, `BT_EN` gpio21. An
  antenna-switch control line on gpio124 has no mainline consumer; its effect is unknown.
- Power rails (downstream request, DT range):

  | Chip rail | Regulator | Requested | Range |
  |---|---|---|---|
  | `vdd-wlan-io` | pm8150_s4 (smpa4) | 1.8 V | fixed |
  | `vdd-wlan-aon` / `vdd-wlan-dig` | pm8150_s6 (smpa6) | 0.95 V | 0.6-1.128 V |
  | `vdd-wlan-rfa1` | pm8150_s5 (smpa5) | 1.9 V | 1.824-2.04 V |
  | `vdd-wlan-rfa2` | pm8150a_s8 (smpc8) | 1.35 V | 1.2-1.4 V |

  The PCIe PHY uses `vreg-0p9` = pm8150_l5 (0.88 V) and `vreg-1p8` = pm8150_l9 (which is actually 1.2 V
  despite the name). Downstream power-up order: io, aon, dig, rfa1, rfa2, `BT_EN` on, `WLAN_EN` asserted,
  then link training after about 60 ms. (confirmed)
- DMA: PCIe stream IDs are 0x1c00 (root port, no DMA) and 0x1c01 (the QCA6390), both behind the apps SMMU
  (`iommu-map` in the DT). The downstream driver gave the endpoint its own context bank with a 256 MiB IOVA
  pool at 0xa0000000. See [../design-decisions.md](../design-decisions.md) for how this project attaches
  devices to the apps SMMU.
- Bluetooth HCI transport is probably the 4-wire GENI UART `998000.qcom,qup_uart` (`ttyHS0`, gpio16-19)
  (inferred from pin names); the Bluetooth audio path goes through SLIMbus. The UART `ttyHS13` on gpio14/15
  belongs to the display backlight MCU, not to Bluetooth.

## Mainline driver and firmware

- `ath11k_pci` supports QCA6390 (`pci17cb,1101`, hardware "qca6390 hw2.0"); the upstream SM8250 PCIe node
  and the QCA6390 power sequencer (`qcom,qca6390-pmu`) exist in the kernel tree. See
  [../upstream-state.md](../upstream-state.md).
- **Firmware must come from your own device or from `linux-firmware`.** ath11k looks for
  `ath11k/QCA6390/hw2.0/{amss.bin,m3.bin,regdb.bin}` and board data (`board.bin` or an entry in
  `board-2.bin`). The downstream driver loads the same content under other names (`amss20.bin`, `m3.bin`,
  `regdb.bin` and `bdwlan.elf` / `bdwlan.eNN` board files selected by board id). Board data is device
  specific. None of these files is shipped here.
- The host-side `ath11k` path only loads RAM images (MHI/BHI download, QMI messages); no write path to the
  chip's non-volatile memory was found in the driver (strong, source reading only). Bluetooth through
  `hci_qca` downloads a rampatch/NVM over UART, which writes the chip's NVM and is outside this project's scope: see [../safety.md](../safety.md).

## Not known

- Which board id the chip reports (selects the downstream `bdwlan.eNN` file).
- Which UART carries Bluetooth HCI.
- What the antenna-switch line does.
