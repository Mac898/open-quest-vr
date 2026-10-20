#!/bin/busybox sh
# SPDX-License-Identifier: GPL-2.0-only
# Mainline init for the Meta Quest 2: a USB Ethernet (ECM) gadget + BusyBox telnet shell, with
# self-recovery so every unattended outcome ends in a hardware-watchdog reset.
#
# Derived from the initramfs init.sh of quest-kexec (https://github.com/Mac898/quest-kexec,
# a fork of https://github.com/YusufOzmen01/quest-kexec; GPL-2.0): same gadget, MACs, IPs,
# udhcpd and telnetd. Differences:
#   - no custom-OS assembly, no qkx-switch; /proc/qkx_bootdone use is guarded (absent
#     on mainline, so those calls do nothing there);
#   - UDC wait is UDC_WAIT (60) s of uptime, not 10 tries;
#   - no UDC, or a UDC the host never configures within USB_CFG_WAIT (120) s, or the
#     heartbeat dead-man (below): open qcom_wdt's /dev/watchdogN and never write it.
#     qcom_wdt has WDIOF_MAGICCLOSE and no max_hw_heartbeat_ms, so once userspace holds
#     it open the kernel stops pinging and the APSS WDT bites one timeout (30 s on this
#     DT) after the open;
#   - heartbeat dead-man: resets lvr_deadman= s (cmdline; default 600, clamped
#     to 120..3600) after the last heartbeat, i.e. after the last time the host ran
#     `tools/dev heartbeat`, which writes the target's uptime into /tmp/lvr-heartbeat.
#     Before the first heartbeat it counts from boot, so a boot nobody talks to still
#     resets itself. A live host session keeps the device up as long as it likes;
#   - kernel log follower is inline, survives /dev/kmsg EPIPE and drops duplicates;
#   - diagnostics: front-LED stages (led_stage/led_worker below), the LED and UDC lists
#     and, at the no-UDC timeout, the deferred-probe list on kmsg. BusyBox applet links
#     come from the initramfs (build.py), minus reboot/dd/... (STRIPPED);
#   - LED diagnostic code: on the no-UDC and not-configured paths a background player
#     blinks a code (device/driver flags, the deferred-probe list, uptime, USB controller
#     state) for LEDCODE_CYCLES cycles before force_reset; the reset is delayed by at most
#     LEDCODE_MAX s and never later than 60 s before the dead-man deadline. With no
#     qcom_wdt it repeats until a long press. The dead-man path is unchanged (no code,
#     immediate). The player logs one `usbdiag:` line; the dwc3 debugfs regdump (the only
#     dwc3 debugfs file read) is read at most once per boot, by the player only, with a
#     LC_RD_WAIT s limit;
#   - return-to-Android request: each dead-man pass first checks for /tmp/lvr-return
#     (created only by `tools/dev return-android`'s fixed command) and, if present, calls
#     warm_return: a LVR-WARM-MARKER line, sync, and if /proc/lvr_warm_reset reads `armed`
#     a write of `r` there (kernel: PON WARM + PS_HOLD drop, WDT bite backstop); missing or
#     anything else: straight to force_reset. If the device is still up WARM_WAIT s later,
#     the same force_reset (watchdog bite). At most one pass (5 s sleep + pass time) after
#     the request.
#
# Test mode: test_init.py runs this script as an ordinary process with every path under LVR_TEST_ROOT.
# Test mode is impossible as PID 1.
UDC_WAIT=60
USB_CFG_WAIT=120
DEADMAN_DEFAULT=600
DEADMAN_MIN=120
DEADMAN_MAX=3600
WDT_DEVICE=17c10000.watchdog
# LED code. LEDCODE_MAX >= 3 x the longest possible cycle (158.60 s, `tools/led_code.py worst`) + 5 %
# write overhead + setup (regdump read <= LC_RD_WAIT); test_init.py checks this.
LEDCODE_CYCLES=3
LEDCODE_MAX=510
LEDCODE_BRIGHT=255
LEDCODE_STUCK_GAP=20
LC_RD_WAIT=5
# Seconds the warm-reset trigger gets before the watchdog fallback.
WARM_WAIT=10

R=
LOG_OUT=/dev/kmsg
if [ "$$" != 1 ]; then
	case $LVR_TEST_ROOT in
	/?*) ;;
	*) echo "lvr-init: not PID 1 and no LVR_TEST_ROOT; refusing to run" >&2; exit 1 ;;
	esac
	real=$(cd "$LVR_TEST_ROOT" 2>/dev/null && pwd -P)
	case $real in
	''|/|//) echo "lvr-init: bad LVR_TEST_ROOT; refusing to run" >&2; exit 1 ;;
	esac
	if [ ! -f "$LVR_TEST_ROOT/LVR_TEST_ROOT" ]; then
		echo "lvr-init: LVR_TEST_ROOT has no marker file; refusing to run" >&2
		exit 1
	fi
	R=$LVR_TEST_ROOT
	LOG_OUT=$R/console.log
fi

export PATH=$R/bin:$R/sbin
export HOME=/root
export TERM=vt100
if [ -z "$R" ]; then
	# Applet links are in the initramfs (build.py); no `busybox --install` (it would
	# bring back the stripped applets).
	mount -t proc proc /proc
	mount -t sysfs sysfs /sys
	mount -t devtmpfs devtmpfs /dev
	mkdir -p /dev/pts /config /tmp
	mount -t devpts devpts /dev/pts
	mount -t configfs configfs /config
	mount -t tmpfs tmpfs /tmp
fi
exec >"$LOG_OUT" 2>&1

log() { echo "lvr-init: [${UP:-?}s] $*"; }
# Downstream-kernel hook only. Never present on mainline; guarded so it is a no-op.
bootdone() { [ -w "$R/proc/qkx_bootdone" ] && echo "$1" > "$R/proc/qkx_bootdone"; }

# (2>/dev/null goes before the input redirect: a failing "< file" reports its error before any later
# redirect applies, which would put a line on /dev/kmsg every pass.)
# Whole seconds of uptime in UP (UP_OK=1). If /proc/uptime can't be read, UP is huge and UP_OK empty,
# so every deadline counts as expired (fail towards the reset).
uptime_s() {
	UP=
	UP_OK=
	read -r UP _ 2>/dev/null < "$R/proc/uptime"
	UP=${UP%%.*}
	case $UP in
	''|*[!0-9]*) UP=999999 ;;
	*) UP_OK=1 ;;
	esac
}

# NUM = $1 as a decimal integer if it is 1..6 digits once leading zeros are dropped
# (shell arithmetic would read 0900 as bad octal); otherwise NUM is empty, return 1.
to_num() {
	NUM=$1
	case $NUM in
	''|*[!0-9]*) NUM=; return 1 ;;
	esac
	while :; do
		case $NUM in
		0?*) NUM=${NUM#0} ;;
		*) break ;;
		esac
	done
	[ ${#NUM} -le 6 ] || { NUM=; return 1; }
}

hold_forever() { while :; do sleep 3600; done; }

# Print the device node of the qcom_wdt instance at $WDT_DEVICE, if registered.
find_wdt() {
	for w in "$R"/sys/class/watchdog/watchdog*; do
		[ -e "$w/device" ] || continue
		case $(readlink -f "$w/device") in
		*/"$WDT_DEVICE") ;;
		*) continue ;;
		esac
		d=$R/dev/${w##*/}
		if [ -c "$d" ] || { [ -n "$R" ] && [ -f "$d" ]; }; then
			echo "$d"
			return 0
		fi
	done
	return 1
}

