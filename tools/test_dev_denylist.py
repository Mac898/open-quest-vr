#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Offline tests for the tools/dev command denylist.

Loads tools/dev as a module and calls its pure `refusal()` function. Nothing here
opens a socket or touches the headset. Dangerous command strings belong in this
file only; never send them to the device to test the filter.
Covers the free-form denylist, the fixed subcommands (fan-test, load-step, drain, heartbeat,
return-android, syncboss-txlen, stage 2), /sys/power writes, cpufreq-cap, mod-fetch/mod-load/
mod-unload, and kexec-self (the --cmdline token allowlist, the watchdog.open_timeout=90
exception, quest2_warm_reset.on_panic=1 and quest2_warm_reset.boot_stall_secs=<20..75>, and
reboot=warm staying refused).
Run: python3 -I tools/test_dev_denylist.py
"""
import importlib.machinery
import importlib.util
import os
import shutil
import socket
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


_WORK = []


def work_base():
    """A private temporary directory for this test run (removed at exit)."""
    if not _WORK:
        _WORK.append(tempfile.mkdtemp(prefix="dev-test-"))
        import atexit
        atexit.register(shutil.rmtree, _WORK[0], True)
    return _WORK[0]


def load_dev():
    loader = importlib.machinery.SourceFileLoader("dev", os.path.join(HERE, "dev"))
    spec = importlib.util.spec_from_loader("dev", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


# Make any accidental network use fail loudly.
def _no_network(*a, **k):
    raise AssertionError("denylist test tried to open a network connection")


socket.create_connection = _no_network
socket.socket.connect = _no_network

REFUSE = [
    "dd if=/dev/zero of=/dev/block/sda bs=1 count=1",
    "dd if=x of=/tmp/y",                      # any dd of= is refused
    "mkfs.ext4 /dev/block/sda5",
    "fdisk -l /dev/block/sda",
    "sgdisk -Z /dev/block/sdb",
    "blkdiscard /dev/block/sda",
    "flash_erase /dev/mtd0 0 0",
    "fastboot flash boot x.img",
    "echo 0 > /dev/block/sda",
    "cat x >> /dev/mmcblk0",
    "echo 0 > /stage2/dev/block/sda",         # same device via the stage-2 /dev bind
    "cat /dev/mem | head",
    "devmem 0x1000",
    "mount /dev/block/sda12 /mnt",
    "echo 1 > /sys/bus/spi/devices/spi1.0/syncboss_fw_update",
    "echo 1 > /sys/devices/platform/soc/894000.spi/spi_master/spi1/spi1.0/swd_flash",
    "cat syncboss.bin > /dev/syncboss0",
    "printf x >> /dev/syncboss_control0",
    "echo 1 > /sys/devices/platform/soc/blu/update_firmware",
    "cat fw.bin > /dev/ttyHS13",
    "echo go > /sys/bus/platform/devices/oculus-blu/blu_fw_update",
    "echo 1 > /sys/bus/spi/devices/spi1.0/control/reset",
    "echo 40 > /sys/bus/spi/devices/spi1.0/control/transaction_length",
    "cat /sys/class/misc/syncboss0/swd/update_status",
    "ls /sys/bus/spi/devices/spi1.0/swd",
    "cat /dev/ttyHS14",
    "exec 3</dev/syncboss_powerstate0",
    "i2cset -y 0 0x10 0x00 0x01",
    "i2ctransfer -y 1 w1@0x55 0x00",
    "setpci -s 00:00.0 COMMAND=0x02",
    "watchdog -T 1 /dev/watchdog0",
    "hwclock -w",
    "stty -F /dev/ttyHS0 115200",
    "cat /sys/kernel/debug/dri/0/state",
    "cat /sys/kernel/debug/kgsl/proc",
    "cat /sys/kernel/debug/gpio",
    "reboot",
    "poweroff -f",
    "echo r > /proc/qkx_bootdone",
    "echo b > /proc/sysrq-trigger",
    "kexec -l Image",
    "insmod foo.ko",
    "rmmod foo",
    # block devices and recursive removal inside the stage-2 chroot
    "rm -rf /stage2",
    "rm -r /stage2",
    "rm -fR /tmp/x",
    "rm -f -r /stage2",
    "rm --recursive /stage2",
    "find /stage2 | xargs rm -rf",
    "umount /dev",
    "umount -l /dev",
    "umount /dev/pts",
    "umount /tmp",
    "umount /proc",
    "umount -l /sys",
    "umount /config",
    "umount /",
    "umount \"/tmp\"",
    "umount -a",
    "umount -al",
    "qkx-launch-android",
    "qkx-switch",
    "touch /tmp/qkx-launch",
    "echo 1 > /tmp/qkx-launch",
    "cat /dev/watchdog0",
    "exec 3>/dev/watchdog",
    "i2cdetect -y 0",
    "i2cdetect 1",
    "lspci -vvv",
    "lspci -xxx",
    "lspci",
    "tee /dev/block/sda < x",
    "cp x /dev/block/by-name/boot",
    "mv x /dev/sda",
    "install x /dev/mmcblk0",
    "ln -sf x /dev/block/sde",
    "python3 -c \"open('/dev/block/sda','r+b').write(b'x')\"",
    "perl -e 'open(F,\">/dev/sda\")'",
    "cat /dev/block/sda | head -c 16",
    "ls /dev/block; cat /dev/block/sda",
    "echo 0 > /dev/ufs-bsg0",
    "dd if=/dev/mtd0",
    # every spi1.0 / syncboss sysfs write form stays refused via sh/s2, including the
    # approved value (only `tools/dev syncboss-txlen` may send it, as a fixed string).
    "echo 248 > /sys/bus/spi/devices/spi1.0/control/transaction_length",
    "echo 249 > /sys/bus/spi/devices/spi1.0/control/transaction_length",
    "echo 0 > /sys/bus/spi/devices/spi1.0/control/transaction_length",
    "echo 248 >> /sys/bus/spi/devices/spi1.0/control/transaction_length",
    "echo 248 > /sys/class/misc/syncboss0/spi/control/transaction_length",
    "echo 248 > /sys/devices/virtual/misc/syncboss0/spi/control/transaction_length",
    "printf 248 > /sys/devices/platform/soc/894000.spi/spi_master/spi1/spi1.0/control/transaction_length",
    "echo 248 | tee /sys/bus/spi/devices/spi1.0/control/transaction_length",
    "cp /tmp/x /sys/bus/spi/devices/spi1.0/control/transaction_length",
    "cd /sys/bus/spi/devices/spi1.0/control && echo 248 > transaction_length",
    "cd /sys/bus/spi/devices/spi1.0/control; echo 1 > reset",
    "p=/sys/bus/spi/devices/spi1.0/control/reset; echo 1 > $p",
    "cd /sys/bus/spi/devices && echo 1 > spi1.0/control/reset",
    "cd /sys/bus/spi/devices/spi1*/control && echo 1 > reset",
    "echo 1 > reset",
    "echo 0f > /sys/bus/spi/devices/spi1.0/control/cpu_affinity",
    "echo 1000 > /sys/bus/spi/devices/spi1.0/control/minimum_time_between_transactions_us",
    "echo reset > /sys/bus/spi/devices/spi1.0/control/stats",
    "echo 1 > /sys/bus/spi/devices/spi1.0/control/enable_fastpath",
    "python3 -c \"open('/sys/bus/spi/devices/spi1.0/control/reset','w').write('1')\"",
    "sh -c 'echo 1 > /sys/bus/spi/devices/spi1.0/control/reset'",
    "echo /sys/bus/spi/devices/spi1.0/control/reset | xargs -n1 sh -c 'echo 1 > $0'",
    "busybox tee /sys/bus/spi/devices/spi1.0/control/transaction_length < /tmp/v",
    "sed -i s/0/1/ /sys/bus/spi/devices/spi1.0/control/transaction_length",
    "ls /sys/bus/spi/devices/spi1.0/894000.spi:syncboss_nrf@0:swd@0/",
    "cat /sys/bus/spi/devices/spi1.0/894000.spi:syncboss_nrf@0:swd@0/update_status",
    "cat x > /dev/syncboss0",
    "printf '\\x6e\\x01\\x01\\x00' > /dev/syncboss0",
    # assorted write forms, all must be refused
    "echo 1 > /sys/bus/spi/devices/spi1.0/894000.spi:syncboss_nrf@0:swd@0/update_firmware",
    "cd /sys/bus/spi/devices/spi1.0/894000.spi:syncboss_nrf@0:swd@0 && echo 1 > update_firmware",
    "cd /sys/class/misc/syncboss0/swd && echo 1 > update_firmware",
    "echo 1 > /sys/class/misc/syncboss0/swd/update_firmware",
    "cd /sys/bus/spi/devices/spi1*/control && echo 248 > t*_length",
    "cd /sys/bus/spi/devices/spi1*/control && echo 1 > rese?",
    "cd /sys/bus/spi/devices/spi1*/*swd* && echo 1 > update_firm*",
    "cd /sys/bus/spi/devices/spi1*/*swd* && echo 1 > upd*",
    "cd /dev && echo 1 > sync*power*",
    "cat /dev/syncboss_powerstate0",
    "printf '\\x77\\x01\\x00' | tee /dev/syncboss0",
    "cp /tmp/pkt /dev/syncboss0",
    "python3 -c \"open('/dev/syncboss0','wb').write(b'\\x77')\"",
    "cat /tmp/pkt >> /dev/syncboss0",
    "echo 1 > /dev/syncboss_control0",
    "printf x | tee /dev/syncboss_control0",
    "chroot /stage2 python3 -c \"import os; os.write(os.open('/dev/syncboss0',1),b'\\x6e\\x01\\x01\\x00')\"",
    "echo 1 > /dev/ttyHS14",
    "cat /tmp/x > /dev/ttyHS1?",
    "echo 1 > /sys/bus/spi/drivers/syncboss/unbind",
    "echo spi1.0 > /sys/bus/spi/drivers/syncboss_spi/unbind",
    # C2: other writer forms into /dev/syncboss*
    "dd if=/tmp/pkt of=/dev/syncboss0",
    "perl -e 'open(F, \">/dev/syncboss0\"); print F \"x\"'",
    "sh -c 'cat /tmp/pkt > /dev/syncboss0'",
    "busybox tee /dev/syncboss0 < /tmp/pkt",
    "exec 3<>/dev/syncboss0",
    "cat /tmp/pkt 1<>/dev/syncboss0",
    "d=/dev/syncb; cat /tmp/pkt > ${d}oss0",
    "python3 /tmp/x.py /dev/syncboss0",
    "echo \"$(cat /tmp/pkt > /dev/syncboss0)\"",
    # C3: glob + write, cd + write
    "echo 1 > rese?",
    "echo 1 > /sys/class/gpio/gpio[0-9]/value",
    "for f in /sys/class/leds/*/brightness; do echo 0 > $f; done",
    "cp /tmp/x /sys/class/*/foo",
    "echo 1 | tee /sys/devices/*/x",
    "rm -f /dev/sync*",
    "cat /tmp/x > /dev/ttyHS1*",
    "cd /sys/class/gpio && echo 5 > export",
    "cd /proc && echo b > sysrq-trigger",
    "cd / && echo 1 > sys/bus/spi/devices/spi1.0/control/reset",
    "cd /tmp/../sys/kernel && echo 1 > x",
    "cd && echo 1 > x",
    "cd /dev; cp /tmp/x ttyHS13",
    "pushd /sys/class/gpio && echo 5 > export",
    "cd \"$D\" && echo 1 > x",
    # capture tool only by its exact path behind the sha gate
    "/usr/bin/python3 -I /tmp/lvr/syncboss_capture.py capture --out /tmp/sbcap/X --duration 5",
    "(setsid chroot /stage2 /usr/bin/python3 /tmp/lvr/syncboss_capture.py capture --out /tmp/sbcap/S0 --duration 15 </dev/null >/tmp/sbcap/S0.log 2>&1 &)",
    "sha256sum /tmp/lvr/syncboss_capture.py | grep -q '^0000000000000000000000000000000000000000000000000000000000000000 ' && (setsid chroot /stage2 /usr/bin/python3 -I /tmp/x/syncboss_capture.py capture --out /tmp/sbcap/S0 --duration 15 &)",
    "chroot /stage2 python3 /tmp/syncboss_capture.py capture --out /tmp/a --duration 5",
    # N3: kill only explicit PIDs > 1
    "kill -TERM 1",
    "kill -9 -1",
    "killall python3",
    "kill -1",
    "kill 1",
    "kill 0",
    "kill -s KILL 1",
    "kill -- -1",
    "kill -TERM -4242",
    "kill $(cat /tmp/telnet.pid)",
    "kill -9 `cat /tmp/telnet.pid`",
    "pkill -f telnetd",
    "busybox killall telnetd",
    "killall5 -9",
    "kill -TERM 4242; kill 1",
    # the heartbeat dead-man state is written only by `tools/dev heartbeat`
    "echo 99999 > /tmp/lvr-heartbeat",
    "echo 900 >> /tmp/lvr-heartbeat",
    "cut -d' ' -f1 /proc/uptime > /tmp/lvr-heartbeat",
    "touch /tmp/lvr-heartbeat",
    "cp /tmp/x /tmp/lvr-heartbeat",
    "cd /tmp && echo 5 > lvr-heartbeat",
    "cd /tmp; echo 5 | tee lvr-heartbeat",
    "printf 5 > /tmp//lvr-heartbeat",
    "echo timeout=3600 > /tmp/lvr-deadman",
    "echo deadline_uptime=99999 > /tmp/lvr-deadline",
    "rm -f /tmp/lvr-reset/reason",
    "rmdir /tmp/lvr-reset",
    "mv /tmp/lvr-reset /tmp/x",
    "mkdir /tmp/lvr-reset",
    "mkdir -p /tmp/lvr-reset/x",
    "echo x > /tmp/lvr-reset/reason",
    "sh -c 'echo 1 > /tmp/lvr-heartbeat'",
    "cut -d' ' -f1 /proc/uptime > /tmp/lvr-heartbeat; echo hb=$(cat /tmp/lvr-heartbeat); "
    "cat /tmp/lvr-deadman /tmp/lvr-deadline 2>/dev/null; echo reset=$(cat /tmp/lvr-reset/reason 2>/dev/null); echo END; reboot",
    "cut -d' ' -f1 /tmp/x > /tmp/lvr-heartbeat; echo hb=$(cat /tmp/lvr-heartbeat); "
    "cat /tmp/lvr-deadman /tmp/lvr-deadline 2>/dev/null; echo reset=$(cat /tmp/lvr-reset/reason 2>/dev/null); echo END",
    # the return-to-Android request only via `tools/dev return-android`.
    "touch /tmp/lvr-return",
    ": > /tmp/lvr-return",
    "echo 1 > /tmp/lvr-return",
    "cd /tmp && touch lvr-return",
    "cd /tmp; : > lvr-return",
    "mkdir /tmp/lvr-return",
    "cp /tmp/x /tmp/lvr-return",
    "ln -s /tmp/x /tmp/lvr-return",
    "touch /tmp//lvr-return",
    "touch /tmp/lvr-return && echo return=requested; "
    "echo reset=$(cat /tmp/lvr-reset/reason 2>/dev/null); echo END",
    "touch /tmp/lvr-return && echo return=requested; "
    "echo reset=$(cat /tmp/lvr-reset/reason 2>/dev/null); echo END; reboot",
    "/bin/busybox reboot",
    "reboot -f",
    "echo r >/proc/qkx_bootdone",
    "echo b >/proc/sysrq-trigger",
    # GPIO/pinctrl debugfs reads every pin's CTL register incl. TZ-owned reserved
    # GPIOs; refused however spelled. Also recursive readers that
    # can walk into debugfs, and any debugfs mount other than the canonical read-only one.
    "cat /sys/kernel/debug/pinctrl/f100000.pinctrl/pinconf-pins",
    "cat /sys/kernel/debug/pinctrl/f100000.pinctrl/pinconf-groups",
    "cat /sys/kernel/debug/pinctrl/f100000.pinctrl/pinmux-pins",
    "cat /sys/kernel/debug/pinctrl/f100000.pinctrl/pins",
    "cat /sys/kernel/debug/pinctrl/pinctrl-handles",
    "ls /sys/kernel/debug/pinctrl",
    "ls /sys/kernel/debug/pinctrl/",
    "head -c 4096 /sys/kernel/debug/pinctrl/f100000.pinctrl/pinconf-pins",
    "cat /sys/kernel/debug/pinctrl/f100000.pinctrl/pinconf-pins > /dev/null",
    "cat /sys/kernel/debug//pinctrl/f100000.pinctrl/pins",
    "cat /sys/kernel/debug/./gpio",
    "cat /sys//kernel/debug/gpio",
    "cat /sys/kernel/debug/gpio > /tmp/g.txt",
    "cat /proc/1/root/sys/kernel/debug/gpio",
    "cat /stage2/sys/kernel/debug/gpio",
    "cat /d/gpio",
    "cat /d/pinctrl/f100000.pinctrl/pinconf-pins",
    "cd /sys/kernel/debug && cat gpio",
    "cd /sys/kernel/debug/pinctrl/f100000.pinctrl; cat pinconf-pins",
    "cd /sys/kernel/debug/pinctrl/f100000.pinctrl && cat pins",
    "cd /sys/kernel && cat debug/gpio",
    "cd / && cat sys/kernel/debug/gpio",
    "cat pinconf-pins",
    "cat /sys/kernel/debug/*/pinconf-pins",
    "cat /sys/kernel/debug/gp*",
    "cat /sys/kernel/deb*/gpio",
    "cat /sys/kernel/debug/g?io",
    "cat /*/*/*/gpio",
    "cat */*/*/gpio",
    "cat /s*/k*/d*/g*",
    "cd /sys/kernel/debug && cat g*",
    "cd /sys && cat k*/d*/gpio",
    "cd $D && cat g*",
    "p=/sys/kernel/debug; cat $p/gpio",
    "g=gp; cat /sys/kernel/debug/${g}io",
    "for f in /sys/kernel/debug/*; do cat $f; done",
    "grep -r . /sys/kernel/debug",
    "grep -R gpio /sys",
    "grep -rs x /",
    "egrep -r x /sys/kernel",
    "find /sys -type f -exec cat {} +",
    "find /sys/kernel -name gpio | xargs cat",
    "tar cf /tmp/d.tar /sys/kernel/debug",
    "cp -r /sys/kernel/debug /tmp/dbg",
    "mount -t debugfs none /sys/kernel/debug",
    "mount -t debugfs debugfs /sys/kernel/debug",
    "mount -t debugfs -o rw debugfs /sys/kernel/debug",
    "mount -t debugfs -o ro debugfs /tmp/dbg",
    "mount -t debugfs -o ro,remount debugfs /sys/kernel/debug",
    "mount -o remount,rw /sys/kernel/debug",
    "mount -o ro,remount /sys/kernel/debug",
    "mount --bind /sys/kernel/debug /tmp/d",
    "busybox mount -t debugfs none /sys/kernel/debug",
    "mount -t debugfs -o ro debugfs /sys/kernel/debug; cat /sys/kernel/debug/gpio",
    "mount -t debugfs -o ro debugfs /sys/kernel/debug && mount -o remount,rw /sys/kernel/debug",
    # sysfs, procfs and configfs writes. None is ever sent to a device.
    "echo 0 > /sys/class/hwmon/hwmon0/pwm1",
    "echo 0 > /sys/class/hwmon/hwmon0/pwm1_enable",
    "echo 0 >/sys/class/hwmon/hwmon0/pwm1",
    "echo 0 >>/sys/class/hwmon/hwmon0/pwm1",
    "echo 0 >| /sys/class/hwmon/hwmon0/pwm1",
    "echo 0 1>/sys/class/hwmon/hwmon0/pwm1",
    "echo 0 &> /sys/class/hwmon/hwmon0/pwm1",
    "echo 0 >& /sys/class/hwmon/hwmon0/pwm1",
    "echo 0 >\"/sys/class/hwmon/hwmon0/pwm1\"",
    "echo 0 > '/sys/class/hwmon/hwmon0/pwm1'",
    "echo 0 > //sys//class/hwmon/hwmon0/pwm1",
    "echo 0 > /./sys/class/hwmon/hwmon0/pwm1",
    "echo 0 > sys/class/hwmon/hwmon0/pwm1",
    "echo 0 > /stage2/sys/class/hwmon/hwmon0/pwm1",
    "echo 0 > /proc/1/root/sys/class/hwmon/hwmon0/pwm1",
    "printf 0 > /sys/class/hwmon/hwmon0/pwm1",
    "cat /tmp/zero > /sys/class/hwmon/hwmon0/pwm1",
    "exec 3>/sys/class/hwmon/hwmon0/pwm1",
    "exec 3<>/sys/class/hwmon/hwmon0/pwm1",
    "echo \"$(echo 0 > /sys/class/hwmon/hwmon0/pwm1)\"",
    "echo `echo 0 > /sys/class/hwmon/hwmon0/pwm1`",
    "echo 0 | tee /sys/class/hwmon/hwmon0/pwm1",
    "tee /sys/class/hwmon/hwmon0/pwm1 < /tmp/zero",
    "busybox tee /sys/class/hwmon/hwmon0/pwm1 < /tmp/zero",
    "cp /tmp/zero /sys/class/hwmon/hwmon0/pwm1",
    "mv /tmp/zero /sys/class/hwmon/hwmon0/pwm1",
    "install -m 644 /tmp/zero /sys/class/hwmon/hwmon0/pwm1",
    "ln -sf /sys/class/hwmon/hwmon0/pwm1 /tmp/p",
    "truncate -s 0 /sys/class/hwmon/hwmon0/pwm1",
    "chmod 666 /sys/class/hwmon/hwmon0/pwm1",
    "dd if=/tmp/zero of=/sys/class/hwmon/hwmon0/pwm1",
    "sh -c 'echo 0 > /sys/class/hwmon/hwmon0/pwm1'",
    "busybox sh -c 'echo 0 > /sys/class/hwmon/hwmon0/pwm1'",
    "eval 'echo 0 > /sys/class/hwmon/hwmon0/pwm1'",
    "python3 -c \"open('/sys/class/hwmon/hwmon0/pwm1','w').write('0')\"",
    "perl -e 'open(F, \">/sys/class/hwmon/hwmon0/pwm1\")'",
    "awk 'BEGIN{print 0 > \"/sys/class/hwmon/hwmon0/pwm1\"}'",
    "sed -n 'w /sys/class/hwmon/hwmon0/pwm1' /tmp/zero",
    "echo /sys/class/hwmon/hwmon0/pwm1 | xargs -n1 cp /tmp/zero",
    "f=/sys/class/hwmon/hwmon0/pwm1; echo 0 > $f",
    "echo 0 > ${H}/pwm1",
    "echo 0 > \"$H/pwm1\"",
    "echo 0 > $(ls -d /sys/class/hwmon/hwmon0)/pwm1",
    "echo 0 > `ls -d /sys/class/hwmon/hwmon0`/pwm1",
    "find /sys/class/hwmon -name pwm1 -exec tee {} ; < /tmp/zero",
    "echo disabled > /sys/class/regulator/regulator.5/state",
    "echo 0 > /sys/devices/system/cpu/cpu1/online",
    "echo 1 > /sys/devices/system/cpu/cpu1/online",
    "echo a600000.usb > /sys/bus/platform/drivers/dwc3/unbind",
    "echo a600000.usb > /sys/bus/platform/drivers/dwc3/bind",
    "echo dwc3 > /sys/bus/platform/devices/a600000.usb/driver_override",
    "echo a600000.usb > /sys/bus/platform/drivers_probe",
    "echo '' > /config/usb_gadget/qkx/UDC",
    "echo > /sys/kernel/config/usb_gadget/qkx/UDC",
    "rm /config/usb_gadget/qkx/configs/c.1/ecm.usb0",
    "rmdir /config/usb_gadget/qkx/configs/c.1",
    "mkdir /sys/kernel/config/usb_gadget/g2",
    "echo 0 > /sys/class/leds/red:status/brightness",
    "echo 255 | tee /sys/class/leds/green:status/brightness",
    "echo none > /sys/class/leds/blue:heartbeat/trigger",
    "cp /tmp/x /sys/class/leds/red:status/brightness",
    "echo 1 > /sys/class/gpio/export",
    "echo mem > /sys/power/state",
    "echo 0 > /sys/module/qcom_wdt/parameters/x",
    "echo 1 > /proc/sys/kernel/sysrq",
    "echo 0 > /proc/sys/kernel/printk",
    "echo 3 > /proc/sys/vm/drop_caches",
    "echo f > /proc/irq/default_smp_affinity",
    "tee -a /proc/sys/vm/drop_caches < /tmp/x",
    "echo 1 > /proc/sys/kernel/firmware_config/ignore_sysfs_fallback; echo 0 > /sys/class/hwmon/hwmon0/pwm1",
    "echo 2 > /proc/sys/kernel/firmware_config/ignore_sysfs_fallback",
    "cat /proc/cmdline | tee /tmp/cmdline.txt",     # strict: no writer tool in a /proc command
    "echo 0 > /sys/class/hwmon/hwmon0/pwm1 2>/dev/null",
    "echo 0 > /tmp/x\"",                            # unbalanced quote
    # backslash-newline continuations are removed by the shell before tokenising
    "echo 1 > /s\\\nys/class/hwmon/hwmon0/pwm1",
    "echo 1 > /s\\\r\nys/class/hwmon/hwmon0/pwm1",
    "echo 1 >\\\n /sys/class/hwmon/hwmon0/pwm1",
    "t\\\nee /sys/class/hwmon/hwmon0/pwm1 < /tmp/zero",
    "echo 1 > /pr\\\noc/sys/kernel/sysrq",
    "echo 1 > /con\\\nfig/usb_gadget/qkx/UDC",
    # Android's /d debugfs alias
    "echo 1 > /d/regulator/x/enable",
    "echo 1 > //d/regulator/x/enable",
    "echo 1 > /d",
    "cp /tmp/x /d/regulator/x/enable",
    "tee /d/regulator/x/enable < /tmp/x",
    # writers with an output-file option or argument
    "tar -C /sys/class/hwmon/hwmon0 -xf /tmp/x.tar",
    "sort -o /sys/class/hwmon/hwmon0/pwm1 /tmp/zero",
    "sort --output=/sys/class/hwmon/hwmon0/pwm1 /tmp/zero",
    "wget -O /sys/class/hwmon/hwmon0/pwm1 http://10.42.0.1:8000/zero",
    "tftp -g -l /sys/class/hwmon/hwmon0/pwm1 -r zero 10.42.0.1",
    "curl -o /sys/class/hwmon/hwmon0/pwm1 http://10.42.0.1:8000/zero",
    "ftpget 10.42.0.1 /sys/class/hwmon/hwmon0/pwm1 zero",
    "cpio -i -D /sys/class/hwmon/hwmon0 < /tmp/a.cpio",
    "unzip -o -d /sys/class/hwmon/hwmon0 /tmp/a.zip",
    "uniq /tmp/zero /sys/class/hwmon/hwmon0/pwm1",
    "split -b 1 /tmp/zero /sys/class/hwmon/hwmon0/pwm",
    "patch /sys/class/hwmon/hwmon0/pwm1 /tmp/p.diff",
    "uudecode -o /sys/class/hwmon/hwmon0/pwm1 /tmp/z.uu",
    "dos2unix /sys/class/hwmon/hwmon0/pwm1",
    "chgrp root /sys/class/hwmon/hwmon0/pwm1",
    ". /tmp/s.sh; cat /sys/class/hwmon/hwmon0/pwm1",
    "source /tmp/s.sh; cat /sys/class/hwmon/hwmon0/pwm1",
    "rsync /tmp/zero /sys/class/hwmon/hwmon0/pwm1",
]
ALLOW = [
    "echo kernel=$(uname -r); cat /tmp/lvr-deadman 2>/dev/null; "
    "echo reset=$(cat /tmp/lvr-reset/reason 2>/dev/null); "
    "echo wdt=$(cat /tmp/lvr-reset/device 2>/dev/null); echo END",
    "echo reset=$(cat /tmp/lvr-reset/reason 2>/dev/null); "
    "echo wdt=$(cat /tmp/lvr-reset/device 2>/dev/null); echo END",
    "ls -l /tmp/lvr-return",
    "cat /tmp/lvr-return",
    "uname -a",
    "cat /proc/meminfo",
    "ls /sys/class/power_supply/",
    "cat /sys/class/power_supply/battery/capacity",
    "cat /sys/kernel/debug/regulator/regulator_summary",
    "cat /sys/kernel/debug/clk/clk_summary",
    "ls /dev/block",                          # listing is fine
    "grep -i error /tmp/kernel.log",
    "cat /proc/interrupts",
    "cd /sys/bus/spi/devices/spi1.0/control && for a in transaction_length streaming stats; do echo \"$a: $(cat $a)\"; done",
    "cat /sys/bus/spi/devices/spi1.0/nsync@0/dump_stats",
    "i2cdetect -l",
    "for z in /sys/class/thermal/thermal_zone*; do cat $z/type; done",
    # these must keep working
    "ls -l /dev/block/by-name",
    "ls /dev/sda /dev/sdb",
    "cat /proc/partitions",
    "umount -l /stage2/tmp",
    "umount /stage2",
    "rm -f /stage2/.stage2.tar.gz",
    "rmdir /stage2",
    "i2cdetect -l | head -20",
    "lspci --version",
    "lspci -n",
    "grep -ciE 'arm-smmu|context fault' /tmp/kernel.log",
    "cat /proc/sys/kernel/random/boot_id",
    "grep MemAvailable /proc/meminfo",
    # commands of a syncboss capture session
    "cat /sys/bus/spi/devices/spi1.0/control/stats",
    "cat /sys/bus/spi/devices/spi1.0/control/transaction_length 2>/dev/null",
    "cd /sys/bus/spi/devices/spi1.0/control && for a in transaction_length minimum_time_between_transactions_us maximum_send_delay_us spi_max_clk_rate cpu_affinity poll_prio enable_fastpath streaming stats; do echo \"$a=$(cat $a)\"; done",
    "cat /proc/sys/kernel/random/boot_id; wc -l < /tmp/kernel.log",
    "mkdir -p /tmp/lvr /tmp/sbcap && wget -q -O /tmp/lvr/syncboss_capture.py http://10.42.0.1:8001/syncboss_capture.py && sha256sum /tmp/lvr/syncboss_capture.py",
    "sha256sum /tmp/lvr/syncboss_capture.py | grep -q '^0233319dc61f3d2c0851b1bf214541a6ea1dde55d77cf49374ee69306d5a1ffe ' && (setsid chroot /stage2 /usr/bin/python3 -I /tmp/lvr/syncboss_capture.py capture --out /tmp/sbcap/S0 --duration 15 --schedule still --label S0 </dev/null >/tmp/sbcap/S0.log 2>&1 &); sleep 10; cat /tmp/sbcap/S0.log",
    "sha256sum /tmp/lvr/syncboss_capture.py | grep -q '^0233319dc61f3d2c0851b1bf214541a6ea1dde55d77cf49374ee69306d5a1ffe ' && (setsid chroot /stage2 /usr/bin/python3 -I /tmp/lvr/syncboss_capture.py capture --out /tmp/sbcap/S1 --duration 105 --schedule pitch --label S1 </dev/null >/tmp/sbcap/S1.log 2>&1 &); sleep 10; cat /tmp/sbcap/S1.log",
    "echo go > /tmp/sbmark",
    "cat /tmp/sbcap/S0.log; cat /tmp/sbcap/S0/summary.json; cat /sys/bus/spi/devices/spi1.0/control/streaming; ps | grep syncboss_captur[e]; true",
    "tail -n +$((5765+1)) /tmp/kernel.log | grep -iE 'syncb|nsync|miscfifo|meta_swd|fwupdate|MCU' | tail -30; echo END",
    "tail -n +$((5765+1)) /tmp/kernel.log | grep -iE 'oops|bug:|panic|segfault|unable to handle|arm-smmu|smmu|iommu|context fault|meta_swd|fwupdate|firmware update' | tail -20; echo END",
    "ps | grep syncboss_captur[e]",
    "kill -TERM 4242",
    "cd /tmp/sbcap && tar czf /tmp/sbcap-S0.tgz S0 S0.log && ls -l /tmp/sbcap-S0.tgz",
    "cat /tmp/sbcap-S0.tgz",
    "rm -f /tmp/sbcap-S0.tgz /tmp/sbcap-all.tgz",
    "cd /tmp/sbcap && tar czf /tmp/sbcap-all.tgz S0 S0.log S1 S1.log S2 S2.log S3 S3.log S4 S4.log S5 S5.log && ls -l /tmp/sbcap-all.tgz",
    "kill -TERM 4242 4243",
    "kill -9 4242",
    "kill -l",
    # probes that are reads and must stay allowed
    "cd /sys/devices/platform/soc/894000.spi/spi_master/spi1/spi1.0 && ls",
    "cat /sys/bus/spi/devices/spi1.0/control/stats",
    "echo 0 > /proc/sys/kernel/firmware_config/ignore_sysfs_fallback",
    # glob / quote handling: reads with globs and '>' inside quotes stay allowed
    "ls /sys/class/*/",
    "cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null",
    "ls /dev/sync* 2>&1",
    "ls -l /dev/ttyHS*",
    "for d in /sys/bus/platform/devices/*; do echo \"$(basename $d) -> $(basename $(readlink $d/driver) 2>/dev/null)\"; done",
    "echo 'x > y'; echo \"a -> b\"",
    "grep -c stage2 /proc/mounts > /tmp/count",
    "cat /proc/interrupts | grep -E 'gpio|spi' > /tmp/irq.txt",
    "for p in /proc/[0-9]*; do [ -e $p/comm ] && cat $p/comm 2>/dev/null; done; true",
    "dmesg | grep -i syncboss | tail -5",
    # reads of the dead-man state stay allowed
    "cat /tmp/lvr-deadman /tmp/lvr-deadline; cat /tmp/lvr-reset/reason 2>/dev/null; echo END",
    "cat /tmp/lvr-heartbeat",
    "grep -E 'lvr-init|pretimeout' /tmp/kernel.log | tail -5",
    # diagnostic reads and
    # neighbours that must stay allowed
    "ls /sys/class/leds",
    "cat /sys/class/leds/*/trigger",
    "for l in /sys/class/leds/*; do echo $l $(cat $l/brightness) $(cat $l/max_brightness); done",
    "ls /sys/class/udc",
    "if grep -q ' /sys/kernel/debug debugfs ' /proc/mounts; then cat /sys/kernel/debug/devices_deferred; "
    "else echo 'debugfs not mounted: skipped'; fi",
    "grep debugfs /proc/mounts",
    "cat /sys/kernel/debug/devices_deferred",
    "ls /sys/kernel/debug",
    "grep -E 'lvr-init|quest2-diag' /tmp/kernel.log | head -150; echo END",
    "grep -iE 'gpio-reserved|gpiochip|pinctrl|f100000|msm_gpio|tlmm' /tmp/kernel.log | head -40; echo END",
    "od -An -tx1 /proc/device-tree/soc@0/pinctrl@f100000/gpio-reserved-ranges; echo END",
    "mount -t debugfs -o ro debugfs /sys/kernel/debug",
    "mount -o ro -t debugfs none /sys/kernel/debug",
    "find / -name '*.ko'",
    "ls -R /sys/class/leds/",
    "cd /tmp && ls *",
    # reads of the same attributes, reads redirected to /tmp, quoted text, the
    # fw-fallback sysctl (exact forms only) stay allowed
    "cat /sys/class/hwmon/hwmon0/pwm1",
    "cat /sys/class/hwmon/hwmon0/pwm1 /sys/class/hwmon/hwmon0/fan1_input 2>/dev/null",
    "cat /sys/devices/system/cpu/cpu1/online",
    "cat /sys/class/regulator/regulator.5/state",
    "cat /sys/class/leds/red:status/brightness",
    "ls /sys/bus/platform/drivers/dwc3",
    "ls /config/usb_gadget/qkx; cat /config/usb_gadget/qkx/UDC",
    "cat /proc/cmdline > /tmp/cmdline.txt",
    "cut -d' ' -f1 /proc/uptime > /tmp/up.txt 2>&1",
    "cat /proc/uptime >> /tmp/up.log",
    "echo 'echo 0 > /sys/class/hwmon/hwmon0/pwm1'",
    "grep -c pwm1 /tmp/kernel.log > /tmp/n",
    "cat /sys/class/thermal/thermal_zone0/temp >&2",
    "echo 1 > /proc/sys/kernel/firmware_config/ignore_sysfs_fallback",
]
# tools/dev heartbeat must send exactly this (pinned here, like SYNCBOSS_TXLEN_CMD).
HEARTBEAT_CMD = ("cut -d' ' -f1 /proc/uptime > /tmp/lvr-heartbeat; echo hb=$(cat /tmp/lvr-heartbeat); "
                 "cat /tmp/lvr-deadman /tmp/lvr-deadline 2>/dev/null; "
                 "echo reset=$(cat /tmp/lvr-reset/reason 2>/dev/null); echo END")
# tools/dev return-android: pinned like HEARTBEAT_CMD.
RETURN_CMD = ("touch /tmp/lvr-return && echo return=requested; "
              "echo reset=$(cat /tmp/lvr-reset/reason 2>/dev/null); echo END")
RETURN_PROBE = ("echo kernel=$(uname -r); cat /tmp/lvr-deadman 2>/dev/null; "
                "echo reset=$(cat /tmp/lvr-reset/reason 2>/dev/null); "
                "echo wdt=$(cat /tmp/lvr-reset/device 2>/dev/null); "
                "echo warm=$(cat /tmp/lvr-warm 2>/dev/null); "
                "echo trig=$(cat /proc/lvr_warm_reset 2>/dev/null); echo END")      # + the trigger read
RETURN_POLL = ("echo reset=$(cat /tmp/lvr-reset/reason 2>/dev/null); "
               "echo wdt=$(cat /tmp/lvr-reset/device 2>/dev/null); "
               "echo warm=$(cat /tmp/lvr-warm 2>/dev/null); echo END")
# Allowed via `sh`, refused inside the stage-2 chroot.
S2_REFUSE_ONLY = ["mount -t debugfs -o ro debugfs /sys/kernel/debug",
                  "mount -o ro -t debugfs none /sys/kernel/debug"]
HB_REPLY = "hb=1234.56\ntimeout=600 pid=77\ndeadline_uptime=1800 heartbeat=1200\nreset=\nEND\n"
MODULES_OK = ["insmod foo.ko", "rmmod foo"]


FAKE_SHA = "ab" * 32


def fake_device(cmd, timeout):
    """Stand-in for tools/dev's telnet transport: plausible replies, no I/O."""
    if "lvr-heartbeat" in cmd:
        return HB_REPLY, 0
    if "readlink $p/root" in cmd:
        return "", 0                                  # no chroot processes
    if "SHA_OK" in cmd:
        return "SHA_OK\n", 0
    if "EXISTS" in cmd:
        return "FREE\nMemAvailable: 5800000 kB\n", 0
    if "STILL_BOUND" in cmd:
        return "S2_GONE\n", 0
    if cmd.startswith("uname -r"):
        return "4.19.325-cip124-st8-qkx\n1234.5\n0-7\n", 0
    if "power_supply/battery" in cmd:
        return "1760000000,88,301,Charging,4100000,-157000\n", 0
    if cmd.startswith("base64 "):
        return "aGVsbG8K\n", 0
    if "echo 248 > " in cmd:
        return "0\n248\n", 0
    if "ignore_sysfs_fallback" in cmd:
        return {"echo 1 >": "0\n1\n", "echo 0 >": "1\n0\n"}.get(
            next((k for k in ("echo 1 >", "echo 0 >") if k in cmd), ""), "0\n"), 0
    return "ok\n", 0


