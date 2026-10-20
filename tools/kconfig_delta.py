#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Kernel .config delta between two builds, with risky symbols flagged. Host only, read-only.

    python3 -I tools/kconfig_delta.py OLD/.config NEW/.config   report; exit 0 = no flagged symbol,
                                                                1 = flagged symbols changed, 2 = error

Parses `CONFIG_X=v` and `# CONFIG_X is not set` (= n) lines and lists every symbol added, removed or
changed. A change is FLAGGED (a person judges it; the tool never clears it) when the symbol touches a
hazard class on this hardware: storage write paths, secure/firmware interfaces (SCM, qfprom, nvmem, SDAM),
charger and fuel gauge, regulators, watchdog, reset/restart/panic/lockup handling (POWER_RESET*, PON, PS_HOLD,
PANIC*, DETECT_HUNG_TASK, SOFTLOCKUP*, download mode, SysRq), kexec and modules, /dev/mem, debugfs, suspend, thermal and
cpufreq, IOMMU, display/GPU, radios and remoteproc, MCU buses (SPI, syncboss), pstore, the built-in cmdline and clock
controllers.
"""
import re
import sys

FLAGS = [
    # BLOCK as a whole word part only (BLOCK, BLOCK_*, *_BLOCK), not DETECT_HUNG_TASK_BLOCKER
    ("storage", re.compile(r"UFS|SCSI|MMC|MTD|BLK_DEV|NVME|EXT4|F2FS|VFAT|FAT_FS|DM_|MD_|(?:^|_)BLOCK(?:_|$)")),
    ("secure", re.compile(r"QCOM_SCM|QFPROM|NVMEM|SDAM|TRUSTZONE|TEE|OPTEE|QCOM_SEC|QCOM_TZ")),
    ("power", re.compile(r"CHARGER|BATTERY|FUEL|BQ27|POWER_SUPPLY|SMB\d|QCOM_PMIC|SPMI|PON|RESET_QCOM|REBOOT")),
    ("regulator", re.compile(r"REGULATOR")),
    ("watchdog", re.compile(r"WATCHDOG|WDT")),
    # anything that resets, restarts or panics the SoC, or decides when it does
    ("reset-panic", re.compile(r"POWER_RESET|RESET|RESTART|REBOOT|WATCHDOG|WDT|PANIC|HUNG_TASK|SOFTLOCKUP|"
                               r"HARDLOCKUP|LOCKUP_DETECTOR|(?:^|_)PON(?:_|$)|PSHOLD|PS_HOLD|DLOAD|DOWNLOAD_MODE|"
                               r"PWRKEY|RESIN|MAGIC_SYSRQ|SYSRQ|BOOTPARAM_|OOPS")),
    ("kexec-modules", re.compile(r"KEXEC|MODULE|KALLSYMS")),
    ("devmem", re.compile(r"DEVMEM|DEVKMEM|IO_STRICT|DEVPORT")),
    ("debugfs", re.compile(r"DEBUG_FS|PINCTRL.*DEBUG|GPIO_SYSFS|GPIO_CDEV")),
    ("suspend", re.compile(r"SUSPEND|HIBERNATION|PM_SLEEP|PM_AUTOSLEEP|WAKELOCK")),
    ("thermal-cpufreq", re.compile(r"THERMAL|CPU_FREQ|CPUFREQ|DEVFREQ|ENERGY_MODEL|SENSORS_PWM_FAN")),
    ("iommu", re.compile(r"IOMMU|SMMU")),
    ("display-gpu", re.compile(r"DRM|MSM|ADRENO|FB_|BACKLIGHT|PANEL")),
    ("radio", re.compile(r"REMOTEPROC|RPROC|Q6V5|MHI|ATH1|WLAN|CFG80211|MAC80211|BT_|BT$|QRTR|QMI")),
    ("mcu-bus", re.compile(r"SPI|SYNCBOSS|SERIAL_QCOM|I2C_CHARDEV|SPIDEV|UIO")),
    ("pstore", re.compile(r"PSTORE|RAMOOPS")),
    ("cmdline", re.compile(r"CMDLINE")),
    ("clock", re.compile(r"COMMON_CLK|(?:^|_)[A-Z]*CC_|(?:^|_)CLK(?:_|$)")),
]
LINE = re.compile(r"^(CONFIG_[A-Za-z0-9_]+)=(.*)$")
UNSET = re.compile(r"^# (CONFIG_[A-Za-z0-9_]+) is not set$")


def parse(text):
    d = {}
    for line in text.splitlines():
        m = LINE.match(line)
        if m:
            d[m.group(1)] = m.group(2)
            continue
        m = UNSET.match(line)
        if m:
            d[m.group(1)] = "n"
    return d


def flags_for(sym):
    return [name for name, rx in FLAGS if rx.search(sym[7:])]


def changes(old, new):
    out = []
    for sym in sorted(set(old) | set(new)):
        a, b = old.get(sym, "(absent)"), new.get(sym, "(absent)")
        if a != b:
            out.append((sym, a, b, flags_for(sym)))
    return out


def report(old_path, new_path):
    with open(old_path) as f_old, open(new_path) as f_new:
        ch = changes(parse(f_old.read()), parse(f_new.read()))
    flagged = [c for c in ch if c[3]]
    lines = [f"kconfig_delta: {old_path} -> {new_path}: {len(ch)} symbol(s) changed, {len(flagged)} flagged"]
    for sym, a, b, fl in ch:
        lines.append(f"  {'FLAG ' if fl else '     '}{sym}: {a} -> {b}" + (f"   [{', '.join(fl)}]" if fl else ""))
    return lines


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print(__doc__.split("\n\n")[0], file=sys.stderr)
        return 2
    try:
        lines = report(argv[0], argv[1])
    except OSError as e:
        print(f"kconfig_delta: {e}", file=sys.stderr)
        return 2
    print("\n".join(lines))
    return 1 if any(l.startswith("  FLAG") for l in lines[1:]) else 0


if __name__ == "__main__":
    sys.exit(main())