# First act: take over the watchdog. The loader may hand over a running APSS WDT (watchdog_recovery=1)
# that the kernel pets only until watchdog.open_timeout= expires, so a stall before this line ends in a
# bite. A magic close ('V', then close) stops it; every reset path below then works as usual. Harmless if
# the WDT was not running. If the write fails the close leaves it running unpetted: a bite ~30 s later.
uptime_s
if wdt_dev=$(find_wdt); then
	if printf V 2>/dev/null > "$wdt_dev"; then
		log "watchdog $wdt_dev taken over and stopped (magic close)"
	else
		log "watchdog $wdt_dev magic close FAILED: expect a bite ~30 s after uptime ${UP}s"
	fi
else
	log "no qcom_wdt device for $WDT_DEVICE at init start; nothing to take over"
fi

# Force a reset through the hardware watchdog. Does not return. The caller's shell
# keeps the device open on fd 3 for good; nothing ever writes to it. Only one caller
# arms it (mkdir is the lock); later callers just hold.
force_reset() {
	uptime_s
	if ! mkdir "$R/tmp/lvr-reset" 2>/dev/null; then
		log "reset requested ($1) but already armed: $(cat "$R/tmp/lvr-reset/reason" 2>/dev/null)"
		hold_forever
	fi
	echo "$1 (uptime ${UP}s)" > "$R/tmp/lvr-reset/reason"
	# LED: red fast blink. Only a /tmp write here; led_worker applies it within ~1 s.
	echo reset 2>/dev/null > "$R/tmp/lvr-reset/led"
	log "RESET: $1"
	bootdone r
	dev=$(find_wdt)
	if [ -z "$dev" ]; then
		echo stuck 2>/dev/null > "$R/tmp/lvr-reset/led"
		log "RESET FAILED: no qcom_wdt device for $WDT_DEVICE; cannot self-reset; needs a long power-button press"
		# Nothing left to delay: show the LED code (path 4) until the long press,
		# alternating with the slow red blink. Waits for an earlier player to stop.
		lc_old=
		read -r lc_old 2>/dev/null < "$R/tmp/lvr-ledcode.pid"
		echo starting 2>/dev/null > "$R/tmp/lvr-ledcode.pid"
		ledcode_player 4 0 "$lc_old" &
		echo $! 2>/dev/null > "$R/tmp/lvr-ledcode.pid"
		hold_forever
	fi
	if command exec 3>"$dev"; then
		echo "$dev" > "$R/tmp/lvr-reset/device"
		log "watchdog $dev held open and never written: expect the bite ~30 s after uptime ${UP}s (cold); with quest2_warm_reset.on_panic=1 the bark ~29 s panics instead and panic=10 ends warm ~41 s after (<= ~61 s)"
		hold_forever
	fi
	# Open failed although qcom_wdt is registered (something else holds it).
	# Fallback: crash; with panic=0 the kernel stops and stops pinging (bite);
	# with panic=N it restarts N s later, warm if quest2_warm_reset.on_panic=1.
	log "watchdog $dev open failed; fallback: sysrq crash (panic=0: then bite; panic=N: restart N s later, warm with quest2_warm_reset.on_panic=1)"
	echo c > "$R/proc/sysrq-trigger"
	hold_forever
}

# Return to Android. Does not return; only the dead-man loop calls it, for /tmp/lvr-return. If another
# reset path is already armed: force_reset (it logs and holds). The kernel trigger /proc/lvr_warm_reset
# (quest2-warm-reset driver) reads `armed` or `refused`; missing, unreadable or not `armed`: log why and
# force_reset at once. Armed: one LVR-WARM-MARKER <boot_id> <uptime> line to /dev/kmsg (so it lands in
# console-ramoops), then a background child syncs and writes `r` to the trigger: PON warm reset + PS_HOLD
# drop, with the driver's short APSS WDT bite as backstop. No reboot(2) and no device_shutdown, so the
# fan keeps its duty until the reset. The child keeps a hung sync from holding this shell; if the write
# fails or returns, the device is still up WARM_WAIT s later and force_reset runs (once the kernel has
# stopped the other CPUs only the driver's backstop is left). /tmp/lvr-warm (reason and uptime) is for
# tools/dev's poll and never read here.
warm_return() {
	uptime_s
	if [ -e "$R/tmp/lvr-reset" ]; then
		log "warm reset skipped: another reset path is already armed"
		force_reset "$1"
	fi
	wr=
	read -r wr 2>/dev/null < "$R/proc/lvr_warm_reset"
	if [ "$wr" != armed ]; then
		if [ -e "$R/proc/lvr_warm_reset" ]; then
			log "warm reset: /proc/lvr_warm_reset says '$wr' (not armed); watchdog reset instead"
		else
			log "warm reset: no /proc/lvr_warm_reset (kernel without quest2-warm-reset); watchdog reset instead"
		fi
		force_reset "$1"
	fi
	bid=
	read -r bid 2>/dev/null < "$R/proc/sys/kernel/random/boot_id"
	upf=
	read -r upf _ 2>/dev/null < "$R/proc/uptime"
	echo "$1 (uptime ${UP}s)" 2>/dev/null > "$R/tmp/lvr-warm"
	led_stage reset
	log "LVR-WARM-MARKER ${bid:-no-boot-id} ${upf:-no-uptime}"
	log "warm reset: sync, then r to /proc/lvr_warm_reset (PON WARM + PS_HOLD); watchdog fallback in ${WARM_WAIT}s"
	(sync; printf r > "$R/proc/lvr_warm_reset") < /dev/null &
	# Wait WARM_WAIT s of uptime in 1 s steps: a single `sleep 10` returns early when the
	# child exits (SIGCHLD interrupts BusyBox's in-shell sleep). At most 2 x WARM_WAIT
	# steps, so a stuck uptime cannot stretch it; an unreadable one ends it at once.
	w0=$UP
	n=0
	while [ "$n" -lt $((2 * WARM_WAIT)) ]; do
		sleep 1
		n=$((n + 1))
		uptime_s
		[ "$UP" -ge $((w0 + WARM_WAIT)) ] && break
	done
	log "warm reset did not happen within ${WARM_WAIT}s; falling back to the watchdog"
	force_reset "$1"
}

# --- Front status LED ------------------------------------------------------
# Shows state to someone watching the headset when there is no USB link:
#   red solid         init running, waiting for a UDC
#   green solid       gadget bound to a UDC, waiting for the host to configure it
#   green heartbeat   host configured the gadget: telnet ready
#   red fast blink    a reset path is armed (100/100 ms); the watchdog bites ~30 s later
#   red slow blink    reset wanted but no qcom_wdt (1000/1000 ms): needs a long press
#   blue heartbeat    kernel default trigger (DT), never touched here: kernel alive
# Callers only write the wanted stage to /tmp (led_stage, force_reset); led_worker, a separate process,
# applies it, so a failing or stuck LED write can never delay init or a reset path. Once a reset is
# armed, /tmp/lvr-reset/led wins over /tmp/lvr-led. Writes are best-effort and only touch brightness,
# trigger, delay_on, delay_off and multi_intensity of /sys/class/leds/<name> for LEDs with a red or
# green colour field (e.g. red:status) or, if there are none, of the first multicolour LED. Flash LEDs
# are skipped. The worker re-scans every second, so a late-probing LED still gets the current stage.
led_stage() { echo "$1" 2>/dev/null > "$R/tmp/lvr-led"; }

led_w() { echo "$2" 2>/dev/null > "$1" || LED_ERR=1; }