def transport_tests(dev):
    """Every op must send only commands that pass refusal(); run() must refuse on its own."""
    import io
    import tempfile
    sent, fails, total = [], 0, 0

    def rec(cmd, timeout):
        sent.append(cmd)
        return fake_device(cmd, timeout)
    dev._send = rec
    real_isfile, real_open = os.path.isfile, open
    tmp = tempfile.mkdtemp(prefix="devtest-", dir=work_base())
    ops = [["status"], ["battery"], ["battery", "--csv"], ["log", "20"],
           ["pull", "/tmp/kernel.log", os.path.join(tmp, "k.log")],
           ["sh", "uname -a"], ["s2", "python3 -V"], ["s2-up"], ["s2-down"],
           ["fw-fallback", "status"], ["fw-fallback", "off"], ["fw-fallback", "on"],
           ["heartbeat"]]
    # s2-up reads the tarball's .sha256 on the host; serve it a fixed value.
    shafile = os.path.realpath(os.path.join(dev.OUT_DIR, "stage2-latest.tar.gz")) + ".sha256"
    dev.os.path.isfile = lambda p: True if p.startswith(dev.OUT_DIR) or p == shafile else real_isfile(p)
    dev.open = lambda p, *a, **k: io.StringIO(FAKE_SHA + "  x\n") if p == shafile else real_open(p, *a, **k)
    for argv in ops:
        n0 = len(sent)
        sys.argv = ["dev"] + argv
        out = io.StringIO()
        real_stdout, sys.stdout = sys.stdout, out
        try:
            dev.main()
            code = 0
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) or e.code is None else 1
        finally:
            sys.stdout = real_stdout
        mine = sent[n0:]
        bad = [c for c in mine if dev.refusal(c) is not None]
        ok = not code and mine and not bad
        fails += not ok
        total += 1
        print(f"{'ok  ' if ok else 'FAIL'} op      {' '.join(argv)}  ({len(mine)} cmds sent)"
              + ("" if ok else f"  code={code} bad={bad[:1]} out={out.getvalue()[-200:]!r}"))
    dev.os.path.isfile = real_isfile
    del dev.open
    import shutil
    shutil.rmtree(tmp)
    # run() refuses by itself, before anything reaches the transport.
    for cmd in ("rm -rf /stage2", "touch /tmp/qkx-launch", "umount -l /dev"):
        n0 = len(sent)
        try:
            dev.run(cmd)
            ok = False
        except SystemExit:
            ok = len(sent) == n0
        fails += not ok
        total += 1
        print(f"{'ok  ' if ok else 'FAIL'} run()   refuses {cmd} without sending")
    # s2-up --sha must refuse a tarball other than the pinned one, before sending anything.
    dev.os.path.isfile = lambda p: True if p.startswith(dev.OUT_DIR) or p == shafile else real_isfile(p)
    dev.open = lambda p, *a, **k: io.StringIO(FAKE_SHA + "  x\n") if p == shafile else real_open(p, *a, **k)
    for pin, want_ok in (("cd" * 32, False), (FAKE_SHA, True)):
        n0 = len(sent)
        try:
            dev.s2_up("stage2-latest.tar.gz", 8000, pin)
            ok = want_ok and len(sent) > n0
        except SystemExit as e:
            ok = not want_ok and "pinned" in str(e) and len(sent) == n0
        fails += not ok
        total += 1
        print(f"{'ok  ' if ok else 'FAIL'} s2-up   --sha {'match' if want_ok else 'mismatch'}")
    dev.os.path.isfile = real_isfile
    del dev.open
    # syncboss-txlen: sends exactly SYNCBOSS_TXLEN_CMD, once, and nothing else.
    total += 1
    n0 = len(sent)
    try:
        dev.syncboss_txlen()
        ok = sent[n0:] == [dev.SYNCBOSS_TXLEN_CMD]
    except SystemExit as e:
        ok = False
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} op      syncboss-txlen sends exactly {sent[n0:]!r}")
    # ... and fails (non-zero exit) if the device does not read back 248.
    total += 1
    real_send = dev._send
    dev._send = lambda c, t: (sent.append(c), ("0\n0\n", 0))[1]
    try:
        dev.syncboss_txlen()
        ok = False
    except SystemExit as e:
        ok = "failed" in str(e)
    dev._send = real_send
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} op      syncboss-txlen fails when read-back is not 248")
    # A modified txlen string (other value, other attribute) is refused before sending.
    real_cmd = dev.SYNCBOSS_TXLEN_CMD
    for bad in (real_cmd.replace("248", "249"), real_cmd.replace("transaction_length", "reset"),
                real_cmd + "; echo 1 > /sys/bus/spi/devices/spi1.0/control/reset",
                real_cmd + "; reboot"):
        dev.SYNCBOSS_TXLEN_CMD = bad
        n0 = len(sent)
        try:
            dev.syncboss_txlen()
            ok = False
        except SystemExit as e:
            ok = len(sent) == n0 and "refusing" in str(e)
        fails += not ok
        total += 1
        print(f"{'ok  ' if ok else 'FAIL'} op      syncboss-txlen refuses modified string {bad[-60:]!r}")
    dev.SYNCBOSS_TXLEN_CMD = real_cmd
    # if only the generic sysfs rule (or any set without SB_WHY_DENY) refused the
    # pinned string, the syncboss rules have stopped firing: the self-check refuses.
    real_refusals = dev.refusals
    for fake in ([dev.SYSFS_WHY], [dev.SB_WHY_ATTR, dev.SYSFS_WHY], [dev.SB_WHY_REDIRECT], []):
        dev.refusals = lambda c, allow_modules=False, f=fake: list(f)
        n0 = len(sent)
        try:
            dev.syncboss_txlen()
            ok = False
        except SystemExit as e:
            ok = len(sent) == n0 and "self-check" in str(e)
        fails += not ok
        total += 1
        print(f"{'ok  ' if ok else 'FAIL'} op      syncboss-txlen self-check refuses when refusals == {fake}")
    dev.refusals = real_refusals
    n0 = len(sent)
    try:
        dev.syncboss_txlen()
        ok = sent[n0:] == [real_cmd]
    except SystemExit:
        ok = False
    fails += not ok
    total += 1
    print(f"{'ok  ' if ok else 'FAIL'} op      syncboss-txlen still sends its pinned string (real refusals)")
    # The txlen string itself is refused by run(), `sh` and `s2` (no bypass via the generic path).
    for argv in (None, ["sh", real_cmd], ["s2", real_cmd]):
        n0 = len(sent)
        try:
            if argv is None:
                dev.run(real_cmd)
            else:
                sys.argv = ["dev"] + argv
                dev.main()
            ok = False
        except SystemExit:
            ok = len(sent) == n0
        fails += not ok
        total += 1
        print(f"{'ok  ' if ok else 'FAIL'} refuse  {'run()' if argv is None else argv[0]} SYNCBOSS_TXLEN_CMD without sending")
    # fw-fallback sends only its fixed commands, which name nothing but the sysctl.
    for mode, cmd in dev.FW_FALLBACK_CMDS.items():
        ok = dev.refusal(cmd) is None and cmd.replace(dev.FW_FALLBACK, "").strip(" &;") in (
            "cat", "cat  && echo 1 >  && cat", "cat  && echo 0 >  && cat")
        fails += not ok
        total += 1
        print(f"{'ok  ' if ok else 'FAIL'} fw-fallback {mode} command is fixed and allowed")
    f, t = heartbeat_tests(dev, sent)
    fails, total = fails + f, total + t
    f, t = return_tests(dev, sent)
    fails, total = fails + f, total + t
    f, t = s2_debugfs_tests(dev, sent)
    fails, total = fails + f, total + t
    f, t = fan_tests(dev, sent)
    fails, total = fails + f, total + t
    f, t = load_tests(dev, sent)
    fails, total = fails + f, total + t
    f, t = line_limit_tests(dev, sent)
    fails, total = fails + f, total + t
    f, t = fixed_cmd_tests(dev, sent)
    fails, total = fails + f, total + t
    f, t = kexec_cmdline_tests(dev, sent)
    fails, total = fails + f, total + t
    f, t = reboot_token_tests(dev, sent)
    fails, total = fails + f, total + t
    f, t = status_tests()
    fails, total = fails + f, total + t
    f, t = warm_token_tests(dev, sent)
    fails, total = fails + f, total + t
    f, t = drain_tests(dev, sent)
    fails, total = fails + f, total + t
    # s2-up must refuse names outside rootfs/out.
    for name in ("../../etc/passwd", ".hidden", "a/b"):
        try:
            dev.s2_up(name, 8000)
            ok = False
        except SystemExit as e:
            ok = "bad tarball name" in str(e) or "outside" in str(e)
        fails += not ok
        total += 1
        print(f"{'ok  ' if ok else 'FAIL'} s2-up   refuses name {name}")
    return fails, total


