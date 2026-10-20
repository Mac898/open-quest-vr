# Booting the kernel and returning to Android

The Quest 2's bootloader is not unlocked. This project boots mainline Linux from a running, rooted Android
system with **quest-kexec**, a kernel module that loads a new kernel and jumps into it without a reboot. The
loader is published separately (GPL-2.0): upstream <https://github.com/YusufOzmen01/quest-kexec>, and the
fork used here <https://github.com/Mac898/quest-kexec>. How the loader works and how the headset is rooted
are covered there and are out of scope for this repository.

This page describes what you give the loader, what the kernel expects from it, what you get after the jump,
and how the kernel side returns to Android.

## What you need

1. A Quest 2 in rooted Android with adb working (`adb shell id` reports `uid=0`). The battery does not charge
   while Linux runs (see [safety.md](safety.md)).
2. The three boot inputs, built by this repository:
   - `Image`: the mainline 6.18 arm64 kernel image. It is built with text offset 0 (a normal upstream arm64
     image) and must stay small: the loader's staging window limits kernel plus BSS to about 48 MiB.
   - the device tree blob for `sm8250-oculus-hollywood`;
   - the initramfs, a gzip newc cpio of **at most 2 MiB** (see [rootfs.md](rootfs.md)).
3. A USB cable to a Linux host and the loader fork. Use the fork's capture, prepare and run steps to stage the
   three inputs and jump.

## What the kernel expects from the loader

At a high level:

- **Entry state.** The kernel is entered at EL1 (not EL2: no KVM), MMU off, on CPU 0 only, with the DTB
  address in `x0`, as in the arm64 boot protocol. Secondary CPUs come up through PSCI (`enable-method =
  "psci"`, method `smc`).
- **Warm handoff.** The firmware side (secure monitor, RPMh, the SMMU, remote processors) was not reset. The
  kernel inherits it. This is why the device tree leaves many blocks disabled, why the command line keeps
  clocks, power domains and regulators that Android left on (the `*_ignore_unused` parameters), and why the apps SMMU is handled in an "inherit" mode
  (see [design-decisions.md](design-decisions.md)).
- **Reserved memory.** The loader adds no-map reservations to the DTB for memory the secure side still owns.
  The kernel has to honour them, so they must stay in the DTB it is given.
- **Watchdog.** The SoC watchdog state at entry depends on the loader's parameters. The project kernel keeps
  the watchdog node enabled and built in, and the init takes the watchdog over as its first act. With a
  `watchdog.open_timeout` of 90 s on the command line, a kernel that never reaches init is reset by the
  watchdog about 90 s after entry, so a stalled boot returns to Android instead of hanging.
- **Log retention.** The kernel writes its console to a ramoops region in RAM. A warm reset keeps it, so
  Android can read it afterwards (see "Returning to Android").

### Command line

The kernel command line is built by `tools/dev kexec-self` (see its `--cmdline` option) for Linux-to-Linux
reloads, and by the loader for the first jump. It covers the init (`rdinit=`), the `*_ignore_unused` options,
console and log-buffer settings, `panic=10` (a warm restart together with the warm-reset driver's `on_panic`
option), the watchdog open-timeout, and `driver_async_probe=` for the two probes that talk to firmware, so a
hang there is caught by the stall detector and leaves a log. The init's own parameters (`lvr_deadman=`) are
documented in the header of `rootfs/mainline-init/init.sh`.

## After the jump

- The init (`rootfs/mainline-init/init.sh`) creates a USB gadget: **USB Ethernet (ECM), `1d6b:0105`**, fixed
  MAC addresses (host `02:51:4b:58:00:01`, device `02:51:4b:58:00:02`). The host interface name is assigned by
  your system (often `enx02514b580001`) but is stable.
- The headset is `10.42.0.2`; the host side is `10.42.0.1` (the headset runs a DHCP server with a single
  lease). Bring the link up with `nmcli device connect <iface>`, or add the address by hand
  (`sudo ip addr add 10.42.0.1/24 dev <iface>`).
- The shell is **telnet**: `telnet 10.42.0.2 23` gives a **root shell with no password and no
  authentication**. Anyone who can reach that link can run any command as root, so the interface is not meant
  to be bridged or routed to a network. See [safety.md](safety.md).
- A healthy boot logs `lvr-init:` lines: `reached`, `bound <udc>`, `ready: telnet 10.42.0.2`. The front
  LED shows the stage ([led-diag-code.md](led-diag-code.md)); if no USB link appears the init
  plays a blink code and resets the device.
- **Dead-man timer.** The init resets the headset (through the hardware watchdog) `lvr_deadman` seconds (default
  600, allowed 120-3600) after the last "heartbeat" from the host, or after boot if none ever arrives. The
  host tools (`tools/dev heartbeat`) write the target's uptime to `/tmp/lvr-heartbeat` periodically to keep the
  session alive. From a bare telnet session the device resets after the timeout unless something does the same.

## Returning to Android

Everything is RAM-only; nothing is written to the headset's storage. A return to Android is a reset, and
afterwards the headset boots Android again.

**The warm-reset driver.** `drivers/misc/quest2-warm-reset.c` (kernel patch in this repository) provides
`/proc/lvr_warm_reset`. Reading it gives `armed` (the driver verified its pre-conditions) or `refused`.
Writing `r` performs a reset that **keeps RAM contents**, in this order:

1. flush the console, disable interrupts and stop the other CPUs, so no CPU is mid-transaction on the PMIC bus;
2. arm a short watchdog bite as a backstop (2 s by default, parameter `bite_ms`);
3. set the PMIC reset type to *warm* (volatile control registers of the primary PMIC's power-on block: a
   register write sequence copied from the downstream driver, not a non-volatile or SDAM write);
4. drop `PS_HOLD`.

The same driver can handle panics (`on_panic=1`) and has a boot-stall detector
(`quest2_warm_reset.boot_stall_secs=N`, 20-75 s): if the boot has not reached init by then it prints the stall
site and warm-resets, so the log survives. After the return, read `/sys/fs/pstore/console-ramoops-0` from
Android.

**How the init uses it.** On a return request the init writes a marker line to the kernel log, syncs, and
writes `r` to the proc file. If the machine is still up 10 s later it falls back to the plain watchdog reset
(about 30 s later, a cold reset: RAM is lost).

**Why not `reboot`?** The standard path (`reboot`, then PSCI `SYSTEM_RESET2` warm) did not return on this
platform: the call never came back and the watchdog had already been stopped by the shutdown sequence, so
the device sat dark until a long power-button press. The init therefore does not ship `reboot`/`poweroff`
applets, and the return never goes through them. A plain watchdog bite always works but is a cold reset.
Long-pressing the power button also works (about 40 s to Android).

## Linux to Linux (no Android round trip)

The initramfs carries a static `kexec`. Once the first Linux boot is up you can kexec a new kernel from it
(`kexec -c --dtb ...`), keeping the loader's reservations in the DTB. That avoids a round trip through Android
for each kernel: one Android-to-Linux jump, then reload kernels from Linux (`tools/dev kexec-self`; a
self-kexec takes about 18 s). A failed kernel is reset by the watchdog if one is armed before the jump.

## Risks from the shell

`reboot`, `poweroff`, `halt` and sysrq-`b` do not return to Android on this platform (see above); the
warm-reset driver does. Reading secure or arbitrary IOMMU-mapped memory (`/dev/mem`, `phys_to_virt` on such
addresses) can reset the device. See [safety.md](safety.md).