# LED_MAX = max_brightness of LED dir $1 (255 if unreadable).
led_max() {
	m=
	read -r m _ 2>/dev/null < "$1/max_brightness"
	if to_num "$m" && [ "$NUM" -gt 0 ]; then LED_MAX=$NUM; else LED_MAX=255; fi
}

# led_mode DIR off|solid|heartbeat|blink [ON_MS OFF_MS]
led_mode() {
	led_max "$1"
	case $2 in
	off) led_w "$1/trigger" none; led_w "$1/brightness" 0 ;;
	solid) led_w "$1/trigger" none; led_w "$1/brightness" "$LED_MAX" ;;
	heartbeat) led_w "$1/brightness" "$LED_MAX"; led_w "$1/trigger" heartbeat ;;
	blink)
		led_w "$1/brightness" "$LED_MAX"
		led_w "$1/trigger" timer
		led_w "$1/delay_on" "$3"
		led_w "$1/delay_off" "$4"
		;;
	esac
}

# Sets LED_NAMES (every LED, for the log), LED_RED/LED_GREEN (single-colour LED dirs) and
# LED_MC/LED_MC_IDX (multicolour LED dir and its multi_index; only if no single ones).
# LED_BLUE (single blue LED) is used only by the LED code player, never by the stages.
led_scan() {
	LED_NAMES=
	LED_RED=
	LED_GREEN=
	LED_BLUE=
	LED_MC=
	LED_MC_IDX=
	for d in "$R"/sys/class/leds/*; do
		[ -e "$d" ] || continue
		n=${d##*/}
		LED_NAMES="$LED_NAMES $n"
		[ -e "$d/flash_brightness" ] && continue
		if [ -e "$d/multi_index" ]; then
			idx=
			read -r idx 2>/dev/null < "$d/multi_index"
			case " $idx " in
			*" red "*|*" green "*)
				if [ -z "$LED_MC" ]; then LED_MC=$d; LED_MC_IDX=$idx; fi ;;
			esac
			continue
		fi
		case :$n: in
		*:red:*) [ -n "$LED_RED" ] || LED_RED=$d ;;
		*:green:*) [ -n "$LED_GREEN" ] || LED_GREEN=$d ;;
		*:blue:*) [ -n "$LED_BLUE" ] || LED_BLUE=$d ;;
		esac
	done
	if [ -n "$LED_RED$LED_GREEN" ]; then LED_MC=; LED_MC_IDX=; fi
}

led_apply() {
	case $1 in
	red) col=red; mode=solid ;;
	green) col=green; mode=solid ;;
	green-hb) col=green; mode=heartbeat ;;
	reset) col=red; mode="blink 100 100" ;;
	stuck) col=red; mode="blink 1000 1000" ;;
	*) log "led: unknown stage '$1'"; return ;;
	esac
	LED_ERR=
	did=
	if [ -n "$LED_MC" ]; then
		led_max "$LED_MC"
		v=
		for c in $LED_MC_IDX; do
			if [ "$c" = "$col" ]; then v="$v $LED_MAX"; else v="$v 0"; fi
		done
		led_w "$LED_MC/multi_intensity" "${v# }"
		led_mode "$LED_MC" $mode
		did="${LED_MC##*/}=$col ${mode%% *}"
	else
		on=$LED_RED
		off=$LED_GREEN
		if [ "$col" = green ]; then on=$LED_GREEN; off=$LED_RED; fi
		if [ -n "$off" ]; then led_mode "$off" off; did="${off##*/}=off"; fi
		if [ -n "$on" ]; then led_mode "$on" $mode; did="$did ${on##*/}=${mode%% *}"; fi
	fi
	log "led: stage $1: ${did:-no status LED}${LED_ERR:+ (some LED writes failed)}"
}

led_worker() {
	trap '' HUP INT TERM
	names=-
	applied=
	paused=
	while :; do
		uptime_s
		# Pause while the LED code player runs: its PID is in /tmp/lvr-ledcode.pid
		# and it is alive. Afterwards re-apply the current stage to every LED.
		lcp=
		read -r lcp 2>/dev/null < "$R/tmp/lvr-ledcode.pid"
		case $lcp in
		[1-9]*)
			if kill -0 "$lcp" 2>/dev/null; then
				[ -n "$paused" ] || log "led: paused for the LED code (pid $lcp)"
				paused=1
				applied=
				sleep 1
				continue
			fi
			;;
		esac
		[ -z "$paused" ] || log "led: LED code ended, stages resumed"
		paused=
		st=
		read -r st 2>/dev/null < "$R/tmp/lvr-reset/led"
		[ -n "$st" ] || read -r st 2>/dev/null < "$R/tmp/lvr-led"
		led_scan
		if [ "$LED_NAMES" != "$names" ]; then
			names=$LED_NAMES
			log "leds:${LED_NAMES:- none} (red=${LED_RED##*/} green=${LED_GREEN##*/} multicolor=${LED_MC##*/})"
			applied=
		fi
		if [ -n "$st" ] && [ "$st" != "$applied" ]; then
			led_apply "$st"
			applied=$st
		fi
		sleep 1
	done
}

# UDCS = names in /sys/class/udc, space-separated (empty if none).
udc_list() {
	UDCS=
	for c in "$R"/sys/class/udc/*; do
		[ -e "$c" ] && UDCS="$UDCS ${c##*/}"
	done
	UDCS=${UDCS# }
}

# At the no-UDC / not-configured timeout: which devices still wait for a supplier.
# debugfs is mounted read-only, only here (nobody can reach the shell anyway). Runs in
# the LED code player (a background process), so it can never delay the reset. Only
# devices_deferred is read; never pinctrl/ or gpio (they touch hardware state).
diag_deferred() {
	f=$R/sys/kernel/debug/devices_deferred
	if [ -z "$R" ] && [ ! -e "$f" ]; then
		mount -t debugfs -o ro debugfs /sys/kernel/debug 2>/dev/null ||
			log "deferred: debugfs mount failed"
	fi
	if [ ! -r "$f" ]; then
		log "deferred: $f not readable"
		return
	fi
	n=0
	while IFS= read -r line; do
		n=$((n + 1))
		[ "$n" -le 40 ] && log "deferred: $line"
	done < "$f"
	if [ "$n" -eq 0 ]; then
		log "deferred: none"
	elif [ "$n" -gt 40 ]; then
		log "deferred: ($n in total, first 40 shown)"
	fi
}