def status_tests():
    """`tools/dev status` (and `battery`) with no mainline shell, e.g. run while Android is up
    (ConnectionRefusedError), ends with one stderr line and exit 2, never a traceback.
    A fresh tools/dev copy whose socket module is a stub: nothing reaches the network."""
    import io
    import socket as _socket
    import types
    fails, total = 0, 0

    def t(ok, what):
        nonlocal fails, total
        fails += not ok
        total += 1
        print(f"{'ok  ' if ok else 'FAIL'} t059    {what}")

    def invoke(mod, argv):
        out, err = io.StringIO(), io.StringIO()
        old = sys.argv, sys.stdout, sys.stderr
        sys.argv, sys.stdout, sys.stderr = ["dev"] + argv, out, err
        code, exc = 0, None
        try:
            mod.main()
        except SystemExit as e:
            code = e.code
        except Exception as e:                       # a traceback in real use
            exc = e
        finally:
            sys.argv, sys.stdout, sys.stderr = old
        return code, out.getvalue(), err.getvalue(), exc

    for name, err_obj in (("connection refused (Android up, no ECM link)", ConnectionRefusedError(111, "Connection refused")),
                          ("network unreachable", OSError(101, "Network is unreachable")),
                          ("connect timeout", _socket.timeout("timed out"))):
        fresh = load_dev()
        conns = []

        def connect(*a, _e=err_obj, **k):
            conns.append(a)
            raise _e
        fresh.socket = types.SimpleNamespace(create_connection=connect, timeout=_socket.timeout)
        for op in (["status"], ["battery"], ["battery", "--csv"]):
            code, out, err, exc = invoke(fresh, op)
            lines = err.strip().splitlines()
            t(exc is None and code == 2 and len(lines) == 1 and "no mainline shell at" in lines[0]
              and "Traceback" not in err and out == "" and len(conns) >= 1,
              f"{' '.join(op)}: {name} -> exit 2, one line ({(lines or [''])[0][:70]!r})")
    # a shell that answers but not in the expected form, or never completes
    for name, reply in (("garbage answer", lambda c, to: ("hello\n", 0)),
                        ("no complete response", None)):
        fresh = load_dev()

        def send(cmd, timeout, _r=reply):
            if _r is None:
                raise TimeoutError(f"no complete response within {timeout}s")
            return _r(cmd, timeout)
        fresh._send = send
        code, out, err, exc = invoke(fresh, ["status"])
        t(exc is None and code == 2 and len(err.strip().splitlines()) == 1 and "Traceback" not in err,
          f"status: {name} -> exit 2, one line ({err.strip()[:70]!r})")
    # the normal path still works: release, uptime, CPUs and the battery line
    fresh = load_dev()
    fresh._send = fake_device
    code, out, err, exc = invoke(fresh, ["status"])
    t(exc is None and code == 0 and out.startswith("up ") and "battery " in out, f"status: healthy shell -> 0 ({out.strip()[:70]!r})")
    # status with a broken battery read still prints the status line, exit 0
    fresh = load_dev()

    def half(cmd, timeout):
        if "power_supply" in cmd:
            raise TimeoutError("no complete response within 10s")
        return fake_device(cmd, timeout)
    fresh._send = half
    code, out, err, exc = invoke(fresh, ["status"])
    t(exc is None and code == 0 and out.startswith("up ") and "battery unreadable (TimeoutError" in out,
      f"status: battery read times out -> status line + `battery unreadable` ({out.strip()[-60:]!r})")
    return fails, total


def line_limit_tests(dev, sent):
    """the device shell is an interactive ash whose line editor cuts lines of 1024+
    bytes. _send refuses, before connecting, any command with a wrapped line >= LINE_MAX bytes
    or a line-editor control character; every fixed/composed command fits."""
    import types
    fails, total = 0, 0

    def t(ok, what):
        nonlocal fails, total
        fails += not ok
        total += 1
        print(f"{'ok  ' if ok else 'FAIL'} lines   {what}")

    t(dev.LINE_MAX == 1000 and dev.TAG_MAX == "Q9999999", "LINE_MAX 1000 (< 1023, the measured limit), worst-case tag Q + 7 digits")
    over = len(dev._wrap("", dev.TAG_MAX))          # wrapper bytes around CMD on its line, newline included
    t(dev.transport_refusal("x" * (dev.LINE_MAX - 1 - over)) is None
      and dev.transport_refusal("x" * (dev.LINE_MAX - over)) is not None,
      f"boundary: wrapped line of {dev.LINE_MAX - 1} bytes allowed, {dev.LINE_MAX} refused")
    long1 = "echo " + "a" * 1100
    t("transmitted line 1 is" in (dev.transport_refusal(long1) or ""), "one long line refused, line number named")
    multi = "( echo " + "a" * 900 + "\necho " + "b" * 900 + " )"
    t(dev.transport_refusal(multi) is None, "1.8 kB over two lines, each < LINE_MAX: allowed")
    multi2 = "( echo a\necho " + "b" * 1000 + " )"
    t("transmitted line 2 is" in (dev.transport_refusal(multi2) or ""), "a long continuation line refused")
    t(dev.transport_refusal(FAN_TEST_TEMPLATE_ONELINE.replace("@N@", "15")) is not None,
      "the one-line fan-test (1385 bytes) would have been refused")
    for c in ("echo a\tb", "echo a\rb", "echo \x03", "echo \x1b[A", "echo \x7f", "echo \x04", "echo a\x00"):
        t(dev.transport_refusal(c) is not None and "control character" in dev.transport_refusal(c),
          f"control character refused: {c!r}")
    # The real _send (a fresh copy: the suites above swap dev._send for fakes) refuses before
    # connecting; its socket module is a stub that records and fails every connection.
    fresh = load_dev()
    conns = []

    def connect(*a, **k):
        conns.append(a)
        raise OSError("stub: no network in tests")
    fresh.socket = types.SimpleNamespace(create_connection=connect, timeout=socket.timeout)
    for c in (long1, multi2, FAN_TEST_TEMPLATE_ONELINE.replace("@N@", "15"), "echo a\tb"):
        try:
            fresh._send(c, 5)
            ok = False
        except SystemExit as e:
            ok = not conns and "refused before sending" in str(e.code)
        except OSError:
            ok = False
        t(ok, f"_send refuses before connecting: {c[:40]!r}")
    try:
        fresh._send(multi, 5)
        ok = False
    except OSError:
        ok = len(conns) == 1
    except SystemExit:
        ok = False
    t(ok, "_send connects for a multi-line command within the limit")
    # Every fixed and composed device command fits.
    fixed = {"HEARTBEAT_CMD": dev.HEARTBEAT_CMD, "LOAD_SAMPLE_CMD": dev.LOAD_SAMPLE_CMD,
             "SYNCBOSS_TXLEN_CMD": dev.SYNCBOSS_TXLEN_CMD, "RETURN_PROBE": dev.RETURN_PROBE,
             "RETURN_CMD": dev.RETURN_CMD, "RETURN_CMD_PINNED": dev.RETURN_CMD_PINNED,
             "RETURN_POLL": dev.RETURN_POLL, "BATT": dev.BATT, "s2_procs_cmd": dev.s2_procs_cmd()}
    fixed.update({f"FW_FALLBACK_CMDS[{k}]": v for k, v in dev.FW_FALLBACK_CMDS.items()})
    for n in range(dev.FAN_TEST_MIN, dev.FAN_TEST_MAX + 1):
        fixed[f"fan_test_cmd({n})"] = dev.fan_test_cmd(n)
    for cores in dev.LOAD_CPUS:
        if cores:
            fixed[f"load_start_cmd({cores}, {dev.LOAD_MAX_S})"] = dev.load_start_cmd(cores, dev.LOAD_MAX_S)
    for label, c, _ in dev.s2_up_steps("stage2-test.tar.gz", 65535, "f" * 64) + dev.s2_down_steps():
        fixed[f"s2 step {label}"] = c
    worst = max(max(len(x.encode()) + 1 for x in dev._wrap(c, dev.TAG_MAX).split("\n")[:-1]) for c in fixed.values())
    bad = [k for k, c in fixed.items() if dev.transport_refusal(c) is not None]
    t(not bad, f"{len(fixed)} fixed/composed commands fit (longest wrapped line {worst} bytes) {bad[:3]}")
    fan_max = max(len(x.encode()) + 1 for n in (60,) for x in dev._wrap(dev.fan_test_cmd(n), dev.TAG_MAX).split("\n")[:-1])
    t(fan_max < 400, f"fan-test: longest wrapped line {fan_max} bytes")
    # The allowed commands of this suite, as `tools/dev sh` and as `tools/dev s2` would send them.
    plan, pbad, pworst = 0, [], 0
    for c in ALLOW:
        plan += 1
        for w in (c, dev.s2_wrap(c)):
            pworst = max(pworst, max(len(x.encode()) + 1 for x in dev._wrap(w, dev.TAG_MAX).split("\n")[:-1]))
            if dev.transport_refusal(w) is not None:
                pbad.append(c[:50])
    t(plan > 0 and not pbad, f"{plan} ALLOW lines fit, raw and s2-wrapped (longest {pworst} bytes) {pbad[:3]}")
    # Every command the fake transport saw in this suite fits too.
    sbad = sorted({c[:60] for c in sent if dev.transport_refusal(c) is not None})
    t(not sbad, f"all {len(sent)} commands sent by the ops/suites above fit {sbad[:3]}")
    # `tools/dev check` reports the transport limit (so `check -f` catches long lines).
    import io
    for argv, want in ((["check", long1], "refused: transmitted line 1"), (["check", "uname -r"], "allowed")):
        sys.argv = ["dev"] + argv
        out, real_stdout = io.StringIO(), sys.stdout
        sys.stdout = out
        try:
            dev.main()
            code = 0
        except SystemExit as e:
            code = e.code
        finally:
            sys.stdout = real_stdout
        t(out.getvalue().startswith(want) and (code == 0) == (want == "allowed"), f"tools/dev check {argv[1][:20]!r}: {want}")
    return fails, total


def heartbeat_tests(dev, sent):
    """`tools/dev heartbeat`: fixed command, parsing, failure exits, loop bounds."""
    import io
    fails, total = 0, 0

    def t(ok, what):
        nonlocal fails, total
        fails += not ok
        total += 1
        print(f"{'ok  ' if ok else 'FAIL'} hb      {what}")

    def call(fn, *a):
        """(exit message or None, stdout)"""
        out = io.StringIO()
        real, sys.stdout = sys.stdout, out
        try:
            fn(*a)
            return None, out.getvalue()
        except SystemExit as e:
            return str(e.code), out.getvalue()
        finally:
            sys.stdout = real

    t(dev.HEARTBEAT_CMD == HEARTBEAT_CMD, "HEARTBEAT_CMD is the pinned string")
    t(dev.refusal(dev.HEARTBEAT_CMD) is None, "HEARTBEAT_CMD passes the denylist")
    t(dev.refusal(dev.s2_wrap(dev.HEARTBEAT_CMD)) is None, "HEARTBEAT_CMD passes the denylist via s2 too")
    t(dev.refusal(dev.HEARTBEAT_CMD + " ") is not None, "HEARTBEAT_CMD + one space is refused (exact only)")
    # parse
    i = dev.parse_heartbeat(HB_REPLY)
    t(i["hb"] == 1234.56 and i["timeout"] == 600 and i["prev_deadline"] == "1800"
      and i["reset"] == "", f"parse normal reply ({i})")
    r = dev.parse_heartbeat(HB_REPLY.replace("reset=", "reset=dead-man: no heartbeat for 600s (uptime 1800s)"))
    t(r["reset"].startswith("dead-man") and r["timeout"] == 600, "parse armed reset reason")
    for bad, what in [("hb=12.3\nreset=\nEND\n", "no lvr-deadman (downstream initramfs)"),
                      ("hb=\ntimeout=600 pid=1\nreset=\nEND\n", "no uptime written"),
                      ("hb=12.3\ntimeout=600 pid=1\n", "truncated reply"),
                      ("", "empty reply")]:
        try:
            dev.parse_heartbeat(bad)
            t(False, f"parse refuses {what}")
        except ValueError:
            t(True, f"parse refuses {what}")
    real_send = dev._send

    def reply(text, rc=0):
        def f(c, tmo):
            sent.append(c)
            return text, rc
        return f
    # one-shot: exactly one command, the pinned one; prints the new deadline
    dev._send = reply(HB_REPLY)
    n0 = len(sent)
    err, out = call(dev.heartbeat)
    t(err is None and sent[n0:] == [HEARTBEAT_CMD] and "reset at uptime >= 1834s" in out,
      f"one-shot sends exactly HEARTBEAT_CMD once ({out.strip()[-80:]!r})")
    # failures exit non-zero
    for text, what in [(HB_REPLY.replace("reset=", "reset=USB not configured (uptime 200s)"), "reset armed"),
                       ("hb=1.0\nreset=\nEND\n", "not the mainline init"),
                       ("garbage", "garbled reply")]:
        dev._send = reply(text)
        err, _ = call(dev.heartbeat)
        t(err is not None and err != "0", f"one-shot exits non-zero: {what} ({(err or '')[:60]!r})")

    def boom(c, tmo):
        sent.append(c)
        raise TimeoutError("no complete response")
    dev._send = boom
    err, _ = call(dev.heartbeat)
    t(err is not None and "heartbeat failed" in err, "one-shot exits non-zero on transport timeout")
    # loop: sleeps between heartbeats, stops at --max-duration, at the first failure
    clock = [0.0]
    dev._now = lambda: clock[0]

    def fake_sleep(sec):
        clock[0] += sec
    dev._sleep = fake_sleep
    dev._send = reply(HB_REPLY)
    n0 = len(sent)
    err, out = call(dev.heartbeat, 60, 600)
    k = len(sent) - n0
    t(err is None and k == 11 and set(sent[n0:]) == {HEARTBEAT_CMD} and "reached" in out,
      f"--loop 60 --max-duration 600 sends 11 heartbeats (t=0..600) then stops ({k})")
    seq = [HB_REPLY, HB_REPLY, HB_REPLY.replace("reset=", "reset=dead-man (uptime 9s)"), HB_REPLY]
    calls = []

    def seq_send(c, tmo):
        sent.append(c)
        calls.append(c)
        return seq[len(calls) - 1], 0
    dev._send = seq_send
    clock[0] = 0.0
    err, _ = call(dev.heartbeat, 60, 3600)
    t(err is not None and "ARMED" in err and len(calls) == 3, f"loop stops at an armed reset ({len(calls)} sent)")
    calls.clear()
    seq[2] = None

    def seq_fail(c, tmo):
        sent.append(c)
        calls.append(c)
        if len(calls) == 2:
            raise OSError("link down")
        return HB_REPLY, 0
    dev._send = seq_fail
    err, _ = call(dev.heartbeat, 60, 3600)
    t(err is not None and "heartbeat failed" in err and len(calls) == 2,
      f"loop stops at the first transport failure ({len(calls)} sent)")
    # bounds: refused before anything is sent
    dev._send = reply(HB_REPLY)
    for args, what in [((5, 600), "--loop 5"), ((301, 3600), "--loop 301"),
                       ((60, 30), "--max-duration < --loop"), ((60, 14401), "--max-duration 14401")]:
        n0 = len(sent)
        err, _ = call(dev.heartbeat, *args)
        t(err is not None and len(sent) == n0, f"loop refuses {what} without sending")
    # interval more than half the target's timeout: one heartbeat, then stop
    dev._send = reply(HB_REPLY.replace("timeout=600", "timeout=120"))
    n0 = len(sent)
    err, _ = call(dev.heartbeat, 61, 3600)
    t(err is not None and "half" in err and len(sent) - n0 == 1, "loop stops if --loop > timeout/2")
    # the CLI path wires --loop/--max-duration through
    dev._send = reply(HB_REPLY)
    clock[0] = 0.0
    n0 = len(sent)
    sys.argv = ["dev", "heartbeat", "--loop", "30", "--max-duration", "90"]
    err, out = call(dev.main)
    t(err is None and len(sent) - n0 == 4, f"CLI heartbeat --loop 30 --max-duration 90 -> 4 sent ({len(sent) - n0})")
    dev._send = real_send
    dev._sleep, dev._now = __import__("time").sleep, __import__("time").monotonic
    return fails, total


def _call(fn, *a):
    """(exit code/message or None, stdout) of fn(*a)."""
    import io
    out = io.StringIO()
    real, sys.stdout = sys.stdout, out
    try:
        r = fn(*a)
        return (None if not r else r), out.getvalue()
    except SystemExit as e:
        return (e.code if e.code not in (0, None) else None), out.getvalue()
    finally:
        sys.stdout = real


ML_PROBE = "kernel=6.18.55-00008-g5463acb4dd65\ntimeout=600 pid=77\nreset=\nwdt=\nEND\n"
DS_PROBE = "kernel=4.19.325-cip124-st8-qkx\nreset=\nwdt=\nEND\n"
ARMED = "reset=return-android requested (uptime 321s)\nwdt=/dev/watchdog0\nEND\n"


def return_tests(dev, sent):
    """`tools/dev return-android`: fixed command only through the subcommand; mainline vs
    downstream dispatch; the poll and its outcomes. No network (fake transport)."""
    fails, total = 0, 0

    def t(ok, what):
        nonlocal fails, total
        fails += not ok
        total += 1
        print(f"{'ok  ' if ok else 'FAIL'} return  {what}")

    t(dev.RETURN_CMD == RETURN_CMD and dev.RETURN_CMD_PINNED == RETURN_CMD, "RETURN_CMD is the pinned string")
    t(dev.RETURN_PROBE == RETURN_PROBE and dev.RETURN_POLL == RETURN_POLL, "probe and poll are the pinned strings")
    t(dev.refusals(RETURN_CMD) == [dev.LVR_WHY], f"RETURN_CMD refused by the lvr-state rule only ({dev.refusals(RETURN_CMD)})")
    t(dev.refusal(RETURN_PROBE) is None and dev.refusal(RETURN_POLL) is None, "probe and poll pass the denylist")
    for c in ("reboot", "/bin/busybox reboot", "echo r > /proc/qkx_bootdone", "echo b > /proc/sysrq-trigger"):
        t(any("return-android" in w for w in dev.refusals(c)), f"{c!r} refused, pointing to return-android")
    # Free-form forms through `sh` / `s2` / run(): refused before anything is sent.
    for argv in (["sh", RETURN_CMD], ["s2", RETURN_CMD], ["sh", "touch /tmp/lvr-return"],
                 ["sh", "cd /tmp && touch lvr-return"], ["s2", "touch /tmp/lvr-return"],
                 ["sh", "reboot"], ["sh", "echo r > /proc/qkx_bootdone"],
                 ["sh", "echo b > /proc/sysrq-trigger"], ["s2", "reboot"],
                 ["s2", "echo b > /proc/sysrq-trigger"], None):
        n0 = len(sent)
        try:
            if argv is None:
                dev.run(RETURN_CMD)
            else:
                sys.argv = ["dev"] + argv
                dev.main()
            ok = False
        except SystemExit as e:
            ok = len(sent) == n0 and "refused" in str(e.code)
        t(ok, f"{'run()' if argv is None else ' '.join(argv)[:60]!s} refused without sending")

    real_send, real_call = dev._send, dev.subprocess.call
    clock = [0.0]
    dev._now = lambda: clock[0]

    def fake_sleep(sec):
        clock[0] += sec
    dev._sleep = fake_sleep
    calls = []
    dev.subprocess.call = lambda argv, **k: (calls.append((argv, k)), 0)[1]

    def device(probe, req="return=requested\nreset=\nEND\n", polls=(ARMED,), batt=None):
        n = [0]

        def f(c, tmo):
            sent.append(c)
            if c == dev.RETURN_PROBE:
                return probe, 0
            if c == dev.RETURN_CMD:
                return req, 0
            if c == dev.RETURN_POLL:
                r = polls[min(n[0], len(polls) - 1)]
                n[0] += 1
                if isinstance(r, Exception):
                    raise r
                return r, 0
            if "power_supply/battery" in c:
                return batt or "sh: cd: can't cd to /sys/class/power_supply/battery\n", 0 if batt else 2
            return "?\n", 0
        return f

    def run_ra(dev_fn, argv=False):
        dev._send = dev_fn
        clock[0] = 0.0
        n0 = len(sent)
        if argv:
            sys.argv = ["dev", "return-android"] + (argv if isinstance(argv, list) else [])
            err, out = _call(dev.main)
        else:
            err, out = _call(dev.return_android)
        return err, out, sent[n0:]

    # Mainline: probe, battery (unreadable), the fixed command once, polls until armed.
    err, out, mine = run_ra(device(ML_PROBE, polls=("reset=\nwdt=\nEND\n", ARMED)))
    t(err is None and mine[0] == RETURN_PROBE and mine.count(RETURN_CMD) == 1
      and mine[mine.index(RETURN_CMD) + 1:] == [RETURN_POLL, RETURN_POLL] and not calls
      and "reset armed: return-android requested" in out and "not readable" in out,
      f"mainline: probe, fixed command once, polls until armed, no kexec script ({len(mine)} sent)")
    t(all(dev.refusal(c) is None for c in mine if c != RETURN_CMD),
      "mainline: every other command sent passes the denylist")
    t(mine.index(RETURN_CMD) > mine.index(RETURN_PROBE), "mainline: the request follows the probe")
    err, out, mine = run_ra(device(ML_PROBE), argv=True)
    t(err is None and mine.count(RETURN_CMD) == 1, f"CLI `tools/dev return-android` on mainline exits 0 ({err})")
    # Battery readable on mainline: printed.
    err, out, mine = run_ra(device(ML_PROBE, batt="1760000000,88,301,Discharging,4100000,-157000\n"))
    t(err is None and "battery 88%" in out, "mainline: battery printed when readable")
    # Downstream quest-kexec boot: battery, then that checkout's return script; no flag file.
    import tempfile
    kx = tempfile.mkdtemp(prefix="quest-kexec-")
    os.makedirs(os.path.join(kx, "tools"))
    open(os.path.join(kx, "tools", "return-android.sh"), "w").close()
    saved_env = os.environ.pop(dev.KEXEC_DIR_ENV, None)
    try:
        # no checkout configured: a clear error naming the variable and the flag, nothing sent but the probe
        err, out, mine = run_ra(device(DS_PROBE, batt="1760000000,88,301,Charging,4100000,-157000\n"))
        t(err is not None and dev.KEXEC_DIR_ENV in str(err) and "--kexec-dir" in str(err)
          and mine == [RETURN_PROBE] and not calls, f"downstream without {dev.KEXEC_DIR_ENV}: clear error, only the probe sent")
        # the directory is not a checkout
        os.environ[dev.KEXEC_DIR_ENV] = os.path.join(kx, "nope")
        err, out, mine = run_ra(device(DS_PROBE))
        t(err is not None and "not found" in str(err) and not calls, "downstream with a bad directory: error, script not run")
        # via the environment
        os.environ[dev.KEXEC_DIR_ENV] = kx
        err, out, mine = run_ra(device(DS_PROBE, batt="1760000000,88,301,Charging,4100000,-157000\n"))
        t(err is None and RETURN_CMD not in mine and len(calls) == 1
          and calls[0][0] == [os.path.join(kx, "tools", "return-android.sh")]
          and calls[0][1].get("cwd") == kx and "battery 88%" in out,
          f"downstream: runs the quest-kexec return-android.sh, sends no flag ({calls[:1]})")
        calls.clear()
        # via --kexec-dir
        del os.environ[dev.KEXEC_DIR_ENV]
        err, out, mine = run_ra(device(DS_PROBE, batt="1760000000,88,301,Charging,4100000,-157000\n"),
                                argv=["--kexec-dir", kx])
        t(err is None and len(calls) == 1 and calls[0][1].get("cwd") == kx, "downstream: --kexec-dir works without the variable")
        calls.clear()
    finally:
        os.environ.pop(dev.KEXEC_DIR_ENV, None)
        if saved_env is not None:
            os.environ[dev.KEXEC_DIR_ENV] = saved_env
        shutil.rmtree(kx, ignore_errors=True)
    # Unknown targets: nothing sent after the probe.
    for probe, what in [("kernel=6.18.55\nreset=\nwdt=\nEND\n", "mainline kernel, no lvr-init"),
                        ("kernel=4.19.325-cip124-st8-qkx\ntimeout=600 pid=7\nreset=\nwdt=\nEND\n", "4.19 with lvr-deadman"),
                        ("kernel=5.4.0\nreset=\nwdt=\nEND\n", "other kernel"),
                        ("garbage", "garbled probe")]:
        err, out, mine = run_ra(device(probe))
        t(err is not None and mine == [RETURN_PROBE] and not calls, f"refuses {what}: only the probe sent")
    # Probe transport failure.
    def dead(c, tmo):
        sent.append(c)
        raise TimeoutError("no complete response")
    err, out, mine = run_ra(dead)
    t(err is not None and "long power-button" in str(err) and mine == [RETURN_PROBE], "probe failure: nothing else sent")
    # Already armed: nothing sent; exit 0 with a held watchdog, 1 without.
    err, out, mine = run_ra(device(ML_PROBE.replace("reset=\nwdt=", "reset=dead-man: x (uptime 9s)\nwdt=/dev/watchdog0")))
    t(err is None and RETURN_CMD not in mine and "already armed" in out, "already armed (watchdog held): no request sent, exit 0")
    err, out, mine = run_ra(device(ML_PROBE.replace("reset=\nwdt=", "reset=dead-man: x (uptime 9s)\nwdt=")))
    t(err is not None and RETURN_CMD not in mine, "already armed without a watchdog: no request, exit non-zero")
    # The init never acts (older init): bounded poll, non-zero exit.
    err, out, mine = run_ra(device(ML_PROBE, polls=("reset=\nwdt=\nEND\n",)))
    np_ = mine.count(RETURN_POLL)
    t(err is not None and "did not act" in str(err) and mine.count(RETURN_CMD) == 1
      and 1 <= np_ <= dev.RETURN_WAIT / dev.RETURN_POLL_EVERY + 1,
      f"init never acts: {np_} polls in {dev.RETURN_WAIT}s, then exit non-zero")
    # Poll transport errors are retried within the window.
    err, out, mine = run_ra(device(ML_PROBE, polls=(OSError("x"), TimeoutError("y"), ARMED)))
    t(err is None and mine.count(RETURN_POLL) == 3, "poll transport errors retried, then armed")
    # the init's clean reboot. Warm state seen, then the link drops: exit 0, stops polling
    # after 3 failed polls.
    WARM = "reset=\nwdt=\nwarm=return-android requested (uptime 321s)\nEND\n"
    err, out, mine = run_ra(device(ML_PROBE, polls=("reset=\nwdt=\nwarm=\nEND\n", WARM, OSError("gone"))))
    t(err is None and "clean reboot issued (warm reset trigger /proc/lvr_warm_reset written: "
      "return-android requested (uptime 321s))" in out and "'warm'" in out and "backstop" in out
      and mine.count(RETURN_POLL) == 5,
      f"clean reboot seen, link lost: exit 0 after 3 failed polls ({mine.count(RETURN_POLL)} polls)")
    # Warm state, then the init's watchdog fallback armed: exit 0, fallback reported.
    err, out, mine = run_ra(device(ML_PROBE, polls=(WARM, WARM, ARMED)))
    t(err is None and "reset armed: return-android requested" in out and "watchdog fallback" in out
      and "trigger returned" in out and "No warm reset tried" not in out,
      "clean reboot did not happen, fallback armed: reported, exit 0")
    # the request armed the bite without any warm state (trigger missing / not armed): exit 0,
    # reported as a cold return; the plain test above (ARMED) gets the same note.
    err, out, mine = run_ra(device(ML_PROBE, polls=("reset=\nwdt=\nwarm=\nEND\n", ARMED)))
    t(err is None and "No warm reset tried" in out and "/proc/lvr_warm_reset" in out and "trigger returned" not in out,
      "trigger not armed (no warm state), bite armed: 'No warm reset tried' note, exit 0")
    err, out, mine = run_ra(device(ML_PROBE, polls=("reset=dead-man: no heartbeat (uptime 600s)\nwdt=/dev/watchdog0\nEND\n",)))
    t(err is None and "No warm reset tried" not in out, "another reset path first: no warm-reset note")
    # Link lost without any state seen: non-zero, says why.
    err, out, mine = run_ra(device(ML_PROBE, polls=(OSError("gone"),)))
    t(err is not None and "consistent with a clean reboot" in str(err)
      and mine.count(RETURN_POLL) == dev.RETURN_WAIT / dev.RETURN_POLL_EVERY,
      f"link lost, nothing seen: polls the full window, exit non-zero ({mine.count(RETURN_POLL)} polls)")
    # A clean reboot already in progress at the probe: nothing sent, exit 0.
    err, out, mine = run_ra(device(ML_PROBE.replace("wdt=", "wdt=\nwarm=return-android requested (uptime 9s)")))
    t(err is None and RETURN_CMD not in mine and "already in progress" in out,
      "clean reboot already in progress: no request sent, exit 0")
    # Reason without a watchdog device: long-press message.
    err, out, mine = run_ra(device(ML_PROBE, polls=("reset=return-android requested (uptime 5s)\nwdt=\nEND\n",)))
    t(err is not None and "long power-button" in str(err), "armed but no watchdog held: exit non-zero, long-press")
    # Another reset path first (dead-man): still exit 0, reported.
    err, out, mine = run_ra(device(ML_PROBE, polls=("reset=dead-man: no heartbeat (uptime 600s)\nwdt=/dev/watchdog0\nEND\n",)))
    t(err is None and "another reset path was first" in out, "another reset path first: reported, exit 0")
    # Request not confirmed.
    for req in ("reset=\nEND\n", "sh: can't create /tmp/lvr-return: Read-only\nreset=\nEND\n"):
        err, out, mine = run_ra(device(ML_PROBE, req=req))
        t(err is not None and RETURN_POLL not in mine, f"request not confirmed ({req[:20]!r}): exit, no poll")
    # Modified command or a changed self-check: refused before sending it.
    real_cmd, real_ref = dev.RETURN_CMD, dev.refusals
    for bad in (real_cmd + "; reboot", real_cmd.replace("lvr-return", "lvr-reset"), "echo r > /proc/qkx_bootdone"):
        dev.RETURN_CMD = bad
        err, out, mine = run_ra(device(ML_PROBE))
        t(err is not None and "refusing" in str(err) and bad not in mine and real_cmd not in mine,
          f"modified RETURN_CMD refused before sending ({bad[-40:]!r})")
    dev.RETURN_CMD = real_cmd
    for fake in ([], [dev.LVR_WHY, "x"], ["x"]):
        dev.refusals = lambda c, allow_modules=False, f=fake, r=real_ref: list(f) if c == real_cmd else r(c, allow_modules)
        err, out, mine = run_ra(device(ML_PROBE))
        t(err is not None and "self-check" in str(err) and real_cmd not in mine,
          f"self-check refuses when refusals(RETURN_CMD) == {fake}")
    dev.refusals = real_ref
    dev._send, dev.subprocess.call = real_send, real_call
    dev._sleep, dev._now = __import__("time").sleep, __import__("time").monotonic
    return fails, total