# --- LED diagnostic code ----------------------------------------------------
# Reference encoder/decoder: tools/led_code.py; test_init.py
# runs the block between the BEGIN/END markers on fixture sysfs trees and compares the
# result with tools/led_code.py. The block only reads sysfs/debugfs/configfs and sets
# LC_* variables; it needs R and log, nothing else.
# BEGIN ledcode
LC_TAB=$(printf '\t')
LC_GADGET=$R/config/usb_gadget/qkx
# Flags 1-11, in order (flag 12 is the UDC).
LC_FLAG_DEVS="80860000.memory 18200000.rsc 18200000.rsc:clock-controller
18200000.rsc:regulators-0 18200000.rsc:bcm-voter 100000.clock-controller
16e0000.interconnect 88e3000.phy a6f8800.usb a600000.usb 88e8000.phy"
# Index table: index, device name, DT path prefix.
LC_TABLE="01 80860000.memory /reserved-memory/memory@80860000
02 18200000.rsc /soc@0/rsc@18200000
03 18200000.rsc:clock-controller /soc@0/rsc@18200000/clock-controller
04 18200000.rsc:regulators-0 /soc@0/rsc@18200000/regulators-0
05 18200000.rsc:regulators-1 /soc@0/rsc@18200000/regulators-1
06 18200000.rsc:bcm-voter /soc@0/rsc@18200000/bcm-voter
07 100000.clock-controller /soc@0/clock-controller@100000
08 1500000.interconnect /soc@0/interconnect@1500000
09 1620000.interconnect /soc@0/interconnect@1620000
10 163d000.interconnect /soc@0/interconnect@163d000
11 16e0000.interconnect /soc@0/interconnect@16e0000
12 90c0000.interconnect /soc@0/interconnect@90c0000
13 9100000.interconnect /soc@0/interconnect@9100000
14 88e3000.phy /soc@0/phy@88e3000
15 a6f8800.usb /soc@0/usb@a6f8800
16 a600000.usb /soc@0/usb@a6f8800/usb@a600000
17 88e8000.phy /soc@0/phy@88e8000
18 fan /fan
19 f100000.pinctrl /soc@0/pinctrl@f100000
20 b220000.interrupt-controller /soc@0/interrupt-controller@b220000
21 c440000.spmi /soc@0/spmi@c440000
22 c440000.spmi:pmic@4:gpio@c000 /soc@0/spmi@c440000/pmic@4/gpio@c000
23 c440000.spmi:pmic@5:pwm /soc@0/spmi@c440000/pmic@5/pwm
24 c263000.thermal-sensor /soc@0/thermal-sensor@c263000
25 c265000.thermal-sensor /soc@0/thermal-sensor@c265000
26 17c10000.watchdog /soc@0/watchdog@17c10000
27 9ba40000.ramoops /reserved-memory/ramoops@9ba40000
28 psci /psci"

# lc_lookup name|path KEY: LC_IDX = table index of the device name (exact) or of the
# longest DT path prefix (KEY equal to it or below it); 99 if none.
lc_lookup() {
	LC_IDX=99
	lc_best=0
	lc_mode=$1
	lc_key=$2
	set -- $LC_TABLE
	while [ $# -ge 3 ]; do
		if [ "$lc_mode" = name ]; then
			if [ "$lc_key" = "$2" ]; then LC_IDX=$1; return; fi
		else
			case $lc_key in
			"$3"|"$3"/*) if [ ${#3} -gt $lc_best ]; then lc_best=${#3}; LC_IDX=$1; fi ;;
			esac
		fi
		shift 3
	done
}

# lc_reason TEXT (after the TAB): LC_C reason category, LC_SS supplier index (00 none).
# The kernel stores "<driver or bus>: <message>" (dd.c:235), so drop a first "word: ".
lc_reason() {
	lc_t=$1
	case $lc_t in
	*": "*)
		case ${lc_t%%": "*} in
		*" "*) ;;
		*) lc_t=${lc_t#*": "} ;;
		esac
		;;
	esac
	LC_SS=00
	case $lc_t in
	'') LC_C=0 ;;
	"wait for supplier "*) LC_C=1; lc_lookup path "${lc_t#wait for supplier }"; LC_SS=$LC_IDX ;;
	"supplier "*" not ready")
		LC_C=2
		lc_t=${lc_t#supplier }
		lc_lookup name "${lc_t% not ready}"
		LC_SS=$LC_IDX
		;;
	*regulator*|*supply*) LC_C=3 ;;
	*clk*|*clock*) LC_C=4 ;;
	*phy*|*PHY*) LC_C=5 ;;
	*interconnect*|*icc*|*path*|*bandwidth*) LC_C=6 ;;
	*domain*) LC_C=7 ;;
	*"Command DB"*|*cmd*|*rpmh*) LC_C=8 ;;
	*) LC_C=9 ;;
	esac
}

# F9 sources (USB controller state). LC_RD is the regdump text to
# parse: the copy usb_diag made (outside this block), or empty. Never any other dwc3
# debugfs file (some write debug-mux registers on read).
LC_IRQF=$R/proc/interrupts
LC_UDCD=$R/sys/class/udc/a600000.usb
LC_RD=

# lc_hex VALUE: LC_H = VALUE if it is 0x and 1..8 hex digits, else empty.
lc_hex() {
	LC_H=
	case $1 in
	0x*)
		lc_h=${1#0x}
		case $lc_h in ''|*[!0-9a-fA-F]*) return ;; esac
		[ ${#lc_h} -le 8 ] && LC_H=$1
		;;
	esac
}

# lc_word STRING: LC_W = one log token: na if empty, not_attached, the string if it is
# only [A-Za-z-], otherwise other.
lc_word() {
	case $1 in
	'') LC_W=na ;;
	"not attached") LC_W=not_attached ;;
	*[!A-Za-z-]*) LC_W=other ;;
	*) LC_W=$1 ;;
	esac
}

# lc_usb: LC_USB = F9 (3 digits), LC_USBLINE = the usbdiag summary
# (== tools/led_code.py usb_fields()). Read-only: /proc/interrupts, the UDC's state and
# current_speed, and the regdump copy in LC_RD.
lc_usb() {
	# IRQ count: first /proc/interrupts line whose last token is "dwc3": sum of the numeric
	# tokens after the IRQ label (the per-CPU counts), each and the sum capped at 99999999.
	LC_IRQ=
	if [ -r "$LC_IRQF" ]; then
		while IFS= read -r lc_l || [ -n "$lc_l" ]; do
			set -f
			set -- $lc_l
			set +f
			[ $# -ge 2 ] || continue
			shift
			lc_s=0
			lc_num=1
			lc_z=
			for lc_x in "$@"; do
				lc_z=$lc_x
				[ -n "$lc_num" ] || continue
				case $lc_x in
				*[!0-9]*) lc_num= ;;
				*)
					while :; do case $lc_x in 0?*) lc_x=${lc_x#0} ;; *) break ;; esac; done
					[ ${#lc_x} -gt 8 ] && lc_x=99999999
					lc_s=$((lc_s + lc_x))
					[ $lc_s -gt 99999999 ] && lc_s=99999999
					;;
				esac
			done
			if [ "$lc_z" = dwc3 ]; then LC_IRQ=$lc_s; break; fi
		done < "$LC_IRQF"
	fi
	if [ -z "$LC_IRQ" ]; then lc_a=9
	elif [ "$LC_IRQ" -eq 0 ]; then lc_a=0
	elif [ "$LC_IRQ" -lt 10 ]; then lc_a=1
	elif [ "$LC_IRQ" -lt 1000 ]; then lc_a=2
	else lc_a=3
	fi
	# UDC state and speed.
	lc_st=
	read -r lc_st 2>/dev/null < "$LC_UDCD/state"
	case $lc_st in
	"not attached"|attached|powered) lc_b=0 ;;
	default) lc_b=1 ;;
	addressed) lc_b=2 ;;
	configured) lc_b=3 ;;
	suspended) lc_b=4 ;;
	*) lc_b=9 ;;
	esac
	lc_word "$lc_st"
	LC_USBLINE="irq=${LC_IRQ:-na} state=$LC_W"
	lc_st=
	read -r lc_st 2>/dev/null < "$LC_UDCD/current_speed"
	lc_word "$lc_st"
	LC_USBLINE="$LC_USBLINE speed=$LC_W"
	# Regdump lines "NAME = 0x%08x" (debugfs_print_regs32); the first valid value per name counts.
	lc_r1= lc_r2= lc_r3= lc_r4= lc_r5= lc_r6= lc_r7= lc_r8= lc_r9= lc_r10=
	if [ -n "$LC_RD" ] && [ -r "$LC_RD" ]; then
		while IFS= read -r lc_l || [ -n "$lc_l" ]; do
			case $lc_l in *" = "*) ;; *) continue ;; esac
			lc_hex "${lc_l#* = }"
			[ -n "$LC_H" ] || continue
			case ${lc_l%% = *} in
			GSTS) lc_r1=${lc_r1:-$LC_H} ;;
			GBUSERRADDR0) lc_r2=${lc_r2:-$LC_H} ;;
			GBUSERRADDR1) lc_r3=${lc_r3:-$LC_H} ;;
			"GEVNTADRLO(0)") lc_r4=${lc_r4:-$LC_H} ;;
			"GEVNTADRHI(0)") lc_r5=${lc_r5:-$LC_H} ;;
			"GEVNTSIZ(0)") lc_r6=${lc_r6:-$LC_H} ;;
			"GEVNTCOUNT(0)") lc_r7=${lc_r7:-$LC_H} ;;
			DCTL) lc_r8=${lc_r8:-$LC_H} ;;
			DEVTEN) lc_r9=${lc_r9:-$LC_H} ;;
			DSTS) lc_r10=${lc_r10:-$LC_H} ;;
			esac
		done < "$LC_RD"
	fi
	# F9.3: GEVNTCOUNT(0) & 0xfffc != 0 -> +1; GSTS bit 4 (BUS_ERR_ADDR_VLD) -> +2. The
	# values are checked hex (lc_hex), so the arithmetic only ever sees 0x[0-9a-f]{1,8}.
	if [ -n "$lc_r1" ] && [ -n "$lc_r7" ]; then
		lc_c=$(( ($lc_r7 & 0xfffc) != 0 ))
		lc_c=$((lc_c + 2 * (($lc_r1 >> 4) & 1)))
	else
		lc_c=9
	fi
	LC_USB=$lc_a$lc_b$lc_c
	LC_USBLINE="$LC_USBLINE GSTS=${lc_r1:-na} BUSERR=${lc_r3:-na}:${lc_r2:-na} EVADR=${lc_r5:-na}:${lc_r4:-na}"
	LC_USBLINE="$LC_USBLINE EVSIZ=${lc_r6:-na} EVCNT=${lc_r7:-na} DCTL=${lc_r8:-na} DEVTEN=${lc_r9:-na}"
	LC_USBLINE="$LC_USBLINE DSTS=${lc_r10:-na} f9=$LC_USB"
}

# lc_collect PATH UPTIME: the fields. Read-only: platform device and driver links, the
# gadget's UDC attribute, /sys/class/udc and debugfs devices_deferred (mounted read-only
# by diag_deferred), plus lc_usb's sources. LC_PATH LC_FLAGS LC_NDEF LC_ENT
# ("DD.C.SS ..."), LC_UP, LC_DRV, LC_USB, LC_USBLINE.
lc_collect() {
	LC_PATH=$1
	LC_UP=$2
	case $LC_UP in ''|*[!0-9]*|???????*) LC_UP=999 ;; esac
	while :; do case $LC_UP in 0?*) LC_UP=${LC_UP#0} ;; *) break ;; esac; done
	[ "$LC_UP" -gt 999 ] && LC_UP=999
	lc_p=$R/sys/bus/platform/devices
	LC_FLAGS=
	LC_DRV=
	for lc_n in $LC_FLAG_DEVS; do
		if [ -e "$lc_p/$lc_n/driver" ]; then
			LC_FLAGS=${LC_FLAGS}G
			lc_d=$(readlink "$lc_p/$lc_n/driver" 2>/dev/null)
			lc_d=${lc_d##*/}
			LC_DRV="$LC_DRV $lc_n=${lc_d:-?}"
		elif [ -e "$lc_p/$lc_n" ]; then
			LC_FLAGS=${LC_FLAGS}B
			LC_DRV="$LC_DRV $lc_n=-"
		else
			LC_FLAGS=${LC_FLAGS}R
		fi
	done
	lc_u=
	read -r lc_u 2>/dev/null < "$LC_GADGET/UDC"
	if [ -n "$lc_u" ]; then
		LC_FLAGS=${LC_FLAGS}G
	else
		lc_f=R
		for lc_d in "$R"/sys/class/udc/*; do
			[ -e "$lc_d" ] && lc_f=B && break
		done
		LC_FLAGS=$LC_FLAGS$lc_f
	fi
	lc_f=$R/sys/kernel/debug/devices_deferred
	LC_NDEF=99
	LC_ENT=
	if [ -r "$lc_f" ]; then
		lc_n=0
		while IFS= read -r lc_l || [ -n "$lc_l" ]; do
			lc_n=$((lc_n + 1))
			[ $lc_n -le 3 ] || continue
			case $lc_l in
			*"$LC_TAB"*) lc_r=${lc_l#*"$LC_TAB"} ;;
			*) lc_r= ;;
			esac
			lc_lookup name "${lc_l%%"$LC_TAB"*}"
			lc_dd=$LC_IDX
			lc_reason "$lc_r"
			LC_ENT="$LC_ENT $lc_dd.$LC_C.$LC_SS"
		done < "$lc_f"
		[ $lc_n -gt 98 ] && lc_n=98
		LC_NDEF=$lc_n
	fi
	LC_ENT=${LC_ENT# }
	lc_usb
}

lc_sym() { LC_TL="$LC_TL $1:$2:$3"; LC_MS=$((LC_MS + $2 + $3)); }

# lc_digits COLOUR VALUE WIDTH: VALUE as WIDTH decimal digits (zero-padded, last WIDTH
# kept); adds each digit to LC_SUM.
lc_digits() {
	lc_v=$2
	while [ ${#lc_v} -lt "$3" ]; do lc_v=0$lc_v; done
	while [ ${#lc_v} -gt "$3" ]; do lc_v=${lc_v#?}; done
	while [ -n "$lc_v" ]; do
		lc_x=${lc_v%"${lc_v#?}"}
		lc_v=${lc_v#?}
		LC_SUM=$((LC_SUM + lc_x))
		if [ "$lc_x" = 0 ]; then
			lc_sym "$1" 1000 250
		else
			lc_k=0
			while [ $lc_k -lt "$lc_x" ]; do lc_sym "$1" 250 250; lc_k=$((lc_k + 1)); done
		fi
		lc_sym dark 0 750
	done
}

# lc_encode: LC_TL = one cycle as "colour:on_ms:off_ms ..." (== tools/led_code.py
# timeline_str(encode(...))), LC_MS its length, LC_CS the checksum, LC_LINE the log line.
lc_encode() {
	LC_TL=
	LC_MS=0
	LC_SUM=0
	lc_sym white 2000 1500
	lc_digits red "$LC_PATH" 1
	lc_sym white 250 1000
	lc_v=$LC_FLAGS
	while [ -n "$lc_v" ]; do
		case $lc_v in
		G*) lc_sym green 400 400; LC_SUM=$((LC_SUM + 1)) ;;
		B*) lc_sym blue 400 400; LC_SUM=$((LC_SUM + 2)) ;;
		*) lc_sym red 400 400 ;;
		esac
		lc_v=${lc_v#?}
	done
	lc_sym white 250 1000
	lc_digits blue "$LC_NDEF" 2
	lc_sym white 250 1000
	lc_d=
	lc_i=0
	for lc_e in $LC_ENT 00.0.00 00.0.00 00.0.00; do
		[ $lc_i -lt 3 ] || break
		lc_i=$((lc_i + 1))
		lc_ee=${lc_e#*.}
		lc_digits green "${lc_e%%.*}" 2
		lc_digits red "${lc_ee%%.*}" 1
		lc_digits blue "${lc_ee#*.}" 2
		lc_sym white 250 1000
		lc_d="$lc_d,$lc_e"
	done
	lc_digits green "$LC_UP" 3
	lc_sym white 250 1000
	lc_digits blue "$LC_USB" 3
	lc_sym white 250 1000
	LC_CS=$((LC_SUM % 10))
	lc_digits red "$LC_CS" 1
	lc_sym dark 0 3000
	LC_TL=${LC_TL# }
	LC_LINE="ledcode v2 path=$LC_PATH flags=$LC_FLAGS ndef=$LC_NDEF d=${lc_d#,} up=$LC_UP usb=$LC_USB sum=$LC_CS"
}
# END ledcode

# --- LED code player ------------------------------------------------------------------
# Runs as its own background process (like led_worker), so a slow or stuck LED write can
# never delay a reset: the caller (ledcode_then_reset) waits for it with a hard limit.
# Writes only brightness, trigger and multi_intensity of the LEDs led_scan picks (single
# red/green/blue, or one multicolour LED), best-effort. led_worker pauses while this
# process (PID in /tmp/lvr-ledcode.pid) is alive and re-applies the stage afterwards.
lc_w() { if echo "$2" 2>/dev/null > "$1"; then LC_OK=1; else LED_ERR=1; fi; }

# lc_sleep MS (test mode may divide by LVR_TEST_LEDCODE_DIV; the shell's sleep is a
# builtin and takes fractions).
lc_sleep() {
	lc_us=$(($1 * 1000 / LC_DIV))
	lc_fr=$((lc_us % 1000000 + 1000000))
	sleep "$((lc_us / 1000000)).${lc_fr#1}"
}

lc_br() { led_max "$1"; LC_BR=$LEDCODE_BRIGHT; [ "$LED_MAX" -lt "$LC_BR" ] && LC_BR=$LED_MAX; }

# usb_diag (player only, after diag_deferred mounted debugfs read-only): the one read of the dwc3 debugfs
# regdump per boot, never any other dwc3 debugfs file. The read takes a runtime-PM reference in the
# kernel, so it runs in a background cat that copies the file to /tmp/lvr-regdump/copy; this process waits
# at most LC_RD_WAIT s and otherwise abandons it. The first caller reads (mkdir lock); a later player
# (path 4) reuses the result. UD_STATE: ok | missing | timeout | error (| busy: another player is
# still reading). LC_RD: the copy to parse if ok, else empty.
usb_diag() {
	ud_d=$R/tmp/lvr-regdump
	ud_src=$R/sys/kernel/debug/usb/a600000.usb/regdump
	if mkdir "$ud_d" 2>/dev/null; then
		if [ ! -e "$ud_src" ]; then
			UD_STATE=missing
		else
			(cat "$ud_src" 2>/dev/null > "$ud_d/copy"; echo $? 2>/dev/null > "$ud_d/rc") &
			ud_pid=$!
			ud_i=0
			while [ ! -s "$ud_d/rc" ] && [ $ud_i -lt $((LC_RD_WAIT * 10)) ]; do
				lc_sleep 100
				ud_i=$((ud_i + 1))
			done
			ud_rc=
			read -r ud_rc 2>/dev/null < "$ud_d/rc"
			case $ud_rc in
			0) UD_STATE=ok ;;
			'') UD_STATE=timeout; kill -9 "$ud_pid" 2>/dev/null ;;
			*) UD_STATE=error ;;
			esac
		fi
		echo "$UD_STATE" 2>/dev/null > "$ud_d/state"
	else
		ud_i=0
		while [ ! -s "$ud_d/state" ] && [ $ud_i -lt $((LC_RD_WAIT * 10 + 10)) ]; do
			lc_sleep 100
			ud_i=$((ud_i + 1))
		done
		UD_STATE=
		read -r UD_STATE 2>/dev/null < "$ud_d/state"
		UD_STATE=${UD_STATE:-busy}
	fi
	LC_RD=
	[ "$UD_STATE" = ok ] && LC_RD=$ud_d/copy
}

# Returns 1 if there is no usable LED, 2 if every setup write failed.
lc_led_setup() {
	led_scan
	LC_SR=
	LC_SG=
	LC_SB=
	LC_OK=
	LED_ERR=
	if [ -n "$LED_MC" ]; then
		lc_br "$LED_MC"
		LC_BR_MC=$LC_BR
		lc_w "$LED_MC/trigger" none
		lc_w "$LED_MC/brightness" 0
	elif [ -n "$LED_RED$LED_GREEN$LED_BLUE" ]; then
		if [ -z "$LC_BLUE_TRIG" ] && [ -n "$LED_BLUE" ]; then
			lc_l=
			read -r lc_l 2>/dev/null < "$LED_BLUE/trigger"
			case $lc_l in
			*"["*"]"*) lc_l=${lc_l#*\[}; LC_BLUE_TRIG=${lc_l%%\]*} ;;
			esac
		fi
		for lc_d in $LED_RED $LED_GREEN $LED_BLUE; do
			lc_w "$lc_d/trigger" none
			lc_w "$lc_d/brightness" 0
		done
		[ -n "$LED_RED" ] && { lc_br "$LED_RED"; LC_BR_R=$LC_BR; }
		[ -n "$LED_GREEN" ] && { lc_br "$LED_GREEN"; LC_BR_G=$LC_BR; }
		[ -n "$LED_BLUE" ] && { lc_br "$LED_BLUE"; LC_BR_B=$LC_BR; }
	else
		return 1
	fi
	[ -n "$LC_OK" ] || return 2
}

# lc_set COLOUR (white red green blue dark)
lc_set() {
	case $1 in
	white) lc_r=1 lc_g=1 lc_b=1 ;;
	red) lc_r=1 lc_g=0 lc_b=0 ;;
	green) lc_r=0 lc_g=1 lc_b=0 ;;
	blue) lc_r=0 lc_g=0 lc_b=1 ;;
	*) lc_r=0 lc_g=0 lc_b=0 ;;
	esac
	if [ -n "$LED_MC" ]; then
		if [ "$1" != dark ]; then
			lc_v=
			for lc_c in $LED_MC_IDX; do
				case $lc_c in
				red) lc_v="$lc_v $((lc_r * LC_BR_MC))" ;;
				green) lc_v="$lc_v $((lc_g * LC_BR_MC))" ;;
				blue) lc_v="$lc_v $((lc_b * LC_BR_MC))" ;;
				*) lc_v="$lc_v 0" ;;
				esac
			done
			lc_w "$LED_MC/multi_intensity" "${lc_v# }"
		fi
		lc_w "$LED_MC/brightness" $(((lc_r | lc_g | lc_b) * LC_BR_MC))
		return
	fi
	if [ -n "$LED_RED" ] && [ "$lc_r" != "$LC_SR" ]; then lc_w "$LED_RED/brightness" $((lc_r * LC_BR_R)); LC_SR=$lc_r; fi
	if [ -n "$LED_GREEN" ] && [ "$lc_g" != "$LC_SG" ]; then lc_w "$LED_GREEN/brightness" $((lc_g * LC_BR_G)); LC_SG=$lc_g; fi
	if [ -n "$LED_BLUE" ] && [ "$lc_b" != "$LC_SB" ]; then lc_w "$LED_BLUE/brightness" $((lc_b * LC_BR_B)); LC_SB=$lc_b; fi
}

# LEDs dark, blue's trigger (the kernel's heartbeat) restored; led_worker re-applies
# the stage to red/green once this process is gone or idle.
lc_handback() {
	lc_set dark
	if [ -n "$LED_BLUE" ] && [ -z "$LED_MC" ] && [ -n "$LC_BLUE_TRIG" ] && [ "$LC_BLUE_TRIG" != none ]; then
		lc_w "$LED_BLUE/trigger" "$LC_BLUE_TRIG"
	fi
}

# lc_cycle STOP: play LC_TL once. STOP=1: return 1 at the next symbol once a reset is
# armed (/tmp/lvr-reset), so the reset path gets the LEDs back within one symbol.
lc_cycle() {
	for lc_s in $LC_TL; do
		if [ "$1" = 1 ] && [ -e "$R/tmp/lvr-reset" ]; then return 1; fi
		lc_c=${lc_s%%:*}
		lc_x=${lc_s#*:}
		if [ "${lc_x%%:*}" -gt 0 ]; then
			lc_set "$lc_c"
			lc_sleep "${lc_x%%:*}"
			lc_set dark
		fi
		lc_sleep "${lc_x#*:}"
	done
}

# ledcode_player PATH CYCLES [WAIT_PID]. CYCLES 0 = forever (path 4: no watchdog), then
# alternating with LEDCODE_STUCK_GAP s of the 'stuck' slow red blink. The caller writes
# "starting" to /tmp/lvr-ledcode.pid before the fork and the new PID right after it.
ledcode_player() {
	if [ -n "$3" ]; then
		lc_i=0
		while kill -0 "$3" 2>/dev/null && [ $lc_i -lt 30 ]; do sleep 0.2; lc_i=$((lc_i + 1)); done
	fi
	uptime_s
	diag_deferred
	usb_diag
	lc_collect "$1" "$INIT_UP"
	lc_encode
	uptime_s
	log "$LC_LINE"
	# One-line USB summary, also kept in /tmp/lvr-usbdiag for a later shell.
	log "usbdiag: t=$UP $LC_USBLINE regdump=$UD_STATE"
	echo "t=$UP path=$1 $LC_USBLINE regdump=$UD_STATE" 2>/dev/null >> "$R/tmp/lvr-usbdiag"
	log "ledcode drivers:${LC_DRV:- none}"
	log "ledcode: cycle $((LC_MS / 1000)).$(((LC_MS % 1000) / 100)) s, cycles ${2} (0 = until a long press)"
	LC_ME=
	lc_i=0
	while [ $lc_i -lt 30 ]; do
		read -r LC_ME 2>/dev/null < "$R/tmp/lvr-ledcode.pid"
		case $LC_ME in [1-9]*) break ;; esac
		LC_ME=
		sleep 0.1
		lc_i=$((lc_i + 1))
	done
	if [ "$2" != 0 ] && [ -e "$R/tmp/lvr-reset" ]; then
		log "ledcode: reset already armed; not played"
		return
	fi
	lc_led_setup
	case $? in
	1) log "ledcode: no red/green/blue LED; not played"; return ;;
	2) log "ledcode: every LED write failed; not played"; return ;;
	esac
	LC_BLUE_TRIG=${LC_BLUE_TRIG:-none}
	log "ledcode: playing on red=${LED_RED##*/} green=${LED_GREEN##*/} blue=${LED_BLUE##*/} multicolor=${LED_MC##*/} (blue trigger ${LC_BLUE_TRIG})"
	if [ "$2" = 0 ]; then
		while :; do
			lc_handback
			echo idle 2>/dev/null > "$R/tmp/lvr-ledcode.pid"
			sleep "$LEDCODE_STUCK_GAP"
			echo "$LC_ME" 2>/dev/null > "$R/tmp/lvr-ledcode.pid"
			lc_sleep 1000
			lc_led_setup
			lc_cycle 0
		done
	fi
	lc_sleep 1000
	lc_k=0
	while [ $lc_k -lt "$2" ]; do
		if ! lc_cycle 1; then
			lc_handback
			uptime_s
			log "ledcode: reset armed; stopped in cycle $((lc_k + 1)), LEDs handed back"
			return
		fi
		lc_k=$((lc_k + 1))
	done
	lc_handback
	uptime_s
	log "ledcode: $lc_k cycle(s) done, LEDs handed back"
}