# tools/dev fan-test: the original device command as one line. It never ran: the device's
# interactive ash cuts input lines of 1024+ bytes. Kept to prove the text below
# is the same command with some "; " separators turned into newlines.
FAN_TEST_TEMPLATE_ONELINE = (
    '( H=; n=0; for h in /sys/class/hwmon/hwmon*; do d=$(readlink $h/device/driver)'
    '; if [ "$(cat $h/name 2>/dev/null)" = pwmfan ] && [ "${d##*/}" = pwm-fan ]; then H=$h'
    '; n=$((n+1)); fi; done; if [ "$n" != 1 ]; then echo FAN_NOHWMON n=$n; exit 10; fi'
    '; E0=$(cat $H/pwm1_enable); P0=$(cat $H/pwm1)'
    "; echo FAN_HWMON=$H enable=$E0 pwm=$P0 rpm=$(cat $H/fan1_input) up=$(cut -d' ' -f1 /proc/uptime)"
    '; if [ "$E0" != 1 ]; then echo FAN_ENABLE_UNEXPECTED; exit 11; fi'
    '; case "$P0" in [1-9]|[1-9][0-9]|1[0-9][0-9]|2[0-4][0-9]|25[0-5]) ;'
    '; *) echo FAN_PWM_UNEXPECTED; exit 12;; esac; RS=0; r() { if [ "$RS" = 1 ]'
    '; then return 0; fi; RS=1; if [ "$(cat $H/pwm1_enable)" != "$E0" ]'
    '; then echo "$E0" > "$H/pwm1_enable"; fi; T=0; c=0'
    '; for z in /sys/class/thermal/thermal_zone*/temp; do v=$(cat $z 2>/dev/null)'
    '; case "$v" in \'\'|*[!0-9]*) ;; *) c=$((c+1)); if [ "$v" -gt "$T" ]; then T=$v; fi;'
    '; esac; done; if [ "$P0" != 255 ] && { [ "$c" = 0 ] || [ "$T" -ge 65000 ]; }'
    '; then echo FAN_LEFT_255 tmax=$T zones=$c; else echo "$P0" > "$H/pwm1"; fi'
    '; echo FAN_RESTORED enable=$(cat $H/pwm1_enable) pwm=$(cat $H/pwm1) rpm=$(cat $H/fan1_input) tmax=$T zones=$c'
    '; }; trap r EXIT; trap \'r; exit 13\' HUP INT TERM; echo 255 > "$H/pwm1"'
    '; echo FAN_SET rc=$? pwm=$(cat $H/pwm1); i=0; while [ $i -lt @N@ ]; do sleep 2'
    '; i=$((i+1))'
    "; echo FAN_S i=$i up=$(cut -d' ' -f1 /proc/uptime) rpm=$(cat $H/fan1_input) pwm=$(cat $H/pwm1)"
    '; done )')
# the device command, pinned here in full (tools/dev pins its sha256): one `( ... )`
# subshell over several lines, each well under tools/dev LINE_MAX once wrapped.
FAN_TEST_LINES = [
    ('( H=; n=0; for h in /sys/class/hwmon/hwmon*; do d=$(readlink $h/device/driver)'
     '; if [ "$(cat $h/name 2>/dev/null)" = pwmfan ] && [ "${d##*/}" = pwm-fan ]; then H=$h'
     '; n=$((n+1)); fi; done; if [ "$n" != 1 ]; then echo FAN_NOHWMON n=$n; exit 10; fi'),
    ('E0=$(cat $H/pwm1_enable); P0=$(cat $H/pwm1)'
     "; echo FAN_HWMON=$H enable=$E0 pwm=$P0 rpm=$(cat $H/fan1_input) up=$(cut -d' ' -f1 /proc/uptime)"
     '; if [ "$E0" != 1 ]; then echo FAN_ENABLE_UNEXPECTED; exit 11; fi'
     '; case "$P0" in [1-9]|[1-9][0-9]|1[0-9][0-9]|2[0-4][0-9]|25[0-5]) ;; *) echo FAN_PWM_UNEXPECTED'
     '; exit 12;; esac'),
    ('RS=0; r() { if [ "$RS" = 1 ]; then return 0; fi; RS=1'),
    ('if [ "$(cat $H/pwm1_enable)" != "$E0" ]; then echo "$E0" > "$H/pwm1_enable"; fi; T=0; c=0'
     '; for z in /sys/class/thermal/thermal_zone*/temp; do v=$(cat $z 2>/dev/null)'
     '; case "$v" in \'\'|*[!0-9]*) ;; *) c=$((c+1)); if [ "$v" -gt "$T" ]; then T=$v; fi;; esac; done'),
    ('if [ "$P0" != 255 ] && { [ "$c" = 0 ] || [ "$T" -ge 65000 ]; }'
     '; then echo FAN_LEFT_255 tmax=$T zones=$c; else echo "$P0" > "$H/pwm1"; fi'),
    ('echo FAN_RESTORED enable=$(cat $H/pwm1_enable) pwm=$(cat $H/pwm1) rpm=$(cat $H/fan1_input) tmax=$T zones=$c'
     '; }'),
    ('trap r EXIT; trap \'r; exit 13\' HUP INT TERM; echo 255 > "$H/pwm1"'
     '; echo FAN_SET rc=$? pwm=$(cat $H/pwm1)'),
    ('i=0; while [ $i -lt @N@ ]; do sleep 2; i=$((i+1))'
     "; echo FAN_S i=$i up=$(cut -d' ' -f1 /proc/uptime) rpm=$(cat $H/fan1_input) pwm=$(cat $H/pwm1)"
     '; done )'),
]
FAN_TEST_TEMPLATE = "\n".join(FAN_TEST_LINES)
FAN_OK_OUT = ("FAN_HWMON=/sys/class/hwmon/hwmon3 enable=1 pwm=84 rpm=4340 up=200.1\nFAN_SET rc=0 pwm=255\n"
              "FAN_S i=1 up=202.1 rpm=6100 pwm=255\nFAN_S i=2 up=204.1 rpm=6900 pwm=255\n"
              "FAN_RESTORED enable=1 pwm=84 rpm=6800 tmax=45100 zones=12\n")