# ledcode_then_reset PATH REASON (main shell): play the code in the background, wait
# for it with a hard limit, then force_reset. Never returns. The wait ends when the
# player exits, LEDCODE_MAX s after the start, or 60 s before the dead-man deadline,
# whichever comes first; the dead-man itself is untouched and stays the backstop.
ledcode_then_reset() {
	uptime_s
	lc_t0=$UP
	echo starting 2>/dev/null > "$R/tmp/lvr-ledcode.pid"
	ledcode_player "$1" "$LEDCODE_CYCLES" &
	lc_pid=$!
	echo "$lc_pid" 2>/dev/null > "$R/tmp/lvr-ledcode.pid"
	log "ledcode: player $lc_pid (path $1); reset when it ends, by uptime $((lc_t0 + LEDCODE_MAX))s at the latest"
	lc_n=0
	while kill -0 "$lc_pid" 2>/dev/null; do
		uptime_s
		if [ "$UP" -ge $((lc_t0 + LEDCODE_MAX)) ]; then
			log "ledcode: time limit (${LEDCODE_MAX}s) reached"
			break
		fi
		lc_dl=
		read -r lc_dl _ 2>/dev/null < "$R/tmp/lvr-deadline"
		if to_num "${lc_dl#deadline_uptime=}" && [ $((UP + 60)) -ge "$NUM" ]; then
			log "ledcode: dead-man deadline ${NUM}s is <= 60 s away; resetting now"
			break
		fi
		# Coarse backstop if uptime stopped advancing: each pass sleeps >= 0.5 s.
		[ $lc_n -ge $((LEDCODE_MAX * 2)) ] && break
		lc_n=$((lc_n + 1))
		sleep 0.5
	done
	force_reset "$2"
}

# Follow the kernel log into /tmp/kernel.log. BusyBox dmesg can't follow and cat
# exits on EPIPE (reader overrun), so restart cat and drop records already written
# (by sequence number). Dictionary continuation lines (leading space) are skipped.
klog() {
	while :; do cat "$R/dev/kmsg"; sleep 1; done | awk '
		/^ / { next }
		{
			sep = index($0, ";")
			if (!sep) next
			split(substr($0, 1, sep - 1), f, ",")
			if (f[2] !~ /^[0-9]+$/) next
			seq = f[2] + 0
			if (seen && seq <= last) next
			seen = 1
			last = seq
			printf "[%12.6f] %s\n", f[3] / 1000000, substr($0, sep + 1)
			fflush()
		}' >> "$R/tmp/kernel.log"
}

# Heartbeat dead-man: reset DEADMAN s after the last heartbeat; before the first one, DEADMAN s after
# boot. `tools/dev heartbeat` writes the target's uptime (seconds, from /proc/uptime) into
# /tmp/lvr-heartbeat. New content counts only if it is a plain number of seconds, not ahead of the
# current uptime and newer than the last accepted one; anything else (partial, garbled, future, stale)
# is logged once and never accepted later. The deadline never moves earlier; /tmp/lvr-deadline shows it.
# Return request: /tmp/lvr-return, checked at the top of every pass, resets now:
# warm_return (kernel warm-reset trigger, then the same force_reset as a fallback). Nothing
# removes the file; warm_return never returns.
deadman() {
	trap '' HUP INT TERM
	base=0
	hb=
	seen=
	warned=
	n=0
	shown=
	while :; do
		uptime_s
		if [ -e "$R/tmp/lvr-return" ]; then
			log "return-android: request /tmp/lvr-return seen"
			warm_return "return-android requested"
		fi
		raw=
		read -r raw _ 2>/dev/null < "$R/tmp/lvr-heartbeat"
		if [ -n "$raw" ] && [ "$raw" != "$seen" ]; then
			seen=$raw
			if [ -n "$UP_OK" ] && to_num "${raw%%.*}" && [ "$NUM" -le "$UP" ] &&
			   [ "$NUM" -gt "$base" ]; then
				base=$NUM
				n=0
				if [ -z "$hb" ]; then
					log "dead-man: first heartbeat at uptime ${base}s; reset at uptime $((base + DEADMAN))s unless refreshed"
				elif [ -n "$warned" ]; then
					log "dead-man: heartbeat at uptime ${base}s; reset postponed to uptime $((base + DEADMAN))s"
				fi
				hb=$base
				warned=
			else
				log "dead-man: ignored heartbeat '$raw' (uptime ${UP}s, last accepted ${hb:-none})"
			fi
		fi
		deadline=$((base + DEADMAN))
		if [ "$shown" != "$deadline" ]; then
			echo "deadline_uptime=$deadline heartbeat=${hb:-none}" > "$R/tmp/lvr-deadline"
			shown=$deadline
		fi
		[ "$UP" -ge "$deadline" ] && break
		# Coarse backstop if uptime stopped advancing: each pass sleeps >= 5 s, and n
		# restarts only at an accepted heartbeat, so this never fires early.
		[ $((n * 5)) -ge "$DEADMAN" ] && break
		if [ -z "$warned" ] && [ $((UP + 60)) -ge "$deadline" ]; then
			log "dead-man: watchdog reset in <= 60 s (no heartbeat since uptime ${hb:-0}s)"
			warned=1
		fi
		n=$((n + 1))
		sleep 5
	done
	force_reset "dead-man: no heartbeat for ${DEADMAN}s (last ${hb:-none}, lvr_deadman=$DEADMAN)"
}