def fan_tests(dev, sent):
    """`tools/dev fan-test SECONDS`: the only fan write, as one self-checked fixed string;
    bounds; refusal of every other spelling; host-side verdicts. No network (fake transport)."""
    import hashlib
    fails, total = 0, 0

    def t(ok, what):
        nonlocal fails, total
        fails += not ok
        total += 1
        print(f"{'ok  ' if ok else 'FAIL'} fan     {what}")

    t(dev.FAN_TEST_TEMPLATE == FAN_TEST_TEMPLATE, "FAN_TEST_TEMPLATE is the pinned string")
    t(FAN_TEST_TEMPLATE.replace("\n", "; ") == FAN_TEST_TEMPLATE_ONELINE and FAN_TEST_TEMPLATE.count("\n") == 7,
      "template = the one-line command with 7 '; ' separators as newlines (same commands)")
    t(FAN_TEST_LINES[0].startswith("( H=;") and FAN_TEST_LINES[-1].endswith("; done )")
      and not any(x.endswith(" )") for x in FAN_TEST_LINES[:-1]),
      "one subshell: opens on line 1, closes only at the end of the last line (tools/test_dev_telnet_sim.py runs it)")
    t(hashlib.sha256(FAN_TEST_TEMPLATE.encode()).hexdigest() == dev.FAN_TEST_TEMPLATE_SHA256,
      "tools/dev's sha256 pin matches the pinned string")
    t((dev.FAN_TEST_MIN, dev.FAN_TEST_MAX) == (4, 60), "SECONDS bounds 4..60")
    # Every allowed SECONDS composes to the template with SECONDS // 2 samples and passes the
    # self-check; the denylist refuses each composed string with exactly the two expected rules.
    ok = True
    for n in range(4, 61):
        c = dev.fan_test_cmd(n)
        ok &= c == FAN_TEST_TEMPLATE.replace("@N@", str(n // 2))
        ok &= set(dev.refusals(c)) == {dev.COMPUTED_WHY, dev.GLOB_WRITE_WHY}
        ok &= [x for x in dev._redirect_targets(c) if x != "/dev/null"] == ["$H/pwm1_enable", "$H/pwm1", "$H/pwm1"]
    t(ok, "fan_test_cmd(4..60): template with SECONDS//2 samples; refused by exactly the computed-path and glob-write rules; write targets $H/pwm1_enable, $H/pwm1 x2")
    t(dev.GLOB_WRITE_WHY in dev.refusals("echo 1 > /tmp/a*"), "GLOB_WRITE_WHY is the denylist's own glob-write text")
    c30 = dev.fan_test_cmd(30)
    t("while [ $i -lt 15 ]" in c30 and "@N@" not in c30, "fan-test 30 runs 15 samples of 2 s")
    t("hwmon/hwmon*" in c30 and not re_search(r"hwmon/hwmon\d", c30), "the hwmon is found by glob + name, never a fixed index")
    t("= pwmfan ]" in c30 and "= pwm-fan ]" in c30, "match: hwmon name pwmfan (pwm-fan.c:689) and driver pwm-fan (:747)")
    t("trap r EXIT; trap 'r; exit 13' HUP INT TERM;" in c30
      and c30.index("trap r EXIT") < c30.index("echo 255 >"), "restore trap (EXIT, HUP, INT, TERM) is set before the 255 write")
    t(c30.count("echo 255 >") == 1 and c30.count(">") - c30.count("2>/dev/null") == 3,
      "exactly three writes: 255, the recorded pwm1_enable, the recorded pwm1")
    # Out-of-range or non-integer SECONDS: refused before anything is sent.
    for bad in (0, 1, 3, 61, 120, -4, True, 30.0, "30"):
        n0 = len(sent)
        try:
            dev.fan_test(bad)
            ok = False
        except SystemExit as e:
            ok = len(sent) == n0 and "4..60" in str(e.code)
        t(ok, f"fan-test {bad!r} refused without sending")
    for argv in (["fan-test", "61"], ["fan-test", "3"], ["fan-test", "abc"], ["fan-test"], ["fan-test", "30", "x"]):
        n0 = len(sent)
        sys.argv = ["dev"] + argv
        real_err, sys.stderr = sys.stderr, __import__("io").StringIO()
        try:
            dev.main()
            ok = False
        except SystemExit as e:
            ok = len(sent) == n0 and e.code not in (0, None)
        finally:
            sys.stderr = real_err
        t(ok, f"CLI {' '.join(argv)} exits non-zero without sending")
    # The composed string through run()/sh/s2: refused, nothing sent.
    for argv in (None, ["sh", c30], ["s2", c30]):
        n0 = len(sent)
        try:
            if argv is None:
                dev.run(c30)
            else:
                sys.argv = ["dev"] + argv
                dev.main()
            ok = False
        except SystemExit as e:
            ok = len(sent) == n0 and "refused" in str(e.code)
        t(ok, f"{'run()' if argv is None else argv[0]} of the fan-test command refused without sending")
    # Free-form fan writes (any spelling) stay refused; reads stay allowed.
    for c in ("echo 255 > /sys/class/hwmon/hwmon1/pwm1", "echo 1 > /sys/class/hwmon/hwmon1/pwm1_enable",
              "echo 0 > /sys/devices/platform/fan/hwmon/hwmon1/pwm1",
              "echo 0 >/sys//class/hwmon/hwmon1/./pwm1", "printf 0 | tee /sys/class/hwmon/hwmon1/pwm1",
              "for h in /sys/class/hwmon/hwmon*; do echo 255 > $h/pwm1; done",
              "H=/sys/class/hwmon/hwmon1; echo 0 > $H/pwm1",
              "cd /sys/class/hwmon/hwmon1 && echo 0 > pwm1", "echo 0 > /sys/class/thermal/cooling_device0/cur_state",
              "echo 0 > /sys/class/thermal/thermal_zone0/mode", "echo 3 > /sys/class/hwmon/hwmon1/pwm1_enable",
              FAN_TEST_TEMPLATE.replace("@N@", "15").replace("( H=", "( true; H=")):
        t(dev.refusal(c) is not None and dev.refusal(dev.s2_wrap(c)) is not None, f"refused (sh and s2): {c[:70]}")
    for c in ("for h in /sys/class/hwmon/hwmon*; do echo $(cat $h/name) $(cat $h/fan1_input 2>/dev/null) $(cat $h/pwm1 2>/dev/null); done",
              "cat /sys/class/hwmon/hwmon1/pwm1_enable", "readlink /sys/class/hwmon/hwmon1/device/driver"):
        t(dev.refusal(c) is None, f"read allowed: {c[:70]}")

    # Tampering: a modified template is refused before sending (sha pin); with the pin also
    # updated, the refusal-set / write-target self-check still refuses extra or other writes.
    real_tpl, real_sha, real_send = dev.FAN_TEST_TEMPLATE, dev.FAN_TEST_TEMPLATE_SHA256, dev._send
    dev._send = lambda c, tmo: (sent.append(c), (FAN_OK_OUT, 0))[1]
    mods = [real_tpl.replace("echo 255 >", "echo 0 >"),
            real_tpl.replace("= pwmfan ]", "= bq27z561_0 ]"),
            real_tpl.replace("trap r EXIT; ", ""),
            real_tpl.replace("@N@", "@N@ ]; do :; done; while [ 1 -lt 2")]
    for bad in mods:
        dev.FAN_TEST_TEMPLATE = bad
        n0 = len(sent)
        try:
            dev.fan_test(30)
            ok = False
        except SystemExit as e:
            ok = len(sent) == n0 and "modified" in str(e.code)
        t(ok, f"modified template refused before sending (sha pin): {bad[-50:]!r}")
    selfcheck = [real_tpl.replace("echo 255 > \"$H/pwm1\";", "echo 255 > \"$H/pwm1\"; echo 0 > \"$H/pwm1\";"),
                 real_tpl.replace("echo 255 > \"$H/pwm1\";", "echo 255 > \"$H/fan1_target\";"),
                 real_tpl.replace("echo 255 > \"$H/pwm1\";", "echo 255 > /sys/class/hwmon/hwmon1/pwm1;"),
                 real_tpl.replace("( H=;", "( cat /dev/mem; H=;"),
                 real_tpl.replace("( H=;", "( echo 1 > /tmp/lvr-return; H=;")]
    for bad in selfcheck:
        dev.FAN_TEST_TEMPLATE = bad
        dev.FAN_TEST_TEMPLATE_SHA256 = hashlib.sha256(bad.encode()).hexdigest()
        n0 = len(sent)
        try:
            dev.fan_test(30)
            ok = False
        except SystemExit as e:
            ok = len(sent) == n0 and "self-check failed" in str(e.code)
        t(ok, f"pin-matched but altered writes refused by the self-check: {bad[bad.find('echo 255') - 30:][:60]!r}")
    dev.FAN_TEST_TEMPLATE, dev.FAN_TEST_TEMPLATE_SHA256 = real_tpl, real_sha
    real_refusals = dev.refusals
    for fake in ([], [dev.COMPUTED_WHY], [dev.COMPUTED_WHY, dev.GLOB_WRITE_WHY, dev.SYSFS_WHY]):
        dev.refusals = lambda c, allow_modules=False, f=fake: list(f)
        n0 = len(sent)
        try:
            dev.fan_test(30)
            ok = False
        except SystemExit as e:
            ok = len(sent) == n0 and "self-check failed" in str(e.code)
        t(ok, f"self-check refuses when refusals == {fake}")
    dev.refusals = real_refusals

    # Host-side verdicts (fake device replies); exactly one command, the composed one, is sent.
    def outcome(reply, rc=0, exc=None, seconds=30):
        seen = []

        def f(c, tmo):
            seen.append((c, tmo))
            sent.append(c)
            if exc:
                raise exc
            return reply, rc
        dev._send = f
        err, out = _call(dev.fan_test, seconds)
        return err, out, seen

    err, out, seen = outcome(FAN_OK_OUT)
    t(err is None and seen == [(dev.fan_test_cmd(30), 60)] and "restored" in out,
      f"rise 4340 -> 6900 and restored: exit 0, one command, timeout 2*15+30 ({err}, {seen[:1] and seen[0][1]})")
    err, out, _ = outcome(FAN_OK_OUT, seconds=5)
    t(err is None, "fan-test 5: 2 samples, exit 0")
    err, out, _ = outcome(FAN_OK_OUT.replace("rpm=6100", "rpm=4500").replace("rpm=6900", "rpm=4700"))
    t(err == 3 and "DID NOT RISE" in out, f"rise of 360 rpm: exit 3 ({err})")
    err, out, _ = outcome(FAN_OK_OUT.replace("FAN_S i=1 up=202.1 rpm=6100 pwm=255\nFAN_S i=2 up=204.1 rpm=6900 pwm=255\n", ""))
    t(err == 3, f"no samples: exit 3 ({err})")
    err, out, _ = outcome(FAN_OK_OUT.replace("FAN_RESTORED enable=1 pwm=84", "FAN_RESTORED enable=1 pwm=255"))
    t(err == 4 and "RESTORE NOT CONFIRMED" in out, f"pwm1 not back to 84 without FAN_LEFT_255: exit 4 ({err})")
    err, out, _ = outcome(FAN_OK_OUT.replace("FAN_RESTORED enable=1", "FAN_RESTORED enable=2"))
    t(err == 4, f"pwm1_enable not back: exit 4 ({err})")
    err, out, _ = outcome(FAN_OK_OUT.split("FAN_RESTORED")[0], rc=13)
    t(err == 4, f"no FAN_RESTORED line: exit 4 ({err})")
    err, out, _ = outcome(FAN_OK_OUT.replace("FAN_RESTORED enable=1 pwm=84",
                                             "FAN_LEFT_255 tmax=66000 zones=12\nFAN_RESTORED enable=1 pwm=255"))
    t(err == 5 and "LEFT AT 255" in out, f"hot at restore: FAN_LEFT_255, exit 5 ({err})")
    err, out, _ = outcome(FAN_OK_OUT.replace("FAN_RESTORED enable=1 pwm=84", "FAN_RESTORED enable=1 pwm=255")
                          .replace("rpm=6100", "rpm=4300").replace("rpm=6900", "rpm=4300"))
    t(err == 4, f"restore failure outranks no-rise: exit 4 ({err})")
    for reply, rc in (("FAN_NOHWMON n=0\n", 10), ("FAN_NOHWMON n=2\n", 10),
                      ("FAN_HWMON=/sys/class/hwmon/hwmon3 enable=0 pwm=84 rpm=0 up=1\nFAN_ENABLE_UNEXPECTED\n", 11),
                      ("FAN_HWMON=/sys/class/hwmon/hwmon3 enable=1 pwm=0 rpm=0 up=1\nFAN_PWM_UNEXPECTED\n", 12),
                      ("", 0), ("sh: garbage\n", 2)):
        err, out, _ = outcome(reply, rc)
        t(err == 2 and "nothing written" in out, f"{(reply.splitlines() or ['empty'])[-1][:40]!r}: exit 2, nothing written ({err})")
    err, out, seen = outcome("", exc=TimeoutError("no complete response within 60s"))
    t(err == 6 and "trap restores" in out and len(seen) == 1, f"transport timeout: exit 6, no retry ({err})")
    err, out, seen = outcome("", exc=OSError("connection refused"))
    t(err == 6 and len(seen) == 1, f"connection error: exit 6, no retry ({err})")
    dev._send = real_send
    sys.argv = ["dev", "fan-test", "30"]
    dev._send = lambda c, tmo: (sent.append(c), (FAN_OK_OUT, 0))[1]
    n0 = len(sent)
    err, out = _call(dev.main)
    t(err is None and sent[n0:] == [dev.fan_test_cmd(30)], f"CLI `tools/dev fan-test 30` sends exactly the composed command, exit 0 ({err})")
    dev._send = real_send
    return fails, total


# tools/dev load-step: pinned commands.
LOAD_START = {1: "for c in 4; do nohup nice -n 19 taskset -c $c timeout 180 yes > /dev/null 2>&1 & echo pid=$!; done; echo END",
              8: "for c in 0 1 2 3 4 5 6 7; do nohup nice -n 19 taskset -c $c timeout 180 yes > /dev/null 2>&1 & echo pid=$!; done; echo END"}


class FakeThermal:
    """Scripted device for load-step: temps[i] (m°C, max zone) for the i-th sample, fan, gauge."""

    def __init__(self, dev, temps, fan=(4340, 84), gauge=(90, 360), fail=(), kill_works=True,
                 pids=None, early_exit=False, preexisting=(), outlive=False):
        self.dev, self.temps, self.fan, self.gauge = dev, list(temps), fan, gauge
        self.fail, self.kill_works, self.pids_override = set(fail), kill_works, pids
        self.early_exit, self.outlive = early_exit, outlive
        self.running, self.n, self.sent = set(preexisting), 0, []

    def __call__(self, cmd, timeout):
        self.sent.append(cmd)
        d = self.dev
        if cmd == d.LOAD_SAMPLE_CMD:
            i = self.n
            self.n += 1
            if i in self.fail:
                raise TimeoutError("no complete response")
            t = self.temps[min(i, len(self.temps) - 1)]
            fan = self.fan(i, t) if callable(self.fan) else self.fan
            gauge = self.gauge(i) if callable(self.gauge) else self.gauge
            if self.early_exit and i == 2:
                self.running = set(list(self.running)[1:])
            z = "".join(f"z {n} {t - 1000 * k}\n" for k, n in enumerate(("cpu7-thermal", "cpu4-thermal", "bq27z561-0")))
            return ((f"up={100 + 15 * i}.00\n{z}h bq27z561_0 rpm= pwm=\nh pwmfan rpm={fan[0]} pwm={fan[1]}\n"
                    f"g cap={gauge[0]} temp={gauge[1]} uv=4100000 ua=-850000 uah=3000000\n")
                    + "".join(f"y {p} R\n" for p in sorted(self.running)) + "y 4242 Z\nEND\n", 0)
        if cmd.startswith("for c in "):
            cpus = cmd.split("for c in ")[1].split("; do")[0].split()
            pids = self.pids_override if self.pids_override is not None else [1000 + int(c) for c in cpus]
            self.running |= set(pids)
            return "".join(f"pid={p}\n" for p in pids) + "END\n", 0
        if cmd.startswith("kill "):
            if self.kill_works:
                self.running -= {int(x) for x in cmd.split() if x.isdigit()}
            return "", 0
        raise AssertionError(f"unexpected command {cmd!r}")

    def end_of_timeout(self):
        if not self.outlive:
            self.running.clear()


def load_tests(dev, sent):
    fails, total = 0, 0

    def t(ok, what):
        nonlocal fails, total
        fails += not ok
        total += 1
        print(f"{'ok  ' if ok else 'FAIL'} load    {what}")

    t(dev.load_start_cmd(1, 180) == LOAD_START[1] and dev.load_start_cmd(8, 180) == LOAD_START[8],
      "start commands are the pinned strings (CPU 4 for 1 core; 0-7 for 8)")
    t(dev.LOAD_CPUS == {0: "", 1: "4", 2: "4 5", 4: "4 5 6 7", 8: "0 1 2 3 4 5 6 7"}, "CPU sets 1:4, 2:4-5, 4:4-7, 8:0-7")
    t(all(dev.refusal(dev.load_start_cmd(n, s)) is None for n in (1, 2, 4, 8) for s in (30, 180, 300))
      and dev.refusal(dev.LOAD_SAMPLE_CMD) is None, "start and sample commands pass the denylist (sent through run())")
    t(all(f"timeout {s} yes" in dev.load_start_cmd(n, s) and "nohup nice -n 19 taskset -c $c" in dev.load_start_cmd(n, s)
          for n in (1, 2, 4, 8) for s in (30, 300)), "every worker is bounded on the device (timeout S), HUP-proof, nice 19")
    t((dev.LOAD_ZONE_STOP, dev.LOAD_FAN_TRIP, dev.LOAD_GAUGE_TEMP_STOP, dev.LOAD_CAPACITY_STOP, dev.LOAD_EVERY)
      == (78000, 70000, 420, 45, 15), "thresholds 78 C, fan trip 70 C, gauge 42.0 C, 45 %, every 15 s")
    for args in ((3, 180), (16, 180), (-1, 180), (1, 29), (1, 301), (8, 0), (1, 180.0), (True, 180)):
        n0 = len(sent)
        try:
            dev.load_start_cmd(*args)
            ok = False
        except SystemExit:
            ok = len(sent) == n0
        t(ok, f"load_start_cmd{args} refused")
    for c in ("kill 1004", "kill 1000 1001 1002 1003 1004 1005 1006 1007", "kill -9 1004 1005"):
        t(dev.refusal(c) is None, f"stop command allowed: {c}")

    real_send = dev._send
    clock = [0.0]

    def run(fake, cores, seconds, fan255=None, log=None):
        clock[0] = 0.0
        dev._now = lambda: clock[0]

        def slp(x):
            clock[0] += x
            if clock[0] >= seconds:
                fake.end_of_timeout()
        dev._sleep = slp
        dev._send = fake
        err, out = _call(dev.load_step, cores, seconds, fan255, log)
        dev._send = real_send
        bad = [c for c in fake.sent if dev.refusal(c) is not None]
        return (err or 0), out, bad

    f = FakeThermal(dev, [50000] * 20)
    rc, out, bad = run(f, 1, 60)
    starts = [c for c in f.sent if c.startswith("for c")]
    t(rc == 0 and starts == [dev.load_start_cmd(1, 60)] and not any(c.startswith("kill") for c in f.sent)
      and not bad, f"1 core 60 s at 50 C: exit 0, one start, no kill, every command allowed ({rc})")
    t(f.sent.count(dev.LOAD_SAMPLE_CMD) == 6, f"samples: before + every 15 s + final at S+3 ({f.sent.count(dev.LOAD_SAMPLE_CMD)})")
    t("stop by hand: tools/dev sh 'kill 1004'" in out, "prints the manual stop command with the PIDs")

    f = FakeThermal(dev, [50000, 60000, 70000, 78000, 50000])
    rc, out, _ = run(f, 8, 180)
    kills = [c for c in f.sent if c.startswith("kill")]
    t(rc == 10 and kills == ["kill " + " ".join(str(1000 + c) for c in range(8))] and "STOP (zone)" in out,
      f"zone 78 C: exit 10, one kill with the 8 PIDs ({rc}, {kills})")
    t(f.sent.index(kills[0]) == f.sent.index(dev.LOAD_SAMPLE_CMD, 4) + 1, "the kill follows the breaching sample at once")
    f = FakeThermal(dev, [50000, 77999, 50000])
    rc, out, _ = run(f, 1, 30)
    t(rc == 0, f"77.999 C: no stop ({rc})")

    f = FakeThermal(dev, [50000, 71000, 72000, 72000], fan=(4340, 84))
    rc, out, _ = run(f, 4, 120)
    t(rc == 11 and "pwm1 84" in out and any(c.startswith("kill") for c in f.sent),
      f">= 70 C for two samples with pwm1 84: exit 11 + kill ({rc})")
    f = FakeThermal(dev, [50000, 71000, 60000, 71000, 60000], fan=(4340, 84))
    rc, out, _ = run(f, 4, 60)
    t(rc == 0, f">= 70 C in single samples only: no fan stop ({rc})")
    f = FakeThermal(dev, [50000, 71000, 72000, 72000], fan=(5000, 255))
    rc, out, _ = run(f, 4, 60, fan255=7000)
    t(rc == 11 and "5950" in out, f"pwm 255 but 5000 rpm < 0.85 x 7000: exit 11 ({rc})")
    f = FakeThermal(dev, [50000, 71000, 72000, 72000, 72000], fan=(6500, 255))
    rc, out, _ = run(f, 4, 60, fan255=7000)
    t(rc == 0, f"pwm 255 at 6500 rpm (>= 5950): no stop ({rc})")
    f = FakeThermal(dev, [50000] * 6, fan=lambda i, t: (0 if i == 2 else 4340, 84))
    rc, out, _ = run(f, 2, 60)
    t(rc == 11 and "unreadable or 0" in out, f"fan rpm 0: exit 11 ({rc})")
    f = FakeThermal(dev, [50000] * 6, gauge=lambda i: (90, 421 if i == 3 else 360))
    rc, out, _ = run(f, 2, 60)
    t(rc == 12 and any(c.startswith("kill") for c in f.sent), f"gauge 42.1 C: exit 12 + kill ({rc})")
    f = FakeThermal(dev, [50000] * 6, gauge=(90, 420))
    rc, out, _ = run(f, 2, 60)
    t(rc == 0, f"gauge 42.0 C: no stop ({rc})")
    f = FakeThermal(dev, [50000] * 6, gauge=lambda i: (44 if i == 2 else 50, 360))
    rc, out, _ = run(f, 2, 60)
    t(rc == 13, f"gauge 44 %: exit 13 ({rc})")
    f = FakeThermal(dev, [50000] * 8, fail={2, 3})
    rc, out, _ = run(f, 2, 90)
    t(rc == 14 and any(c.startswith("kill") for c in f.sent), f"two sample failures: exit 14 + kill ({rc})")
    f = FakeThermal(dev, [50000] * 8, fail={2})
    rc, out, _ = run(f, 2, 60)
    t(rc == 0, f"one sample failure: continues, exit 0 ({rc})")
    f = FakeThermal(dev, [50000, 79000], kill_works=False)
    rc, out, _ = run(f, 2, 60)
    kills = [c for c in f.sent if c.startswith("kill")]
    t(rc == 20 and kills == ["kill 1004 1005", "kill -9 1004 1005"] and "LONG-PRESS NOW" in out,
      f"kill ineffective: kill, kill -9, LONG-PRESS, exit 20 ({rc}, {kills})")
    f = FakeThermal(dev, [50000] * 6, pids=[1004])
    rc, out, _ = run(f, 2, 60)
    t(rc == 15 and "kill 1004" in f.sent, f"start reply with 1 of 2 PIDs: kill it, exit 15 ({rc})")
    f = FakeThermal(dev, [50000] * 6, pids=[1, 1004])
    rc, out, _ = run(f, 2, 60)
    t(rc == 15 and not any(c.startswith("kill") and " 1 " in c + " " for c in f.sent), f"PID 1 in the reply: exit 15, never kill 1 ({rc})")
    f = FakeThermal(dev, [50000] * 6, early_exit=True)
    rc, out, _ = run(f, 2, 60)
    t(rc == 15 and "gone early" in out, f"worker gone early: exit 15 ({rc})")
    f = FakeThermal(dev, [50000] * 8, outlive=True)
    rc, out, _ = run(f, 2, 60)
    t(rc == 15 and any(c.startswith("kill") for c in f.sent), f"workers outlive their timeout: kill, exit 15 ({rc})")
    f = FakeThermal(dev, [50000] * 6, preexisting={555})
    rc, out, _ = run(f, 2, 60)
    t(rc == 15 and not any(c.startswith(("for c", "kill")) for c in f.sent), f"yes already running: nothing started or killed, exit 15 ({rc})")
    f = FakeThermal(dev, [79000] * 6)
    rc, out, _ = run(f, 8, 60)
    t(rc == 10 and not any(c.startswith(("for c", "kill")) for c in f.sent), f"hot before the start: nothing started, exit 10 ({rc})")
    f = FakeThermal(dev, [50000] * 30)
    rc, out, _ = run(f, 0, 300)
    t(rc == 0 and not any(c.startswith(("for c", "kill")) for c in f.sent) and f.sent.count(dev.LOAD_SAMPLE_CMD) == 22,
      f"--cores 0: sampling only, 300 s, no start or kill ({rc}, {f.sent.count(dev.LOAD_SAMPLE_CMD)})")
    f = FakeThermal(dev, [50000, 79000])
    rc, out, _ = run(f, 0, 60)
    t(rc == 10 and not any(c.startswith("kill") for c in f.sent), f"--cores 0 hot: exit 10, nothing to kill ({rc})")
    import tempfile
    d = tempfile.mkdtemp(prefix="loadlog-", dir=work_base())
    lp = os.path.join(d, "load.txt")
    f = FakeThermal(dev, [50000] * 6)
    rc, out, _ = run(f, 1, 30, log=lp)
    t(rc == 0 and open(lp).read().count("tmax=50.0(cpu7-thermal)") == 4, "--log appends every sample line")
    for kw in (dict(fan255=50), dict(fan255=99999)):
        f = FakeThermal(dev, [50000])
        n0 = len(f.sent)
        rc, out, _ = run(f, 1, 30, **kw)
        t(rc not in (0, None) and len(f.sent) == n0, f"refused before sending: {kw}")
    for argv in (["load-step", "--cores", "3", "--seconds", "60"], ["load-step", "--cores", "8", "--seconds", "301"],
                 ["load-step", "--cores", "8"], ["load-step", "--seconds", "60"]):
        n0 = len(sent)
        sys.argv = ["dev"] + argv
        real_err, sys.stderr = sys.stderr, __import__("io").StringIO()
        try:
            dev.main()
            ok = False
        except SystemExit as e:
            ok = len(sent) == n0 and e.code not in (0, None)
        finally:
            sys.stderr = real_err
        t(ok, f"CLI {' '.join(argv)} exits non-zero without sending")
    dev._sleep, dev._now = __import__("time").sleep, __import__("time").monotonic
    return fails, total


def re_search(p, s):
    import re
    return re.search(p, s)


def s2_debugfs_tests(dev, sent):
    """s2-up refuses or stops on a debugfs that the /sys rbind would carry."""
    import shutil
    import subprocess
    import tempfile
    import io
    fails, total = 0, 0

    def t(ok, what):
        nonlocal fails, total
        fails += not ok
        total += 1
        print(f"{'ok  ' if ok else 'FAIL'} s2dbg   {what}")

    steps = dict((lab, c) for lab, c, _ in dev.s2_up_steps("stage2-x.tar.gz", 8000, "0" * 64))
    bind, pre = steps["bind"], steps["precheck"]
    guard = ("if grep ' /stage2/' /proc/mounts | grep -q -e ' debugfs ' -e ' /stage2/sys/kernel/debug '; "
             "then echo BINDFAIL debugfs; fi")
    t(bind.endswith("; " + guard) and bind.index("--make-rprivate") < bind.index(guard),
      "bind step ends with the debugfs guard, after --make-rprivate")
    t("DEBUGFS_MOUNTED" in pre and dev.refusal(pre) is None and dev.refusal(bind) is None,
      "precheck has the debugfs check; precheck and bind pass the denylist")
    # The guard's shell, run on the host against sample mount tables.
    tmp = tempfile.mkdtemp(prefix="s2dbg-", dir=work_base())
    base = ("stage2 /stage2 tmpfs rw,size=1048576k,mode=755 0 0\n"
            "proc /stage2/proc proc rw 0 0\nsysfs /stage2/sys sysfs rw 0 0\n"
            "devtmpfs /stage2/dev devtmpfs rw 0 0\ndevpts /stage2/dev/pts devpts rw 0 0\n"
            "tmpfs /stage2/tmp tmpfs rw 0 0\n")
    real = "debugfs /sys/kernel/debug debugfs ro,relatime 0 0\n"
    cases = [(base, False, "plain binds"),
             (real + base, False, "debugfs only in the init namespace (not under /stage2)"),
             (base + "debugfs /stage2/sys/kernel/debug debugfs ro,relatime 0 0\n", True, "debugfs carried to /stage2/sys/kernel/debug"),
             (base + "none /stage2/sys/kernel/debug debugfs rw 0 0\n", True, "debugfs named 'none'"),
             (base + "x /stage2/sys/kernel/debug tmpfs rw 0 0\n", True, "anything at /stage2/sys/kernel/debug"),
             (base + "debugfs /stage2/d debugfs ro 0 0\n", True, "debugfs elsewhere under /stage2")]
    for i, (tab, want, what) in enumerate(cases):
        f = os.path.join(tmp, f"mounts{i}")
        open(f, "w").write(tab)
        p = subprocess.run(["/bin/sh", "-c", guard.replace("/proc/mounts", f)], capture_output=True, text=True)
        # the guard is the bind step's last command, so a quiet guard must exit 0.
        t(("BINDFAIL debugfs" in p.stdout) == want and p.returncode == 0,
          f"bind guard on {what}: {'BINDFAIL' if want else 'quiet'}, exit 0 (rc={p.returncode})")
    pre_grep = pre[pre.index("grep -q -e ' /sys/kernel/debug '"):]
    for i, (tab, want, what) in enumerate([("proc /proc proc rw 0 0\n", False, "no debugfs"),
                                           (real, True, "debugfs at /sys/kernel/debug"),
                                           ("none /d debugfs ro 0 0\n", True, "debugfs at /d")]):
        f = os.path.join(tmp, f"pmounts{i}")
        open(f, "w").write(tab)
        p = subprocess.run(["/bin/sh", "-c", pre_grep.replace("/proc/mounts", f)], capture_output=True, text=True)
        t(("DEBUGFS_MOUNTED" in p.stdout) == want and p.returncode == 0, f"precheck on {what}: {'refuse' if want else 'go'}")
    # s2-up with a fake device: refuses at the precheck, or stops at the bind step.
    real_send, real_isfile, real_open = dev._send, dev.os.path.isfile, open
    shafile = os.path.realpath(os.path.join(dev.OUT_DIR, "stage2-latest.tar.gz")) + ".sha256"
    dev.os.path.isfile = lambda p: True if p.startswith(dev.OUT_DIR) or p == shafile else real_isfile(p)
    dev.open = lambda p, *a, **k: io.StringIO(FAKE_SHA + "  x\n") if p == shafile else real_open(p, *a, **k)

    def dev_with(pre_reply, bind_reply):
        def f(c, tmo):
            sent.append(c)
            if c.startswith("if [ -e /stage2 ]"):
                return pre_reply, 0
            if "--make-rprivate" in c:
                # The real guard (the step's tail) on the host against a mount table, so the
                # fake bind step returns the exit status the device would.
                f = os.path.join(tmp, "bindmounts")
                open(f, "w").write(bind_reply)
                p = subprocess.run(["/bin/sh", "-c", c[c.index(guard):].replace("/proc/mounts", f)],
                                   capture_output=True, text=True)
                return p.stdout, p.returncode
            return fake_device(c, tmo)
        return f
    for pre_reply, bind_reply, want, what in [
            ("FREE\nMemAvailable: 5800000 kB\nDEBUGFS_MOUNTED\n", "", 1, "precheck sees debugfs: refuse, nothing mounted"),
            ("FREE\nMemAvailable: 5800000 kB\n", base + "debugfs /stage2/sys/kernel/debug debugfs ro 0 0\n", 6,
             "bind guard fires: stop at bind"),
            ("FREE\nMemAvailable: 5800000 kB\n", base, 8, "no debugfs: all steps run")]:
        dev._send = dev_with(pre_reply, bind_reply)
        n0 = len(sent)
        err, out = _call(dev.s2_up, "stage2-latest.tar.gz", 8000)
        mine = sent[n0:]
        ok = len(mine) == want and ((err is None) == (want == 8))
        if want == 1:
            ok = ok and "debugfs" in str(err)
        if want == 6:
            ok = ok and "'bind' failed" in str(err) and "--make-rprivate" in mine[-1]
        t(ok, f"s2-up: {what} ({len(mine)} sent, {str(err)[:50]!r})")
    dev._send, dev.os.path.isfile = real_send, real_isfile
    del dev.open
    shutil.rmtree(tmp)
    # No debugfs mount inside the chroot, in any spelling; the canonical one stays allowed via sh.
    for c in ("mount -t debugfs -o ro debugfs /sys/kernel/debug", "mount -t debugfs none /d",
              "busybox mount -t debugfs x /sys/kernel/debug", "mount -o ro -t debugfs debugfs /mnt",
              "mount -t debugfs -o ro debugfs /sys/kern\\\nel/debug"):
        t(dev.refusal(dev.s2_wrap(c)) is not None, f"s2 refuses {c!r}")
    t(dev.refusal("mount -t debugfs -o ro debugfs /sys/kernel/debug") is None, "sh still allows the canonical ro mount")
    for c in ("mount | grep debugfs", "grep debugfs /proc/mounts", "ls /mnt"):
        t(dev.refusal(dev.s2_wrap(c)) is None, f"s2 allows read-only {c!r}")
    n0 = len(sent)
    sys.argv = ["dev", "s2", "mount -t debugfs -o ro debugfs /sys/kernel/debug"]
    err, _ = _call(dev.main)
    t(err is not None and len(sent) == n0, "CLI s2 debugfs mount refused without sending")
    return fails, total


PIN_CPUFREQ_CAP_TEMPLATE_LINES = [
    "( P=/sys/devices/system/cpu/cpufreq; for p in 0 4 7; do echo CF_POLICY$p rel=$(cat $P/policy$p/related_cpus | tr ' ' ,) drv=$(cat $P/policy$p/scaling_driver) gov=$(cat $P/policy$p/scaling_governor) max=$(cat $P/policy$p/scaling_max_freq) cmax=$(cat $P/policy$p/cpuinfo_max_freq) cur=$(cat $P/policy$p/cpuinfo_cur_freq); done",
    'if [ "$(cat $P/policy4/related_cpus)" != "4 5 6" ] || [ "$(cat $P/policy7/related_cpus)" != 7 ] || [ "$(cat $P/policy4/scaling_driver)" != qcom-cpufreq-hw ] || [ "$(cat $P/policy7/scaling_driver)" != qcom-cpufreq-hw ] || [ "$(cat $P/policy4/cpuinfo_max_freq)" != 2419200 ] || [ "$(cat $P/policy7/cpuinfo_max_freq)" != 2841600 ]; then echo CF_PRECHECK_FAIL; exit 10; fi',
    'echo @G@ > /sys/devices/system/cpu/cpufreq/policy4/scaling_max_freq; echo CF_SET4 rc=$?; echo @P@ > /sys/devices/system/cpu/cpufreq/policy7/scaling_max_freq; echo CF_SET7 rc=$?',
    'g=$(cat $P/policy4/scaling_max_freq); q=$(cat $P/policy7/scaling_max_freq); echo CF_AFTER policy0=$(cat $P/policy0/scaling_max_freq) policy4=$g policy7=$q cur4=$(cat $P/policy4/cpuinfo_cur_freq) cur7=$(cat $P/policy7/cpuinfo_cur_freq); if [ "$g" = @G@ ] && [ "$q" = @P@ ]; then echo CF_OK; else echo CF_MISMATCH; exit 11; fi )',
]
PIN_KX_LOAD_TEMPLATE_LINES = [
    '( [ "$(cat /sys/kernel/kexec_loaded)" = 0 ] || { echo KX_ALREADY_LOADED; exit 10; }; sha256sum /tmp/kx/Image | grep -q \'^@ISHA@ \' && sha256sum /tmp/kx/initrd | grep -q \'^@RSHA@ \' || { echo KX_SHA_BAD; exit 11; }; /sbin/kexec -s -l /tmp/kx/Image --initrd=/tmp/kx/initrd --command-line="@CMDLINE@"; r=$?; echo KX_LOAD rc=$r loaded=$(cat /sys/kernel/kexec_loaded); exit $r )',
]
PIN_KX_EXEC_CMD_LINES = [
    '( d=; for w in /sys/class/watchdog/watchdog*; do n=$(readlink -f $w/device); if [ "${n##*/}" = 17c10000.watchdog ]; then d=/dev/${w##*/}; fi; done',
    'if [ "$(cat /sys/kernel/kexec_loaded)" != 1 ]; then echo KX_REFUSED not-loaded; exit 10; fi; if [ ! -c "$d" ]; then echo KX_REFUSED no-wdt; exit 11; fi; if grep -q -e \' nfs\' -e \' /stage2\' /proc/mounts; then echo KX_REFUSED mounts; exit 12; fi; if [ -n "$(pidof yes)" ]; then echo KX_REFUSED load; exit 13; fi; if [ -e /tmp/lvr-reset ]; then echo KX_REFUSED reset-armed; exit 14; fi',
    'T=0; for z in /sys/class/thermal/thermal_zone*/temp; do v=$(cat $z 2>/dev/null); case "$v" in \'\'|*[!0-9]*) ;; *) if [ "$v" -gt "$T" ]; then T=$v; fi;; esac; done; if [ "$T" = 0 ] || [ "$T" -ge 65000 ]; then echo KX_REFUSED tmax=$T; exit 15; fi',
    'setsid sh -c "exec 3>$d || exit 1; sleep 2; exec /sbin/kexec -e" </dev/null >/tmp/kx/exec.log 2>&1 & echo KX_ARMED wdt=$d tmax=$T )',
]
PIN_KX_UNLOAD_CMD_LINES = [
    '( /sbin/kexec -u; r=$?; echo KX_UNLOAD rc=$r loaded=$(cat /sys/kernel/kexec_loaded); exit $r )',
]
PIN_CPUFREQ_CAP_TEMPLATE = "\n".join(PIN_CPUFREQ_CAP_TEMPLATE_LINES)
PIN_KX_LOAD_TEMPLATE = "\n".join(PIN_KX_LOAD_TEMPLATE_LINES)
PIN_KX_EXEC_CMD = "\n".join(PIN_KX_EXEC_CMD_LINES)
PIN_KX_UNLOAD_CMD = "\n".join(PIN_KX_UNLOAD_CMD_LINES)
PIN_REL = "6.18.55-00019-gce462cdadbad"
PIN_KOSHA = "sha256:" + "e" * 64
PIN_CF_OK_OUT = (
    "CF_POLICY0 rel=0,1,2,3 drv=qcom-cpufreq-hw gov=schedutil max=1804800 cmax=1804800 cur=1804800\n"
    "CF_POLICY4 rel=4,5,6 drv=qcom-cpufreq-hw gov=schedutil max=2419200 cmax=2419200 cur=2419200\n"
    "CF_POLICY7 rel=7 drv=qcom-cpufreq-hw gov=schedutil max=2841600 cmax=2841600 cur=2841600\n"
    "CF_SET4 rc=0\nCF_SET7 rc=0\n"
    "CF_AFTER policy0=1804800 policy4=1478400 policy7=1075200 cur4=1478400 cur7=1075200\nCF_OK\n")


def fixed_cmd_tests(dev, sent):
    """/sys/power denylist; cpufreq-cap; mod-fetch/mod-load/mod-unload; kexec-self.
    Fake transport only: nothing is sent anywhere."""
    import hashlib
    import io
    import tempfile
    fails, total = 0, 0

    def t(ok, what):
        nonlocal fails, total
        fails += not ok
        total += 1
        print(f"{'ok  ' if ok else 'FAIL'} t042    {what}")

    replies = {}

    def fake(cmd, timeout):
        sent.append(cmd)
        for k, v in replies.items():
            if k in cmd:
                return v
        return "ok\n", 0
    real_send = dev._send
    dev._send = fake

    def call(fn, *a):
        """(exit code or return value, stdout, commands sent)."""
        n0 = len(sent)
        buf, real = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            r = fn(*a)
        except SystemExit as e:
            r = e.code if isinstance(e.code, int) or e.code is None else str(e.code)
        finally:
            sys.stdout = real
        return r, buf.getvalue(), sent[n0:]

    def cli(argv):
        n0 = len(sent)
        sys.argv = ["dev"] + argv
        buf, real, rerr = io.StringIO(), sys.stdout, sys.stderr
        sys.stdout, sys.stderr = buf, io.StringIO()
        try:
            dev.main()
            code = 0
        except SystemExit as e:
            code = e.code
        finally:
            sys.stdout, sys.stderr = real, rerr
        return code, sent[n0:]

    def lines_ok(c):
        return dev.transport_refusal(c) is None and \
            max(len(x.encode()) + 1 for x in dev._wrap(c, dev.TAG_MAX).split("\n")[:-1]) < dev.LINE_MAX

    try:
        # ---- (d) /sys/power: every write spelling refused, reads allowed ----
        for c in ("echo mem > /sys/power/state", "echo freeze >/sys/power/state", "printf mem >\"/sys/power/state\"",
                  "echo s2idle > /sys//power/./mem_sleep", "echo 1 > /proc/1/root/sys/power/pm_async",
                  "echo mem | tee /stage2/sys/power/state", "cd /sys/power && echo mem > state",
                  "echo mem > power/state", "cp /tmp/x /sys/power/wakeup_count",
                  "python3 -c \"open('/sys/power/state','w').write('mem')\"", "sh -c 'echo mem > /sys/power/state'",
                  "echo 1 >> /sys/power/wake_lock", "busybox sh -c 'echo disk > /sys/power/disk'"):
            t(dev.POWER_WHY in dev.refusals(c), f"/sys/power write refused with POWER_WHY: {c}")
            t(dev.refusal(dev.s2_wrap(c)) is not None, f"/sys/power write refused inside s2: {c}")
        for c in ("cat /sys/power/state", "cat /sys/power/mem_sleep /sys/power/wakeup_count",
                  "cat /sys/power/state > /tmp/pstate", "ls /sys/power"):
            t(dev.refusal(c) is None, f"/sys/power read allowed: {c}")

        # ---- (a) cpufreq-cap ----
        t(dev.CPUFREQ_CAP_TEMPLATE == PIN_CPUFREQ_CAP_TEMPLATE, "CPUFREQ_CAP_TEMPLATE is the pinned string")
        t(hashlib.sha256(PIN_CPUFREQ_CAP_TEMPLATE.encode()).hexdigest() == dev.CPUFREQ_CAP_TEMPLATE_SHA256,
          "tools/dev's cpufreq-cap sha256 pin matches the pinned string")
        t(dev.CPUFREQ_CAP_VALUES == {"cap": (1478400, 1075200), "uncap": (2419200, 2841600)},
          "values: cap 1478400/1075200, uncap 2419200/2841600 (cpuinfo_max)")
        targets = ["/sys/devices/system/cpu/cpufreq/policy4/scaling_max_freq",
                   "/sys/devices/system/cpu/cpufreq/policy7/scaling_max_freq"]
        for mode, (g, p) in dev.CPUFREQ_CAP_VALUES.items():
            c = dev.cpufreq_cap_cmd(mode)
            t(c == PIN_CPUFREQ_CAP_TEMPLATE.replace("@G@", str(g)).replace("@P@", str(p)),
              f"cpufreq-cap {mode}: template with literal {g}/{p}")
            t(set(dev.refusals(c)) == {dev.SYSFS_WHY}, f"cpufreq-cap {mode}: refused in free form by exactly the sysfs-write rule")
            t([x for x in dev._redirect_targets(c) if x != "/dev/null"] == targets,
              f"cpufreq-cap {mode}: write targets are exactly policy4 and policy7 scaling_max_freq")
            t(c.count(f"echo {g} > ") == 1 and c.count(f"echo {p} > ") == 1 and "policy0/scaling_max_freq;" not in c,
              f"cpufreq-cap {mode}: one write each, policy0 never written")
            pre, wr = c.index("CF_PRECHECK_FAIL; exit 10"), c.index(f"echo {g} > ")
            t(pre < wr and '!= "4 5 6"' in c and "!= 7 ]" in c and c.count("!= qcom-cpufreq-hw") == 2
              and "!= 2419200" in c and "!= 2841600" in c,
              f"cpufreq-cap {mode}: precheck (related_cpus, driver, cpuinfo_max) before any write, exit 10")
            t(c.startswith("( ") and c.endswith(" )") and lines_ok(c), f"cpufreq-cap {mode}: one subshell, every line < LINE_MAX")
            for argv in (["sh", c], ["s2", c]):
                code, mine = cli(argv)
                t(code not in (0, None) and not mine, f"cpufreq-cap {mode} string refused via {argv[0]} without sending")
            replies.clear()
            replies["CF_PRECHECK_FAIL"] = (PIN_CF_OK_OUT.replace("1478400", str(g)).replace("1075200", str(p)), 0)
            r, out, mine = call(dev.cpufreq_cap, mode)
            t(r == 0 and mine == [c], f"cpufreq-cap {mode}: sends exactly the composed command once; exit 0 on readback OK")
        for c in ("echo 1478400 > /sys/devices/system/cpu/cpufreq/policy4/scaling_max_freq",
                  "echo 1075200 > /sys/devices/system/cpu/cpufreq/policy7/scaling_max_freq",
                  "cat /sys/devices/system/cpu/cpufreq/policy4/cpuinfo_max_freq > /sys/devices/system/cpu/cpufreq/policy4/scaling_max_freq",
                  "echo 300000 > /sys/devices/system/cpu/cpufreq/policy0/scaling_max_freq",
                  "echo performance > /sys/devices/system/cpu/cpufreq/policy4/scaling_governor"):
            t(dev.refusal(c) is not None, f"generic cpufreq write stays refused: {c[:70]}")
        replies.clear()
        replies["CF_PRECHECK_FAIL"] = ("CF_POLICY4 rel=4,5 drv=qcom-cpufreq-hw\nCF_PRECHECK_FAIL\n", 10)
        r, out, mine = call(dev.cpufreq_cap, "cap")
        t(r == 2 and "nothing written" in out, "cpufreq-cap: precheck failure -> exit 2, nothing written")
        replies["CF_PRECHECK_FAIL"] = (PIN_CF_OK_OUT.replace("policy7=1075200", "policy7=2841600")
                                       .replace("CF_OK", "CF_MISMATCH"), 11)
        r, out, mine = call(dev.cpufreq_cap, "cap")
        t(r == 3 and "MISMATCH" in out, "cpufreq-cap: readback mismatch -> exit 3")
        replies["CF_PRECHECK_FAIL"] = (PIN_CF_OK_OUT.replace("CF_OK\n", ""), 0)
        r, out, mine = call(dev.cpufreq_cap, "cap")
        t(r == 3, "cpufreq-cap: no CF_OK line -> exit 3 (never reported as capped)")
        replies.clear()
        real_t = dev.CPUFREQ_CAP_TEMPLATE
        for bad in (real_t.replace("@G@ >", "@G@ >>"), real_t.replace("policy7/scaling_max_freq;", "policy0/scaling_max_freq;"),
                    real_t + "; reboot", real_t.replace("!= 7 ]", "!= 7 ] && true")):
            dev.CPUFREQ_CAP_TEMPLATE = bad
            r, out, mine = call(dev.cpufreq_cap, "cap")
            t(not mine and "modified" in str(r), f"cpufreq-cap refuses a modified template ({bad[-40:]!r})")
        dev.CPUFREQ_CAP_TEMPLATE = real_t
        real_refusals = dev.refusals
        for fake_whys in ([], [dev.SYSFS_WHY, dev.GLOB_WRITE_WHY], [dev.COMPUTED_WHY]):
            dev.refusals = lambda c, allow_modules=False, f=fake_whys: list(f)
            r, out, mine = call(dev.cpufreq_cap, "cap")
            t(not mine and "self-check" in str(r), f"cpufreq-cap self-check refuses when refusals == {fake_whys}")
        dev.refusals = real_refusals
        t(dev.refusal(dev.CPUFREQ_STATUS_CMD) is None and not [x for x in dev._redirect_targets(dev.CPUFREQ_STATUS_CMD)
                                                                 if x != "/dev/null"] and lines_ok(dev.CPUFREQ_STATUS_CMD),
          "cpufreq-cap status: read-only, passes the denylist")
        r, out, mine = call(dev.cpufreq_cap, "status")
        t(r == 0 and mine == [dev.CPUFREQ_STATUS_CMD], "cpufreq-cap status sends only the read-only command")
        for argv in (["cpufreq-cap"], ["cpufreq-cap", "max"], ["cpufreq-cap", "cap", "1"]):
            code, mine = cli(argv)
            t(code not in (0, None) and not mine, f"CLI {' '.join(argv)} exits non-zero without sending")

        # ---- (b) modules ----
        tmp = tempfile.mkdtemp(prefix="t042-", dir=work_base())
        allow = os.path.join(tmp, "modules-allow.txt")
        open(allow, "w").write(f"# test allowlist\nrelease {PIN_REL}\n"
                               f"gpucc kernel/drivers/clk/qcom/gpucc-sm8250.ko {PIN_KOSHA}\n"
                               f"msm kernel/drivers/gpu/drm/msm/msm.ko {PIN_KOSHA}\n"
                               f"msm-zap kernel/drivers/gpu/drm/msm/msm.ko zap_preloaded=1 {PIN_KOSHA}\n")
        rel, mods = dev.parse_mod_allow(allow)
        t(rel == PIN_REL and list(mods) == ["gpucc", "msm", "msm-zap"] and mods["msm-zap"][1] == ["zap_preloaded=1"],
          "allowlist parsed: release, labels in order, parameters")
        c = dev.mod_load_cmd(rel, *mods["msm-zap"])
        t(dev.refusals(c) == [dev.MODULE_WHY] and dev.refusals(c, allow_modules=True) == [],
          "mod-load command: refused in free form only by the module rule")
        t(f"( f=/lib/modules/{PIN_REL}/kernel/drivers/gpu/drm/msm/msm.ko;" in c and f"sha256sum $f | grep -q '^{'e' * 64} '" in c
          and c.index("sha256sum") < c.index("insmod ") and "MOD_SHA_BAD" in c,
          "mod-load: the .ko sha256 pin is checked on the device before insmod")
        t("\ninsmod $f zap_preloaded=1;" in c and c.count("insmod ") == 1
          and "'^msm ' /proc/modules" in c and c.startswith("( ") and c.endswith(" )") and lines_ok(c),
          "mod-load: one insmod of the listed path with the listed params; /proc/modules name msm; subshell")
        t(f'[ "$(uname -r)" = {PIN_REL} ]' in c and c.index("uname -r") < c.index("insmod"),
          "mod-load: release checked on the device before insmod")
        cu = dev.mod_unload_cmd(mods["gpucc"][0])
        t(dev.refusals(cu) == [dev.MODULE_WHY] and "rmmod gpucc_sm8250;" in cu and " -f" not in cu and lines_ok(cu),
          "mod-unload: rmmod gpucc_sm8250 (no -f), refused in free form only by the module rule")
        r, out, mine = call(dev.mod_op, "load", "msm-zap", allow)
        t(mine == [c], "mod-load msm-zap sends exactly its command once")
        r, out, mine = call(dev.mod_op, "unload", "gpucc", allow)
        t(mine == [cu], "mod-unload gpucc sends exactly its command once")
        r, out, mine = call(dev.mod_op, "load", "arm-smmu", allow)
        t(not mine and "not in the module allowlist" in str(r), "mod-load of an unlisted label: refused without sending")
        for argv in (["sh", c], ["s2", c], ["sh", "insmod /lib/modules/x/kernel/a.ko"], ["sh", "rmmod msm"],
                     ["sh", "modprobe msm"]):
            code, mine = cli(argv)
            t(code not in (0, None) and not mine, f"free-form module command refused via {argv[0]}: {argv[1][:50]}")
        bad_lines = {
            "no release": "msm kernel/drivers/gpu/drm/msm/msm.ko\n",
            "two releases": f"release {PIN_REL}\nrelease {PIN_REL}\nmsm kernel/a/msm.ko\n",
            "traversal": f"release {PIN_REL}\nx kernel/../../../tmp/evil.ko\n",
            "absolute": f"release {PIN_REL}\nx /tmp/evil.ko\n",
            "not .ko": f"release {PIN_REL}\nx kernel/drivers/a/b.o\n",
            "param $": f"release {PIN_REL}\nx kernel/drivers/a/b.ko p=$(reboot)\n",
            "param quote": f"release {PIN_REL}\nx kernel/drivers/a/b.ko p='1'\n",
            "param ;": f"release {PIN_REL}\nx kernel/drivers/a/b.ko p=1;reboot\n",
            "dup label": f"release {PIN_REL}\nx kernel/drivers/a/b.ko\nx kernel/drivers/a/c.ko\n",
            "bad label": f"release {PIN_REL}\nX;y kernel/drivers/a/b.ko\n",
            "bad release": "release 6.18;reboot\nx kernel/drivers/a/b.ko\n",
        }
        bad_lines = {k: "\n".join(l + (" " + PIN_KOSHA if l.split()[:1] and l.split()[0] != "release" else "")
                                   for l in v.splitlines()) + "\n" for k, v in bad_lines.items()}
        bad_lines["no sha256 pin"] = f"release {PIN_REL}\nx kernel/drivers/a/b.ko\n"
        bad_lines["two sha256 pins"] = f"release {PIN_REL}\nx kernel/drivers/a/b.ko {PIN_KOSHA} {PIN_KOSHA}\n"
        bad_lines["short sha256 pin"] = f"release {PIN_REL}\nx kernel/drivers/a/b.ko sha256:{'e' * 63}\n"
        bad_lines["upper-case sha256 pin"] = f"release {PIN_REL}\nx kernel/drivers/a/b.ko sha256:{'E' * 64}\n"
        for deny in ("nvmem/nvmem_qcom-spmi-sdam.ko", "nvmem/nvmem_sec_qfprom.ko", "power/reset/qcom-pon.ko",
                     "input/misc/pm8941-pwrkey.ko", "spi/spidev.ko", "i2c/i2c-dev.ko",
                     "power/supply/bq27xxx_battery.ko", "power/supply/qcom_smbx_charger.ko",
                     "power/reset/reboot-mode.ko", "power/reset/nvmem-reboot-mode.ko", "uio/uio.ko",
                     "misc/quest2-syncboss.ko", "power/supply/smb2.ko"):
            bad_lines[f"MOD_DENY {deny}"] = f"release {PIN_REL}\nx kernel/drivers/{deny} {PIN_KOSHA}\n"
        for what, text in bad_lines.items():
            p = os.path.join(tmp, "bad.txt")
            open(p, "w").write(text)
            r, out, mine = call(dev.parse_mod_allow, p)
            t(isinstance(r, str) and not mine, f"allowlist refused: {what}")
        for ok_mod in ("gpu/drm/msm/msm.ko", "clk/qcom/gpucc-sm8250.ko", "nvmem/nvmem_qfprom.ko",
                       "iommu/arm/arm-smmu/arm_smmu.ko", "gpu/drm/drm_exec.ko"):
            p = os.path.join(tmp, "ok.txt")
            open(p, "w").write(f"release {PIN_REL}\nm kernel/drivers/{ok_mod} {PIN_KOSHA}\n")
            r, out, mine = call(dev.parse_mod_allow, p)
            t(isinstance(r, tuple), f"allowlist accepts {ok_mod}")
        # the shipped example GPU allowlist (placeholder hashes)
        ex = os.path.join(os.path.dirname(HERE), "examples", "module-allowlist.txt")
        rel6, m6 = dev.parse_mod_allow(ex)
        gpu_mods = ["gpucc_sm8250", "arm_smmu", "backlight", "drm_panel_orientation_quirks", "drm", "gpu_sched", "drm_exec",
                "drm_gpuvm", "ocmem", "mdt_loader", "ubwc_config", "msm"]
        t(rel6 == "6.18.0-example" and list(m6)[:2] == ["gpucc", "arm-smmu"]
          and list(m6)[-2:] == ["msm", "msm-zappre"] and len(m6) == 13
          and sorted({dev._mod_name(v[0]) for v in m6.values()}) == sorted(gpu_mods),
          "example GPU allowlist: placeholder release, exactly the 12 GPU-path modules")
        t(len({v[3] for v in m6.values()}) == 12, "example: every module carries its own sha256 pin (msm twice, same pin)")
        t(all("separate_gpu_kms=1" in m6[k][1] for k in ("msm", "msm-zappre")) and "zap_preloaded=1" in m6["msm-zappre"][1]
          and "zap_preloaded=1" not in m6["msm"][1] and all(not v[1] for k, v in m6.items() if not k.startswith("msm")),
          "example: msm lines carry separate_gpu_kms=1 (cmdline does not reach modules), zap_preloaded only on msm-zappre")
        cm = dev.mod_load_cmd(rel6, *m6["msm"])
        t(cm.index("bound:3da0000.iommu") < cm.index("insmod ") and "[ -e /sys/bus/platform/devices/3da0000.iommu/driver ]" in cm
          and "grep -q '^gpucc_sm8250 ' /proc/modules ||" in cm and "exit 13" in cm,
          "msm insmod only after gpucc_sm8250 is loaded and 3da0000.iommu is bound (checked on the device)")
        t(all(dev.refusals(dev.mod_load_cmd(rel6, *v)) == [dev.MODULE_WHY] and lines_ok(dev.mod_load_cmd(rel6, *v))
              for v in m6.values()), "every example insmod: refused in free form only by the module rule; lines < LINE_MAX")
        for what, extra in (("bound ..", "bound:../../x"), ("bound $", "bound:$(x)"), ("loaded -", "loaded:a-b"),
                            ("needs ;", "loaded:a;reboot")):
            p = os.path.join(tmp, "bad.txt")
            open(p, "w").write(f"release {PIN_REL}\nx kernel/drivers/a/b.ko {PIN_KOSHA} {extra}\n")
            r, out, mine = call(dev.parse_mod_allow, p)
            t(isinstance(r, str) and not mine, f"allowlist refused: precondition {what}")
        steps = dev.mod_fetch_steps(PIN_REL, "c" * 64, 8000)
        t([s[0] for s in steps] == ["precheck", "fetch", "verify", "members", "extract"]
          and all(dev.refusal(s[1]) is None and lines_ok(s[1]) for s in steps),
          "mod-fetch: five steps, each passes the denylist and the line limit")
        t(f"/modules-{PIN_REL}.tar.gz" in steps[1][1] and f"grep -q '^{'c' * 64} '" in steps[2][1]
          and f"-e '^lib/modules/{PIN_REL}/'" in steps[3][1] and f"mv /tmp/kx/x/lib/modules/{PIN_REL} /lib/modules/" in steps[4][1]
          and " -C / " not in steps[4][1] and " -C /\n" not in steps[4][1],
          "mod-fetch: URL by release, pinned sha on the device, member confinement, unpack via /tmp/kx/x into /lib/modules")
        replies.clear()
        replies["EXISTS"] = ("rel=6.18.55-00017-g27ddc1daa3f7\nFREE\nMemAvailable: 5800000 kB\n", 0)
        r, out, mine = call(dev.mod_fetch, PIN_REL, "c" * 64, 8000)
        t(len(mine) == 1 and "not" in str(r), "mod-fetch: release mismatch -> stops after the precheck")
        replies["EXISTS"] = (f"rel={PIN_REL}\nEXISTS\n", 0)
        r, out, mine = call(dev.mod_fetch, PIN_REL, "c" * 64, 8000)
        t(len(mine) == 1 and "already exists" in str(r), "mod-fetch: /lib/modules/REL exists -> stops after the precheck")
        replies["EXISTS"] = (f"rel={PIN_REL}\nFREE\n", 0)
        replies["SHA_OK"] = ("SHA_BAD\n", 1)
        r, out, mine = call(dev.mod_fetch, PIN_REL, "c" * 64, 8000)
        t(len(mine) == 3 and "verify" in str(r), "mod-fetch: sha mismatch -> stops at verify, nothing unpacked")
        replies["SHA_OK"] = ("SHA_OK\n", 0)
        replies["OUTSIDE="] = ("OUTSIDE=0\n", 0)
        r, out, mine = call(dev.mod_fetch, PIN_REL, "c" * 64, 8000)
        t(len(mine) == 5 and r in (0, None), "mod-fetch: all five steps on success")
        replies.clear()
        for a in ((PIN_REL, "C" * 64, 8000), ("6.18;reboot", "c" * 64, 8000), (PIN_REL, "c" * 63, 8000),
                  (PIN_REL, "c" * 64, 80)):
            r, out, mine = call(dev.mod_fetch, *a)
            t(not mine and isinstance(r, str), f"mod-fetch refuses bad arguments {a[0][:12]} {a[1][:4]}.. {a[2]}")
        import shutil
        shutil.rmtree(tmp)

        # ---- (c) kexec-self ----
        t(dev.KX_LOAD_TEMPLATE == PIN_KX_LOAD_TEMPLATE and dev.KX_EXEC_CMD == PIN_KX_EXEC_CMD
          and dev.KX_UNLOAD_CMD == PIN_KX_UNLOAD_CMD, "kexec-self load/exec/unload are the pinned strings")
        t(all(hashlib.sha256(x.encode()).hexdigest() == dev.KX_SHA256[k] for k, x in
              (("load", PIN_KX_LOAD_TEMPLATE), ("exec", PIN_KX_EXEC_CMD), ("unload", PIN_KX_UNLOAD_CMD))),
          "kexec-self sha256 pins match")
        cmdline = ("rdinit=/init clk_ignore_unused pd_ignore_unused regulator_ignore_unused printk.devkmsg=on "
                   "panic=0 log_buf_len=4M msm.separate_gpu_kms=1 lvr_kexec=1")
        isha, rsha = "1" * 64, "2" * 64
        cl = dev.kx_load_cmd(isha, rsha, cmdline)
        t(dev.refusals(cl) == [dev.KEXEC_WHY] and lines_ok(cl), "kexec-self load: refused in free form only by the kexec rule; line < LINE_MAX")
        t(f"grep -q '^{isha} '" in cl and f"grep -q '^{rsha} '" in cl and cl.index(isha) < cl.index("/sbin/kexec -s -l")
          and f'--command-line="{cmdline}"' in cl and "--dtb" not in cl and " -c " not in cl,
          "kexec-self load: shas re-checked before kexec -s -l (kexec_file_load, same DT, no --dtb)")
        t('"$(cat /sys/kernel/kexec_loaded)" = 0 ]' in cl, "kexec-self load: refuses if an image is already loaded")
        t(set(dev.refusals(PIN_KX_EXEC_CMD)) == dev.KX_EXEC_WHYS and lines_ok(PIN_KX_EXEC_CMD)
          and [x for x in dev._redirect_targets(PIN_KX_EXEC_CMD) if x != "/dev/null"] == ["/tmp/kx/exec.log"],
          "kexec-self exec: exactly the expected refusals; only write target /tmp/kx/exec.log; lines < LINE_MAX")
        e = PIN_KX_EXEC_CMD
        arm = e.index("exec 3>$d")
        t(all(e.index(k) < arm for k in ("KX_REFUSED not-loaded", "KX_REFUSED no-wdt", "KX_REFUSED mounts",
                                         "KX_REFUSED load", "KX_REFUSED reset-armed", "KX_REFUSED tmax")),
          "kexec-self exec: every refusal check comes before the watchdog is opened")
        t('= 17c10000.watchdog ]' in e and '[ ! -c "$d" ]' in e and "-ge 65000 ]" in e and '[ "$T" = 0 ]' in e
          and "' nfs'" in e and "' /stage2'" in e and "pidof yes" in e,
          "kexec-self exec: qcom_wdt by device path, char device, tmax < 65 C and readable, no NFS/stage 2/load")
        t('"exec 3>$d || exit 1; sleep 2; exec /sbin/kexec -e"' in e and "setsid sh -c" in e and e.count("kexec -e") == 1,
          "kexec-self exec: watchdog on fd 3 in the process that execs kexec -e, detached, 2 s delay")
        t(dev.refusals(PIN_KX_UNLOAD_CMD) == [dev.KEXEC_WHY], "kexec-self unload: refused in free form only by the kexec rule")
        t(dev.refusals(dev.KX_STATUS_CMD) == [dev.KEXEC_WHY] and not [x for x in dev._redirect_targets(dev.KX_STATUS_CMD)
                                                                      if x != "/dev/null"],
          "kexec-self status: read-only, refused in free form only by the kexec rule")
        fc = dev.kx_fetch_cmd(isha, rsha, 8000)
        t(dev.refusal(fc) is None and lines_ok(fc) and "/Image " in fc and "/initramfs " in fc and "KX_SHA_BAD" in fc,
          "kexec-self fetch: allowed, fetches Image + initramfs, checks both pins on the device")
        for bad in ("", "panic=0 lvr_kexec=1", "rdinit=/init panic=0", "rdinit=/init lvr_kexec=1",
                    "rdinit=/init panic=0 lvr_kexec=1 init=/bin/sh", "rdinit=/init panic=0 lvr_kexec=1 rdinit=/x",
                    'rdinit=/init panic=0 lvr_kexec=1 a="b"', "rdinit=/init panic=0 lvr_kexec=1 $(reboot)",
                    "rdinit=/init  panic=0 lvr_kexec=1", "rdinit=/init panic=0 lvr_kexec=1;reboot",
                    "rdinit=/init panic=0 lvr_kexec=1 `x`", "rdinit=/init panic=0 lvr_kexec=1 x=" + "a" * 600):
            r, out, mine = call(dev.kexec_self, "load", isha, rsha, bad)
            t(not mine and isinstance(r, str), f"kexec-self load refuses cmdline {bad[:50]!r}")
        for a in (("load", "x", rsha, cmdline), ("load", isha, None, cmdline), ("fetch", isha, "G" * 64, None)):
            r, out, mine = call(dev.kexec_self, *a)
            t(not mine and isinstance(r, str), f"kexec-self {a[0]} refuses bad shas")
        replies.clear()
        replies["/sbin/kexec -s -l"] = ("KX_LOAD rc=0 loaded=1\n", 0)
        r, out, mine = call(dev.kexec_self, "load", isha, rsha, cmdline)
        t(r == 0 and mine == [cl], "kexec-self load sends exactly the composed command once")
        replies["KX_ARMED"] = ("KX_ARMED wdt=/dev/watchdog0 tmax=41000\n", 0)
        r, out, mine = call(dev.kexec_self, "exec")
        t(r == 0 and mine == [PIN_KX_EXEC_CMD] and "1d6b:0105" in out, "kexec-self exec: armed -> exit 0 with the host checklist")
        replies["KX_ARMED"] = ("KX_REFUSED tmax=66000\n", 15)
        r, out, mine = call(dev.kexec_self, "exec")
        t(r == 2 and "nothing armed" in out, "kexec-self exec: device refusal -> exit 2")
        replies.clear()
        for name, val in (("KX_EXEC_CMD", PIN_KX_EXEC_CMD.replace("65000", "95000")),
                          ("KX_EXEC_CMD", PIN_KX_EXEC_CMD.replace("sleep 2; ", "")),
                          ("KX_UNLOAD_CMD", PIN_KX_UNLOAD_CMD + "; reboot"),
                          ("KX_LOAD_TEMPLATE", PIN_KX_LOAD_TEMPLATE.replace(" -s -l", " -c -l"))):
            real = getattr(dev, name)
            setattr(dev, name, val)
            act = {"KX_EXEC_CMD": "exec", "KX_UNLOAD_CMD": "unload", "KX_LOAD_TEMPLATE": "load"}[name]
            r, out, mine = call(dev.kexec_self, act, isha, rsha, cmdline)
            setattr(dev, name, real)
            t(not mine and "modified" in str(r), f"kexec-self {act} refuses a modified string")
        real_refusals = dev.refusals
        dev.refusals = lambda c, allow_modules=False: [dev.KEXEC_WHY]
        r, out, mine = call(dev.kexec_self, "exec")
        dev.refusals = real_refusals
        t(not mine and "self-check" in str(r), "kexec-self exec self-check refuses when the denylist changed")
        for c in ("kexec -e", "/sbin/kexec -e", "/sbin/kexec -s -l /tmp/kx/Image", "kexec -u",
                  "exec 3>/dev/watchdog0", "cat /dev/watchdog", e, cl):
            code, mine = cli(["sh", c])
            t(code not in (0, None) and not mine, f"free-form kexec/watchdog refused via sh: {c[:50]}")
        for argv in (["kexec-self"], ["kexec-self", "jump"], ["kexec-self", "load"]):
            code, mine = cli(argv)
            t(code not in (0, None) and not mine, f"CLI {' '.join(argv)} exits non-zero without sending")
        # Existing pinned behaviour unchanged (must not move the other fixed strings).
        t(dev.fan_test_cmd(30) and dev.RETURN_CMD == RETURN_CMD and dev.HEARTBEAT_CMD == HEARTBEAT_CMD,
          "fan-test, return-android and heartbeat strings unchanged")
    finally:
        dev._send = real_send
        replies.clear()
    return fails, total



WDT_CMDLINE = ("rdinit=/init clk_ignore_unused pd_ignore_unused regulator_ignore_unused printk.devkmsg=on "
                    "panic=10 log_buf_len=4M msm.separate_gpu_kms=1 watchdog.open_timeout=90 initcall_debug "
                    "ignore_loglevel lvr_kexec=1")
# The same cmdline with panic=0 and without initcall_debug/ignore_loglevel.
WDT_CMDLINE_PANIC0 = ("rdinit=/init clk_ignore_unused pd_ignore_unused regulator_ignore_unused printk.devkmsg=on "
                     "panic=0 log_buf_len=4M msm.separate_gpu_kms=1 watchdog.open_timeout=90 lvr_kexec=1")


def kexec_cmdline_tests(dev, sent):
    """kexec-self load accepts exactly the diagnostic tokens (watchdog.open_timeout=90,
    initcall_debug, ignore_loglevel, panic=10|0) on top of the base tokens; everything else
    (watchdog variants, extra tokens) is refused before sending, and the general
    denylist still refuses every free-form watchdog command. Fake transport only."""
    import io
    fails, total = 0, 0

    def t(ok, what):
        nonlocal fails, total
        fails += not ok
        total += 1
        print(f"{'ok  ' if ok else 'FAIL'} t048    {what}")

    def fake(cmd, timeout):
        sent.append(cmd)
        return ("KX_LOAD rc=0 loaded=1\n", 0) if "/sbin/kexec -s -l" in cmd else ("ok\n", 0)
    real_send = dev._send
    dev._send = fake

    def call(fn, *a):
        n0 = len(sent)
        buf, real = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            r = fn(*a)
        except SystemExit as e:
            r = e.code if isinstance(e.code, int) or e.code is None else str(e.code)
        finally:
            sys.stdout = real
        return r, buf.getvalue(), sent[n0:]

    def cli(argv):
        n0 = len(sent)
        sys.argv = ["dev"] + argv
        buf, real, rerr = io.StringIO(), sys.stdout, sys.stderr
        sys.stdout, sys.stderr = buf, io.StringIO()
        try:
            dev.main()
            code = 0
        except SystemExit as e:
            code = e.code
        finally:
            sys.stdout, sys.stderr = real, rerr
        return code, sent[n0:]

    def lines_ok(c):
        return dev.transport_refusal(c) is None and \
            max(len(x.encode()) + 1 for x in dev._wrap(c, dev.TAG_MAX).split("\n")[:-1]) < dev.LINE_MAX

    isha, rsha = "1" * 64, "2" * 64
    base = ("rdinit=/init clk_ignore_unused pd_ignore_unused regulator_ignore_unused printk.devkmsg=on "
            "panic=0 log_buf_len=4M msm.separate_gpu_kms=1 lvr_kexec=1")
    try:
        # ---- accepted: the diagnostic tokens, exactly ----
        accept = [
            (WDT_CMDLINE, True), (WDT_CMDLINE_PANIC0, True), (base, False),
            (base.replace("panic=0", "panic=10"), False),
            (base.replace("lvr_kexec=1", "initcall_debug lvr_kexec=1"), False),
            (base.replace("lvr_kexec=1", "ignore_loglevel lvr_kexec=1"), False),
            (base.replace("lvr_kexec=1", "watchdog.open_timeout=90 lvr_kexec=1"), True),
            ("rdinit=/init panic=10 lvr_kexec=1", False),
            ("rdinit=/init lvr_kexec=1 watchdog.open_timeout=90 panic=0", True),
            ("rdinit=/init clk_ignore_unused panic=0 log_buf_len=4M lvr_kexec=1", False),  # telnet-sim cmdline
        ]
        for c, wdt in accept:
            want = {dev.KEXEC_WHY, dev.HWTOOL_WHY} if wdt else {dev.KEXEC_WHY}
            cl = dev.kx_load_cmd(isha, rsha, c)
            r, out, mine = call(dev.kexec_self, "load", isha, rsha, c)
            t(r == 0 and mine == [cl] and f'--command-line="{c}"' in cl and set(dev.refusals(cl)) == want
              and dev.kx_load_whys(isha, rsha, c) == want and lines_ok(cl),
              f"kexec-self load accepts {c[-60:]!r} (self-check {sorted(want)}), sends it once, line < LINE_MAX")
        code, mine = cli(["kexec-self", "load", "--image-sha", isha, "--initrd-sha", rsha,
                          "--cmdline", WDT_CMDLINE_PANIC0])
        t(code == 0 and len(mine) == 1 and WDT_CMDLINE_PANIC0 in mine[0],
          "CLI kexec-self load with the panic=0 diagnostic cmdline is sent")

        # ---- refused before sending: watchdog variants, other tokens, extra tokens, panic variants ----
        v = WDT_CMDLINE
        refuse = [
            v.replace("open_timeout=90", "open_timeout=0"), v.replace("open_timeout=90", "open_timeout=900"),
            v.replace("open_timeout=90", "open_timeout=9"), v.replace("open_timeout=90", "open_timeout=090"),
            v.replace("open_timeout=90", "open_timeout=90s"), v.replace("open_timeout=90", "open_timeout="),
            v.replace("watchdog.open_timeout=90", "Watchdog.open_timeout=90"),
            v.replace("watchdog.open_timeout=90", "watchdog.handle_boot_enabled=0"),
            v.replace("watchdog.open_timeout=90", "watchdog.handle_boot_enabled=1"),
            v.replace("watchdog.open_timeout=90", "watchdog.stop_on_reboot=0"),
            v.replace("watchdog.open_timeout=90", "nowatchdog"),
            v.replace("watchdog.open_timeout=90", "nmi_watchdog=0"),
            v.replace("watchdog.open_timeout=90", "watchdog"),
            v.replace("watchdog.open_timeout=90", "qcom_wdt.timeout=90"),
            v + " nowatchdog", v + " watchdog.handle_boot_enabled=0", v + " watchdog.open_timeout=0",
            v + " watchdog.open_timeout=90",                    # repeated
            v + " initcall_debug", v + " lvr_kexec=1", v + " panic=10", v + " panic=0",
            v.replace("panic=10", "panic=5"), v.replace("panic=10", "panic=-1"), v.replace("panic=10", "panic=1"),
            v.replace("panic=10 ", ""), v.replace("panic=10", "panic=10 panic=0"),
            v.replace("initcall_debug", "initcall_debug=1"), v.replace("ignore_loglevel", "loglevel=8"),
            v + " identity_dma=1", v + " quest2_syncboss.identity_dma=1", v + " quest2_syncboss.identity_dma=0",
            v + " qcom_scm.download_mode=1", v + " qcom_scm.download_mode=0", v + " qcom_scm.download_mode",
            v + " download_mode=1", v + " foo=1", v + " console=ttyMSM0,115200", v + " earlycon",
            v + " init=/bin/sh", v + " rdinit=/x", v.replace("rdinit=/init ", "") + " rdinit=/init",
            v.replace(" lvr_kexec=1", ""), v + ' a="b"', v + " $(reboot)", v + "; reboot", v + " `x`",
            v.replace(" ", "  ", 1), v + " ",
        ]
        for c in refuse:
            r, out, mine = call(dev.kexec_self, "load", isha, rsha, c)
            t(not mine and isinstance(r, str), f"kexec-self load refuses cmdline ...{c[-58:]!r}")
            code, mine = cli(["kexec-self", "load", "--image-sha", isha, "--initrd-sha", rsha, "--cmdline", c])
            t(code not in (0, None) and not mine, f"CLI kexec-self load refuses ...{c[-58:]!r}")

        # ---- the HWTOOL_WHY exception is attributable only to the exact token ----
        real_refusals = dev.refusals
        dev.refusals = lambda c, allow_modules=False: [dev.KEXEC_WHY, dev.HWTOOL_WHY]
        r, out, mine = call(dev.kexec_self, "load", isha, rsha, WDT_CMDLINE)
        dev.refusals = real_refusals
        t(not mine and "self-check" in str(r),
          "kexec-self load refuses if HWTOOL_WHY persists without the watchdog.open_timeout=90 token")
        dev.refusals = lambda c, allow_modules=False: real_refusals(c) + (["x"] if "open_timeout" in c else [])
        r, out, mine = call(dev.kexec_self, "load", isha, rsha, WDT_CMDLINE)
        dev.refusals = real_refusals
        t(not mine and "self-check" in str(r), "kexec-self load refuses any refusal beyond {kexec, hardware-state}")
        dev.refusals = lambda c, allow_modules=False: [dev.KEXEC_WHY, dev.HWTOOL_WHY]
        r, out, mine = call(dev.kexec_self, "load", isha, rsha, base)
        dev.refusals = real_refusals
        t(not mine and "self-check" in str(r), "kexec-self load without the token still expects only {kexec}")

        # ---- the general denylist is not weakened: free-form watchdog commands stay refused ----
        t((r"\b(i2cset|i2ctransfer|setpci|watchdog|wdctl|rtcwake|hwclock|fbset|stty|spidev_test)\b",
           dev.HWTOOL_WHY) in dev.DENY, "the denylist's watchdog rule (hardware-state write tool) is unchanged")
        for c in ("watchdog.open_timeout=90", "echo watchdog.open_timeout=90",
                  "echo 90 > /sys/module/watchdog/parameters/open_timeout",
                  "echo 0 > /sys/module/watchdog/parameters/handle_boot_enabled",
                  "watchdog -t 10 /dev/watchdog0", "busybox watchdog /dev/watchdog", "wdctl",
                  "exec 3>/dev/watchdog0", "echo V > /dev/watchdog", "cat /dev/watchdog0",
                  "echo 0 > /sys/class/watchdog/watchdog0/pretimeout",
                  "/sbin/kexec -s -l /tmp/kx/Image --command-line=\"" + WDT_CMDLINE + "\"",
                  dev.kx_load_cmd(isha, rsha, WDT_CMDLINE)):
            t(dev.refusal(c) is not None, f"denylist refuses {c[:60]!r}")
            code, mine = cli(["sh", c])
            t(code not in (0, None) and not mine, f"sh refuses {c[:60]!r} without sending")
        for k in ("watchdog", "wdctl"):
            t(dev.refusal(k) == dev.HWTOOL_WHY, f"denylist reason for {k!r} is still {dev.HWTOOL_WHY!r}")
        # Pinned kexec strings unchanged.
        t(dev.KX_LOAD_TEMPLATE == PIN_KX_LOAD_TEMPLATE and dev.KX_EXEC_CMD == PIN_KX_EXEC_CMD
          and dev.KX_UNLOAD_CMD == PIN_KX_UNLOAD_CMD, "kexec-self pinned strings unchanged")
    finally:
        dev._send = real_send
    return fails, total



REBOOT_WARM_CMDLINE = WDT_CMDLINE.replace(" lvr_kexec=1", " reboot=warm lvr_kexec=1")


def reboot_token_tests(dev, sent):
    """kexec-self refuses every reboot= cmdline token, reboot=warm included.
    `cat /sys/kernel/reboot/mode` is allowed as that exact string only; every write to
    /sys/kernel/reboot/* and every other reboot-related command stays refused. Fake transport only."""
    import io
    fails, total = 0, 0

    def t(ok, what):
        nonlocal fails, total
        fails += not ok
        total += 1
        print(f"{'ok  ' if ok else 'FAIL'} t048b   {what}")

    def fake(cmd, timeout):
        sent.append(cmd)
        if "/sbin/kexec -s -l" in cmd:
            return "KX_LOAD rc=0 loaded=1\n", 0
        return ("warm\n", 0) if "reboot/mode" in cmd else ("ok\n", 0)
    real_send = dev._send
    dev._send = fake

    def call(fn, *a):
        n0 = len(sent)
        buf, real = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            r = fn(*a)
        except SystemExit as e:
            r = e.code if isinstance(e.code, int) or e.code is None else str(e.code)
        finally:
            sys.stdout = real
        return r, buf.getvalue(), sent[n0:]

    def cli(argv):
        n0 = len(sent)
        sys.argv = ["dev"] + argv
        buf, real, rerr = io.StringIO(), sys.stdout, sys.stderr
        sys.stdout, sys.stderr = buf, io.StringIO()
        try:
            dev.main()
            code = 0
        except SystemExit as e:
            code = e.code
        finally:
            sys.stdout, sys.stderr = real, rerr
        return code, sent[n0:]

    def lines_ok(c):
        return dev.transport_refusal(c) is None and \
            max(len(x.encode()) + 1 for x in dev._wrap(c, dev.TAG_MAX).split("\n")[:-1]) < dev.LINE_MAX

    isha, rsha = "1" * 64, "2" * 64
    K, H, R = dev.KEXEC_WHY, dev.HWTOOL_WHY, dev.REBOOT_WHY
    try:
        t(("\\b(reboot|poweroff|halt)\\b", R) in dev.RISKY and
          ("/proc/(qkx_bootdone|sysrq-trigger)", "reset trigger — use `tools/dev return-android`") in dev.RISKY,
          "the reboot and reset-trigger rules are unchanged (REBOOT_WHY is the reboot rule's text)")
        # ---- (a) reboot=warm is refused; the diagnostic cmdlines still load ----
        v = REBOOT_WARM_CMDLINE
        t("reboot=warm" not in dev.KX_CMDLINE_TOKENS and not hasattr(dev, "KX_REBOOT_TOKEN")
          and not any(x.startswith("reboot") for x in dev.KX_CMDLINE_TOKENS),
          "no reboot token in KX_CMDLINE_TOKENS, KX_REBOOT_TOKEN gone")
        for c, want in ((WDT_CMDLINE, {K, H}), ("rdinit=/init panic=10 lvr_kexec=1", {K}),
                        ("rdinit=/init panic=0 watchdog.open_timeout=90 lvr_kexec=1", {K, H})):
            cl = dev.kx_load_cmd(isha, rsha, c)
            r, out, mine = call(dev.kexec_self, "load", isha, rsha, c)
            t(r == 0 and mine == [cl] and f'--command-line="{c}"' in cl and set(dev.refusals(cl)) == want
              and dev.kx_load_whys(isha, rsha, c) == want and lines_ok(cl),
              f"kexec-self load accepts ...{c[-55:]!r}, sends it once (self-check {len(want)} reasons)")
        code, mine = cli(["kexec-self", "load", "--image-sha", isha, "--initrd-sha", rsha, "--cmdline", v])
        t(code not in (0, None) and not mine, "CLI kexec-self load with a reboot=warm cmdline is refused, nothing sent")
        for c in [v, "rdinit=/init panic=10 reboot=warm lvr_kexec=1",
                  "rdinit=/init reboot=warm panic=0 watchdog.open_timeout=90 lvr_kexec=1"] + \
                 [v.replace("reboot=warm", x) for x in (
                    "reboot=cold", "reboot=hard", "reboot=soft", "reboot=gpio", "reboot=w", "reboot=c", "reboot=s",
                    "reboot=h", "reboot=g", "reboot=t", "reboot=b", "reboot=e", "reboot=p", "reboot=f", "reboot=a",
                    "reboot=k", "reboot=warm,", "reboot=warm,cold", "reboot=warm,hard", "reboot=cold,warm",
                    "reboot=w,s", "reboot=warm,force", "reboot=Warm", "reboot=WARM", "reboot=warm0", "reboot=",
                    "reboot", "Reboot=warm", "reboot.mode=warm", "reboot_mode=warm", "reboot=warm reboot=warm",
                    "reboot=warm reboot=cold", "reboot=cold reboot=warm", "reboot=warm reboot=force",
                    "reboot=warm,warm", "reboot=s3", "reboot=triple", "reboot=bios", "reboot=pci", "reboot=acpi",
                    "reboot=efi", "reboot=force", "reboot=warm,s3", "reboot=0", "reboot=1")] + [
                    v + " panic_on_warn=1", v + " reboot=warm", v.replace("reboot=warm", "reboot=warm  ")]:
            r, out, mine = call(dev.kexec_self, "load", isha, rsha, c)
            t(not mine and isinstance(r, str), f"kexec-self load refuses ...{c[-52:]!r}")
            code, mine = cli(["kexec-self", "load", "--image-sha", isha, "--initrd-sha", rsha, "--cmdline", c])
            t(code not in (0, None) and not mine, f"CLI kexec-self load refuses ...{c[-52:]!r}")
        # The load self-check does not tolerate REBOOT_WHY at all.
        real_refusals = dev.refusals
        for c in (WDT_CMDLINE, "rdinit=/init panic=10 lvr_kexec=1"):
            dev.refusals = lambda x, allow_modules=False, _top=True: sorted(set(real_refusals(x)) | {R})
            r, out, mine = call(dev.kexec_self, "load", isha, rsha, c)
            dev.refusals = real_refusals
            t(not mine and "self-check" in str(r), f"kexec-self load refuses when REBOOT_WHY appears (...{c[-30:]!r})")

        # ---- (b) exactly `cat /sys/kernel/reboot/mode` is allowed ----
        t(dev.REBOOT_MODE_READ == "cat /sys/kernel/reboot/mode" and dev.refusals(dev.REBOOT_MODE_READ) == []
          and dev.refusal(dev.REBOOT_MODE_READ) is None, "denylist allows exactly `cat /sys/kernel/reboot/mode`")
        code, mine = cli(["sh", "cat /sys/kernel/reboot/mode"])
        t(code in (0, None) and mine == ["cat /sys/kernel/reboot/mode"], "sh sends `cat /sys/kernel/reboot/mode` verbatim once")
        code, mine = cli(["check", "cat /sys/kernel/reboot/mode"])
        t(code in (0, None) and not mine, "check passes `cat /sys/kernel/reboot/mode` (nothing sent)")
        refuse = [
            "cat /sys/kernel/reboot/mode ", " cat /sys/kernel/reboot/mode", "cat  /sys/kernel/reboot/mode",
            "cat /sys/kernel/reboot/mode\n", "cat /sys/kernel/reboot/mode\\\n", "cat /sys//kernel/reboot/mode",
            "cat /sys/kernel/reboot/./mode", "cat /sys/kernel/reboot/mode/", "/bin/cat /sys/kernel/reboot/mode",
            "busybox cat /sys/kernel/reboot/mode", "cat /sys/kernel/reboot/mode; reboot",
            "cat /sys/kernel/reboot/mode && reboot -f", "cat /sys/kernel/reboot/mode || poweroff",
            "cat /sys/kernel/reboot/mode | sh", "cat /sys/kernel/reboot/mode; echo b > /proc/sysrq-trigger",
            "cat /sys/kernel/reboot/mode > /tmp/x", "cat /sys/kernel/reboot/mode 2>&1",
            "cat /sys/kernel/reboot/mode /sys/kernel/reboot/type", "cat /sys/kernel/reboot/type",
            "cat /sys/kernel/reboot/cpu", "cat /sys/kernel/reboot/force", "cat /sys/kernel/reboot/*",
            "cat /sys/kernel/reboot/mod?", "ls /sys/kernel/reboot", "head /sys/kernel/reboot/mode",
            "cat /sys/kernel/reboot/mode $(reboot)", "cat /sys/kernel/reboot/mode `reboot`",
            "echo warm > /sys/kernel/reboot/mode", "echo cold > /sys/kernel/reboot/mode",
            "echo warm >/sys/kernel/reboot/mode", "printf warm > /sys/kernel/reboot/mode",
            "echo warm | tee /sys/kernel/reboot/mode", "cp /tmp/x /sys/kernel/reboot/mode",
            "dd if=/tmp/x of=/sys/kernel/reboot/mode", "echo hard > /sys/kernel/reboot/type",
            "echo 1 > /sys/kernel/reboot/force", "echo 0 > /sys/kernel/reboot/cpu",
            "cd /sys/kernel/reboot && echo warm > mode", "cd /sys/kernel/reboot; cat mode",
            "echo warm > /sys/kernel/reboot/mode; cat /sys/kernel/reboot/mode",
            "cat /sys/kernel/reboot/mode; echo warm > /sys/kernel/reboot/mode",
            "reboot", "reboot -f", "busybox reboot", "/sbin/reboot", "poweroff", "halt", "poweroff -f",
            "echo b > /proc/sysrq-trigger", "echo r > /proc/qkx_bootdone", "kexec -e",
            dev.s2_wrap("cat /sys/kernel/reboot/mode"),
        ]
        for c in refuse:
            t(dev.refusal(c) is not None, f"denylist refuses {c[:60]!r}")
            code, mine = cli(["sh", c])
            t(code not in (0, None) and not mine, f"sh refuses {c[:60]!r} without sending")
        code, mine = cli(["s2", "cat /sys/kernel/reboot/mode"])
        t(code not in (0, None) and not mine, "s2 `cat /sys/kernel/reboot/mode` refused (exception is top-level only)")
        t(dev.refusals("cat /sys/kernel/reboot/mode", True) == [], "allow_modules does not change the exact read")
        # Pinned strings unchanged.
        t(dev.KX_LOAD_TEMPLATE == PIN_KX_LOAD_TEMPLATE and dev.KX_EXEC_CMD == PIN_KX_EXEC_CMD
          and dev.KX_UNLOAD_CMD == PIN_KX_UNLOAD_CMD and dev.RETURN_CMD == RETURN_CMD,
          "kexec-self and return-android pinned strings unchanged")
    finally:
        dev._send = real_send
    return fails, total


# The quest2_warm_reset tokens of the warm-reset kernel.
WARM_TOKENS = "quest2_warm_reset.on_panic=1 quest2_warm_reset.boot_stall_secs=60"


def warm_token_tests(dev, sent):
    """kexec-self load accepts exactly quest2_warm_reset.on_panic=1 and
    quest2_warm_reset.boot_stall_secs=N for integer 20 <= N <= 75 (decimal, no sign, no leading zero),
    each at most once, on top of the tokens; every other value or quest2_warm_reset.* token is
    refused before sending, and the load self-check still expects only {kexec[, hardware-state]}."""
    import io
    fails, total = 0, 0

    def t(ok, what):
        nonlocal fails, total
        fails += not ok
        total += 1
        print(f"{'ok  ' if ok else 'FAIL'} t062    {what}")

    def fake(cmd, timeout):
        sent.append(cmd)
        return ("KX_LOAD rc=0 loaded=1\n", 0) if "/sbin/kexec -s -l" in cmd else ("ok\n", 0)
    real_send = dev._send
    dev._send = fake

    def call(fn, *a):
        n0 = len(sent)
        buf, real = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            r = fn(*a)
        except SystemExit as e:
            r = e.code if isinstance(e.code, int) or e.code is None else str(e.code)
        finally:
            sys.stdout = real
        return r, buf.getvalue(), sent[n0:]

    def cli(argv):
        n0 = len(sent)
        sys.argv = ["dev"] + argv
        buf, real, rerr = io.StringIO(), sys.stdout, sys.stderr
        sys.stdout, sys.stderr = buf, io.StringIO()
        try:
            dev.main()
            code = 0
        except SystemExit as e:
            code = e.code
        finally:
            sys.stdout, sys.stderr = real, rerr
        return code, sent[n0:]

    def lines_ok(c):
        return dev.transport_refusal(c) is None and \
            max(len(x.encode()) + 1 for x in dev._wrap(c, dev.TAG_MAX).split("\n")[:-1]) < dev.LINE_MAX

    isha, rsha = "1" * 64, "2" * 64
    K, H = dev.KEXEC_WHY, dev.HWTOOL_WHY
    v = WDT_CMDLINE
    small = "rdinit=/init panic=10 lvr_kexec=1"
    try:
        t(not any(x.startswith("quest2_warm_reset") for x in dev.KX_CMDLINE_TOKENS)
          and dev.KX_WARM_ON_PANIC == "quest2_warm_reset.on_panic=1"
          and (dev.KX_WARM_STALL_MIN, dev.KX_WARM_STALL_MAX) == (20, 75),
          "KX_CMDLINE_TOKENS unchanged; the warm-reset tokens are on_panic=1 and boot_stall_secs 20..75")
        # ---- accepted ----
        accept = [(v + " " + WARM_TOKENS, {K, H}), (v + " quest2_warm_reset.on_panic=1", {K, H}),
                  (v + " quest2_warm_reset.boot_stall_secs=60", {K, H}),
                  (v.replace(" lvr_kexec=1", " " + WARM_TOKENS + " lvr_kexec=1"), {K, H}),
                  (small + " quest2_warm_reset.boot_stall_secs=20 quest2_warm_reset.on_panic=1", {K}),
                  (small + " quest2_warm_reset.on_panic=1 quest2_warm_reset.boot_stall_secs=75", {K})]
        accept += [(small + f" quest2_warm_reset.boot_stall_secs={n}", {K}) for n in range(20, 76)]
        for c, want in accept:
            cl = dev.kx_load_cmd(isha, rsha, c)
            r, out, mine = call(dev.kexec_self, "load", isha, rsha, c)
            t(r == 0 and mine == [cl] and f'--command-line="{c}"' in cl and set(dev.refusals(cl)) == want
              and dev.kx_load_whys(isha, rsha, c) == want and lines_ok(cl),
              f"kexec-self load accepts ...{c[-62:]!r}, sends it once (self-check {len(want)} reasons)")
        code, mine = cli(["kexec-self", "load", "--image-sha", isha, "--initrd-sha", rsha, "--cmdline",
                          v + " " + WARM_TOKENS])
        t(code == 0 and len(mine) == 1 and WARM_TOKENS in mine[0], "CLI kexec-self load with the warm-reset tokens is sent")
        # ---- refused before sending: out of range, malformed, other values/keys, repeats ----
        bad_vals = ["0", "1", "5", "9", "10", "19", "76", "80", "90", "99", "100", "120", "600", "1000", "020",
                    "060", "0060", "+60", "-60", "60.0", "6e1", "0x3c", "60s", "60 ", "", "sixty", "60,", "60=",
                    "١٢", "2O", "7_5", "75 76"]
        refuse = [small + f" quest2_warm_reset.boot_stall_secs={x}" for x in bad_vals]
        refuse += [small + " " + x for x in (
            "quest2_warm_reset.on_panic=0", "quest2_warm_reset.on_panic=2", "quest2_warm_reset.on_panic=y",
            "quest2_warm_reset.on_panic=Y", "quest2_warm_reset.on_panic=true", "quest2_warm_reset.on_panic=01",
            "quest2_warm_reset.on_panic", "quest2_warm_reset.on_panic=", "quest2_warm_reset.on_panic=1,",
            "Quest2_warm_reset.on_panic=1", "quest2_warm_reset.ON_PANIC=1", "quest2-warm-reset.on_panic=1",
            "quest2_warm_reset_on_panic=1", "on_panic=1", "quest2_warm_reset.boot_stall_secs",
            "quest2_warm_reset.boot_stall=60", "boot_stall_secs=60", "quest2_warm_reset.BOOT_STALL_SECS=60",
            "quest2_warm_reset.disable=1", "quest2_warm_reset.armed=0", "quest2_warm_reset.force_reset=1",
            "quest2_warm_reset.on_panic=1 quest2_warm_reset.on_panic=1",
            "quest2_warm_reset.boot_stall_secs=60 quest2_warm_reset.boot_stall_secs=60",
            "quest2_warm_reset.boot_stall_secs=30 quest2_warm_reset.boot_stall_secs=60",
            "quest2_warm_reset.boot_stall_secs=60 quest2_warm_reset.boot_stall_secs=75",
            "quest2_warm_reset.on_panic=1 quest2_warm_reset.on_panic=0",
            "quest2_warm_reset.on_panic=1 quest2_syncboss.identity_dma=1",
            "quest2_warm_reset.on_panic=1 reboot=warm", "quest2_warm_reset.on_panic=1 panic_on_warn=1",
            "quest2_warm_reset.on_panic=1 hung_task_panic=0", "quest2_warm_reset.on_panic=1 softlockup_panic=1",
            "quest2_warm_reset.on_panic=1;reboot", "quest2_warm_reset.on_panic=$(reboot)",
            "quest2_warm_reset.boot_stall_secs=`id`", "quest2_warm_reset.on_panic=1 ")]
        refuse += [small.replace("rdinit=/init ", "quest2_warm_reset.on_panic=1 rdinit=/init "),
                   "rdinit=/init quest2_warm_reset.on_panic=1 lvr_kexec=1",                  # no panic=
                   "rdinit=/init panic=10 quest2_warm_reset.on_panic=1"]                    # no lvr_kexec=1
        for c in refuse:
            r, out, mine = call(dev.kexec_self, "load", isha, rsha, c)
            t(not mine and isinstance(r, str), f"kexec-self load refuses ...{c[-58:]!r}")
            code, mine = cli(["kexec-self", "load", "--image-sha", isha, "--initrd-sha", rsha, "--cmdline", c])
            t(code not in (0, None) and not mine, f"CLI kexec-self load refuses ...{c[-58:]!r}")
        for x in bad_vals + ["60", "20", "75"]:
            tok = f"quest2_warm_reset.boot_stall_secs={x}"
            t(dev.kx_warm_token_ok(tok) == (x in ("60", "20", "75")), f"kx_warm_token_ok({tok[-12:]!r})")
        # ---- the self-check: the warm tokens add no refusal reason and HWTOOL_WHY stays tied to its token ----
        real_refusals = dev.refusals
        dev.refusals = lambda c, allow_modules=False: real_refusals(c) + (["x"] if "on_panic" in c else [])
        r, out, mine = call(dev.kexec_self, "load", isha, rsha, v + " " + WARM_TOKENS)
        dev.refusals = real_refusals
        t(not mine and "self-check" in str(r), "kexec-self load refuses if the warm tokens ever add a refusal reason")
        dev.refusals = lambda c, allow_modules=False: [K, H]
        r, out, mine = call(dev.kexec_self, "load", isha, rsha, small + " " + WARM_TOKENS)
        dev.refusals = real_refusals
        t(not mine and "self-check" in str(r), "kexec-self load: HWTOOL_WHY without watchdog.open_timeout=90 still refused")
        # ---- the general denylist does not learn the tokens: they are cmdline-only ----
        t(dev.KX_LOAD_TEMPLATE == PIN_KX_LOAD_TEMPLATE and dev.KX_EXEC_CMD == PIN_KX_EXEC_CMD
          and dev.KX_UNLOAD_CMD == PIN_KX_UNLOAD_CMD, "kexec-self pinned strings unchanged")
        for c in ("echo 1 > /sys/module/quest2_warm_reset/parameters/on_panic",
                  "echo 30 > /sys/module/quest2_warm_reset/parameters/boot_stall_secs",
                  "/sbin/kexec -s -l /tmp/kx/Image --command-line=\"" + v + " " + WARM_TOKENS + "\""):
            t(dev.refusal(c) is not None, f"denylist refuses {c[:62]!r}")
            code, mine = cli(["sh", c])
            t(code not in (0, None) and not mine, f"sh refuses {c[:62]!r} without sending")
    finally:
        dev._send = real_send
    return fails, total


# The gauge drain line, sent only by `tools/dev drain`.
DRAIN_CMD_PINNED = ("echo $(cut -d' ' -f1 /proc/uptime) $(cat /sys/class/power_supply/bq27z561-0/capacity) "
                 "$(cat /sys/class/power_supply/bq27z561-0/voltage_now) $(cat /sys/class/power_supply/bq27z561-0/current_now) "
                 "$(cat /sys/class/power_supply/bq27z561-0/charge_now) $(cat /sys/class/power_supply/bq27z561-0/temp)")


def drain_tests(dev, sent):
    """`tools/dev drain` (the >= 60 s spacing of drain samples is the tool's) and return-android's
    "warm reset presumed" path (trigger `armed` at the probe, then silence). Fake transport and clocks only."""
    import io
    import tempfile
    fails, total = 0, 0

    def t(ok, what):
        nonlocal fails, total
        fails += not ok
        total += 1
        print(f"{'ok  ' if ok else 'FAIL'} t065    {what}")

    t(dev.DRAIN_CMD == DRAIN_CMD_PINNED, "DRAIN_CMD is the pinned read-only gauge line")
    t(dev.refusals(dev.DRAIN_CMD) == [] and dev.transport_refusal(dev.DRAIN_CMD) is None,
      "DRAIN_CMD passes the denylist and the transport limits")
    t(dev.refusals(RETURN_PROBE) == [] and "cat /proc/lvr_warm_reset" in dev.RETURN_PROBE,
      "the return probe reads the warm-reset trigger and passes the denylist")

    clock = {"wall": 1760000000.0, "mono": 0.0, "slept": []}
    real = dict(_send=dev._send, _sleep=dev._sleep, _now=dev._now, _wall=dev._wall)

    def sleep(sec):
        clock["slept"].append(sec)
        clock["wall"] += sec
        clock["mono"] += sec
    dev._sleep, dev._now, dev._wall = sleep, (lambda: clock["mono"]), (lambda: clock["wall"])
    reply = {"out": "1726.87 99 4393000 0 3554000 300\n", "rc": 0, "raise": None}

    def fake(cmd, tmo):
        sent.append(cmd)
        if reply["raise"]:
            raise reply["raise"]
        return reply["out"], reply["rc"]
    dev._send = fake
    tmp = tempfile.mkdtemp(prefix="t065-drain-", dir=work_base())
    log = os.path.join(tmp, "drain.txt")

    def cli(argv):
        n0 = len(sent)
        clock["slept"].clear()
        sys.argv = ["dev"] + argv
        buf, rout, rerr = io.StringIO(), sys.stdout, sys.stderr
        sys.stdout, sys.stderr = buf, io.StringIO()
        try:
            dev.main()
            code = 0
        except SystemExit as e:
            code = e.code
        finally:
            sys.stdout, sys.stderr = rout, rerr
        return code, buf.getvalue(), sent[n0:], list(clock["slept"])

    code, out, mine, slept = cli(["drain", "--log", log])
    recs = open(log).read().splitlines()
    t(code in (0, None) and mine == [dev.DRAIN_CMD] and not slept and len(recs) == 1
      and recs[0].startswith("DRAIN epoch=1760000000.0 ") and "gap=-" in recs[0]
      and recs[0].endswith("up_cap_uv_ua_uah_dC=1726.87 99 4393000 0 3554000 300"),
      f"first sample: no wait, one DRAIN line ({recs[-1:]})")
    clock["wall"] += 11
    code, out, mine, slept = cli(["drain", "--log", log])
    recs = open(log).read().splitlines()
    t(code in (0, None) and slept == [49.0] and "waiting 49 s" in out and len(recs) == 2 and "gap=60s" in recs[1],
      f"second sample 11 s later: waits 49 s, logged gap=60s ({slept}, {recs[-1][:60]})")
    clock["wall"] += 100
    code, out, mine, slept = cli(["drain", "--log", log])
    t(code in (0, None) and not slept and "gap=100s" in open(log).read().splitlines()[-1],
      "a sample 100 s later: no wait")
    clock["wall"] -= 500                                          # host clock stepped back
    code, out, mine, slept = cli(["drain", "--log", log])
    t(code in (0, None) and slept == [60.0], f"clock behind the last sample: the full 60 s wait ({slept})")
    # --log may be any path; without it the default is LOG_DIR/drain.log (created on demand)
    dflt = os.path.join(os.path.dirname(log), "dflt", "drain.log")
    dev.LOG_DIR = os.path.dirname(dflt)
    code, out, mine, slept = cli(["drain"])
    t(code in (0, None) and len(mine) == 1 and os.path.isfile(dflt),
      "drain without --log writes LOG_DIR/drain.log (directory created)")
    n = len(open(log).read().splitlines())
    reply["raise"] = TimeoutError("no complete response")
    code, out, mine, slept = cli(["drain", "--log", log])
    t(code not in (0, None) and "nothing logged" in str(code) and len(open(log).read().splitlines()) == n,
      "transport failure: non-zero, nothing logged")
    reply["raise"] = None
    reply["out"], reply["rc"] = "cat: /sys/class/power_supply/bq27z561-0/capacity: No such file\n171.8\n", 1
    code, out, mine, slept = cli(["drain", "--log", log])
    last = open(log).read().splitlines()[-1]
    t(code == 1 and "rc=1" in last and " | 171.8" in last, "a failing read: logged with rc=1 and exit 1")

    # return-android: trigger `armed` at the probe, request confirmed, then no reply
    probe_armed = "kernel=6.18.55-00031-gd77cd327d19b\ntimeout=600 pid=77\nreset=\nwdt=\nwarm=\ntrig=armed\nEND\n"
    polls = {"seq": []}

    def ra_dev(probe, seq):
        n = [0]

        def f(c, tmo):
            sent.append(c)
            if c == dev.RETURN_PROBE:
                return probe, 0
            if c == dev.RETURN_CMD:
                return "return=requested\nreset=\nEND\n", 0
            if c == dev.RETURN_POLL:
                r = seq[min(n[0], len(seq) - 1)]
                n[0] += 1
                if isinstance(r, Exception):
                    raise r
                return r, 0
            if "power_supply/battery" in c:
                return "sh: cd: can't cd\n", 2
            return "?\n", 0
        return f

    def ra(probe, seq):
        dev._send = ra_dev(probe, seq)
        clock["mono"] = 0.0
        n0 = len(sent)
        buf, rout = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            r = dev.return_android()
            code = None if not r else r
        except SystemExit as e:
            code = e.code if e.code not in (0, None) else None
        finally:
            sys.stdout = rout
        return code, buf.getvalue(), sent[n0:]
    empty = "reset=\nwdt=\nwarm=\nEND\n"
    code, out, mine = ra(probe_armed, (OSError("gone"),))
    t(code is None and "warm reset presumed" in out and "LVR-WARM-MARKER" in out
      and mine.count(dev.RETURN_POLL) == dev.RETURN_LOST_PRESUMED,
      f"trigger armed, no reply after the request: 'warm reset presumed', exit 0 after "
      f"{mine.count(dev.RETURN_POLL)} polls ({code})")
    code, out, mine = ra(probe_armed, (empty, empty, OSError("gone")))
    t(code is None and "warm reset presumed" in out and mine.count(dev.RETURN_POLL) == 2 + dev.RETURN_LOST_PRESUMED,
      "trigger armed, two empty polls then silence: presumed after 5 silent polls")
    code, out, mine = ra(probe_armed, (empty, OSError("gone"), OSError("x"), empty) + (empty,) * 40)
    t(code is not None and "did not act" in str(code),
      "trigger armed, a short gap then answers again: not presumed (the init did not act)")
    code, out, mine = ra(probe_armed.replace("trig=armed", "trig=refused"), (OSError("gone"),))
    t(code is not None and "unconfirmed (trigger at the probe: refused)" in str(code)
      and mine.count(dev.RETURN_POLL) == dev.RETURN_WAIT / dev.RETURN_POLL_EVERY,
      "trigger `refused`, no reply: the old unconfirmed exit, naming the trigger state")
    code, out, mine = ra(probe_armed.replace("trig=armed\n", ""), (OSError("gone"),))
    t(code is not None and "trigger at the probe: not read" in str(code),
      "no trigger line (pre-init): unconfirmed, as before")
    ARMED_R = "reset=return-android requested (uptime 321s)\nwdt=/dev/watchdog0\nwarm=\nEND\n"
    code, out, mine = ra(probe_armed, (empty, ARMED_R))
    t(code is None and "reset armed: return-android requested" in out and "presumed" not in out,
      "trigger armed but the init's fallback armed: reported as before")
    for k, v in real.items():
        setattr(dev, k, v)

    # KX_CMDLINE_TOKENS + driver_async_probe=qcom_scm: exactly that value, once; the composed load passes
    # its self-check with only the kexec refusal; every other driver_async_probe form is refused before sending.
    base = "rdinit=/init clk_ignore_unused panic=10 lvr_kexec=1"
    ok_cl = base + " driver_async_probe=qcom_scm"
    t("driver_async_probe=qcom_scm" in dev.KX_CMDLINE_TOKENS and dev.kx_check_cmdline(ok_cl) == ok_cl
      and dev.kx_load_whys("a" * 64, "b" * 64, ok_cl) == {dev.KEXEC_WHY},
      "KX cmdline accepts driver_async_probe=qcom_scm; the load self-check sees only `kexec`")
    for bad in ("driver_async_probe=*", "driver_async_probe=qcom_scm,msm_dpu", "driver_async_probe=geni_spi",
                "driver_async_probe=qcom_scm driver_async_probe=qcom_scm", "driver_async_probe",
                "qcom_scm.download_mode=0"):
        n0 = len(sent)
        try:
            dev.kx_check_cmdline(base + " " + bad)
            ok = False
        except SystemExit as e:
            ok = ("not allowed or repeated" in str(e.code) or "plain tokens" in str(e.code)) and len(sent) == n0
        t(ok, f"KX cmdline refuses {bad!r}")
    return fails, total


def main():
    dev = load_dev()
    fails = 0
    for cmd in REFUSE:
        ok = dev.refusal(cmd) is not None
        fails += not ok
        print(f"{'ok  ' if ok else 'FAIL'} refuse  {cmd}")
    for cmd in ALLOW:
        why = dev.refusal(cmd)
        ok = why is None
        fails += not ok
        print(f"{'ok  ' if ok else 'FAIL'} allow   {cmd}" + ("" if ok else f"  ({why})"))
    for cmd in MODULES_OK:
        ok = dev.refusal(cmd, allow_modules=True) is None
        fails += not ok
        print(f"{'ok  ' if ok else 'FAIL'} allow+m {cmd}")
    # Stage-2: every composed device command must pass the denylist, and
    # `s2 CMD` must refuse what `sh CMD` refuses (the inner command is checked).
    composed = [c for _, c, _ in dev.s2_up_steps("stage2-x.tar.gz", 8000, "0" * 64)]
    composed += [c for _, c, _ in dev.s2_down_steps()] + [dev.s2_wrap("python3 -V")]
    for cmd in composed:
        why = dev.refusal(cmd)
        ok = why is None and "rm -rf" not in cmd and "rm -r " not in cmd
        fails += not ok
        print(f"{'ok  ' if ok else 'FAIL'} s2 step {cmd[:70]}" + ("" if ok else f"  ({why})"))
    for cmd in REFUSE:
        ok = dev.refusal(dev.s2_wrap(cmd)) is not None
        fails += not ok
        print(f"{'ok  ' if ok else 'FAIL'} refuse  s2 {cmd}")
    for cmd in ALLOW:
        if cmd in S2_REFUSE_ONLY:
            ok = dev.refusal(dev.s2_wrap(cmd)) is not None and dev.s2_unwrap(dev.s2_wrap(cmd)) == cmd
            fails += not ok
            print(f"{'ok  ' if ok else 'FAIL'} refuse  s2 {cmd[:70]} (debugfs mount in the chroot)")
            continue
        ok = dev.refusal(dev.s2_wrap(cmd)) is None and dev.s2_unwrap(dev.s2_wrap(cmd)) == cmd
        fails += not ok
        print(f"{'ok  ' if ok else 'FAIL'} allow   s2 {cmd[:70]}")
    # A tampered wrapper is not unwrapped and is judged as a whole (refused here).
    w = dev.s2_wrap("ls /sys/class/*/")
    tampered = [w.replace("/bin/sh -c", "/bin/sh -c 'echo 1 > /sys/x/*'; /bin/sh -c"),
                w + "; echo 1 > /sys/class/gpio/gpio*/value",
                w.replace("ls /sys", "ls /sys'; cp /tmp/x /dev/syncboss0; '")]
    for cmd in tampered:
        ok = dev.s2_unwrap(cmd) is None and dev.refusal(cmd) is not None
        fails += not ok
        print(f"{'ok  ' if ok else 'FAIL'} refuse  tampered s2 wrapper {cmd[-60:]!r}")
    total = (len(REFUSE) * 2 + len(ALLOW) * 2 + len(MODULES_OK) + len(composed) + len(tampered))
    f, t = transport_tests(dev)
    fails, total = fails + f, total + t
    print(f"\n{total - fails}/{total} passed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