uptime_s
INIT_UP=$UP
log "reached $(uname -r)"
# LED code time divisor: 1 on the device; test mode only may speed the code up.
LC_DIV=1
if [ -n "$R" ] && to_num "$LVR_TEST_LEDCODE_DIV" && [ "$NUM" -ge 1 ] && [ "$NUM" -le 1000 ]; then
	LC_DIV=$NUM
fi
led_stage red
led_worker &
echo $! > "$R/tmp/led.pid"

# lvr_deadman=SECONDS on the kernel cmdline (last one wins), clamped to
# DEADMAN_MIN..DEADMAN_MAX; empty or non-numeric -> default; more than 6 digits -> max.
DEADMAN=
set -f
for arg in $(cat "$R/proc/cmdline" 2>/dev/null); do
	case $arg in
	lvr_deadman=*) DEADMAN=${arg#lvr_deadman=} ;;
	esac
done
set +f
case $DEADMAN in
''|*[!0-9]*) DEADMAN=$DEADMAN_DEFAULT ;;
*)
	if to_num "$DEADMAN"; then
		DEADMAN=$NUM
		[ "$DEADMAN" -lt "$DEADMAN_MIN" ] && DEADMAN=$DEADMAN_MIN
		[ "$DEADMAN" -gt "$DEADMAN_MAX" ] && DEADMAN=$DEADMAN_MAX
	else
		DEADMAN=$DEADMAN_MAX
	fi
	;;
esac
deadman &
echo "timeout=$DEADMAN pid=$!" > "$R/tmp/lvr-deadman"
log "dead-man armed: watchdog reset ${DEADMAN}s after the last heartbeat (/tmp/lvr-heartbeat; none yet: uptime ${DEADMAN}s) (pid $!)"

klog &
echo $! > "$R/tmp/klog.pid"

g=$R/config/usb_gadget/qkx
mkdir -p "$g/strings/0x409" "$g/configs/c.1/strings/0x409" "$g/functions/ecm.usb0"
echo 0x1d6b > "$g/idVendor"
echo 0x0105 > "$g/idProduct"
echo QKX-RAM > "$g/strings/0x409/serialnumber"
echo quest2-kexec > "$g/strings/0x409/manufacturer"
echo 'RAM-only USB Ethernet shell' > "$g/strings/0x409/product"
echo 'ECM' > "$g/configs/c.1/strings/0x409/configuration"
echo 250 > "$g/configs/c.1/MaxPower"
# Fixed MACs keep the host interface name stable across boots.
echo 02:51:4b:58:00:02 > "$g/functions/ecm.usb0/dev_addr"
echo 02:51:4b:58:00:01 > "$g/functions/ecm.usb0/host_addr"
ln -s "$g/functions/ecm.usb0" "$g/configs/c.1/ecm.usb0"

uptime_s
udc_deadline=$((UP + UDC_WAIT))
log "waiting up to ${UDC_WAIT}s for a UDC"
udc_list
log "udc: ${UDCS:-none}"
udc=
n=0
while :; do
	for c in "$R"/sys/class/udc/*; do
		[ -e "$c" ] || continue
		echo "${c##*/}" > "$g/UDC" && udc=$c && break 2
	done
	uptime_s
	[ "$UP" -ge "$udc_deadline" ] && break
	if [ $((n % 10)) -eq 0 ]; then
		udc_list
		log "waiting for UDC (udc: ${UDCS:-none})"
	fi
	n=$((n + 1))
	bootdone k
	sleep 1
done
uptime_s
if [ -z "$udc" ]; then
	udc_list
	log "no UDC after ${UDC_WAIT}s (udc: ${UDCS:-none})"
	# The player logs the deferred probes (diag_deferred) and the code, then blinks it.
	ledcode_then_reset 1 "no UDC after ${UDC_WAIT}s"
fi
log "bound ${udc##*/}"
led_stage green
# Keep the DWC3 glue device out of runtime suspend.
echo on 2>/dev/null > "$udc/device/../power/control"

ifconfig lo 127.0.0.1 up
ifconfig usb0 10.42.0.2 netmask 255.255.255.0 up
cat > "$R/tmp/udhcpd.conf" <<EOC
interface usb0
start 10.42.0.1
end 10.42.0.1
max_leases 1
lease_file /tmp/udhcpd.leases
option subnet 255.255.255.0
option lease 86400
EOC
touch "$R/tmp/udhcpd.leases"
udhcpd "$R/tmp/udhcpd.conf"
(while :; do telnetd -F -l /bin/sh -p 23; log "telnetd exited ($?); restarting"; sleep 1; done) &
echo $! > "$R/tmp/telnet.pid"

bootdone d

# The host configures the gadget within a second or two of enumeration. If that never
# happens (PHY/link failure, no host), nobody can reach this shell: reset instead of
# idling. "suspended" counts too: the host may autosuspend the idle link afterwards.
uptime_s
cfg_deadline=$((UP + USB_CFG_WAIT))
last=
while :; do
	st=
	read -r st 2>/dev/null < "$udc/state"
	if [ "$st" != "$last" ]; then
		log "UDC state: ${st:-unreadable}"
		last=$st
	fi
	case $st in
	configured|suspended) break ;;
	esac
	uptime_s
	if [ "$UP" -ge "$cfg_deadline" ]; then
		ledcode_then_reset 2 "USB not configured ${USB_CFG_WAIT}s after bind (state ${st:-unreadable})"
	fi
	sleep 1
done

led_stage green-hb
uptime_s
log "ready: telnet 10.42.0.2 (dead-man: reset ${DEADMAN}s after the last tools/dev heartbeat)"
hold_forever
