#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""syncboss_capture: IMU-only capture from the sensor MCU (syncboss) through the mainline quest2-syncboss driver.

    syncboss_capture.py capture --out DIR --duration SECS [--label L] [--schedule NAME]
        Runs on the headset, as root in the stage-2 chroot, on a mainline boot where the
        `quest2-syncboss` driver is loaded and armed (identity_dma = Y). It
          1. checks the device nodes, the arm flag and the per-boot open budget, and refuses
             before opening anything if any fails;
          2. follows /dev/kmsg read-only from now on;
          3. opens stream0 and control0 read-only, reserves an open slot, then opens
             /dev/syncboss0 write-only (this powers and resets the MCU; it takes up to ~5 s to wake);
          4. sends IMU enable (6e 01 01 00) and waits up to 3 s for the 0x7d reply;
          5. records every read() of stream0/control0 for SECS (<= 60) seconds, plus the driver's
             kernel-log lines, into DIR/capture.sbcap;
          6. sends IMU disable (6f 02 01 00) and waits up to 2 s for the reply;
          7. closes syncboss0 (the driver then sends SHUTDOWN, waits for the MCU to sleep, holds it in
             reset and cuts the rails) and reads the driver's `streaming stopped:` line;
          8. writes DIR/summary.json, counters.txt (key=value) and imu.csv, and records the outcome in
             the open budget.
        SIGINT/SIGTERM/SIGHUP, the deadline (SECS + 20 s) or an error after the enable write send the
        disable packet and close. An abort line in the kernel log (A2, A5, or a kernel-side command
        other than the three expected) stops at once and closes without another write. After
        SIGKILL the kernel's close of syncboss0 still sends SHUTDOWN.
    syncboss_capture.py parse FILE [--dump N] [--csv OUT] [--imu-csv OUT]
        Host: split recorded reads into packets, summarise, decode type 0x50 (also reads the
        Android-kernel version 2 capture format).
    syncboss_capture.py cues NAME
        Host: print motion schedule NAME as a countdown for whoever holds the headset.
    Tests: tools/test_syncboss_capture.py (fake driver replaying synthetic IMU records).

Safety (abort codes A2-A6 are at KMSG_ABORT_SB):
  * The only bytes this tool can send to the MCU are the two packets in APPROVED_TX, via
    McuLink.send(name): looked up by name, each at most once per open, enable first, disable only after
    a successful enable. os.write appears only there; no ioctl/fcntl/ctypes/mmap/socket/subprocess (AST
    check in the tests). The driver also refuses any other buffer (-EPERM, `refused write`).
  * /dev/syncboss0 is opened at most twice per boot (OpenBudget): before each open the tool creates
    <STATE_DIR>/<boot_id>.open<N> exclusively, after it <...>.open<N>.outcome. A second open needs open
    1 to have ended `ok` or `a4_wake_timeout` (the one retry). Any other outcome, a missing one (killed,
    still running) or A3/A5/A6 writes <boot_id>.stop and no further open happens that boot. STATE_DIR is
    in /tmp (RAM) and keyed by boot_id, so a stale file never counts for another boot.
  * Opens only syncboss0 (O_WRONLY), syncboss_stream0 and syncboss_control0 (O_RDONLY | O_NONBLOCK) and
    /dev/kmsg (O_RDONLY | O_NONBLOCK); reads boot_id and the identity_dma parameter. Nothing under
    `spi1.0/control/`, no powerstate, SWD, ttyHS*, no other sysfs.
  * Writes files only in DIR, STATE_DIR and the marker file it consumes; every file write goes through
    _wopen(), which refuses /dev, /sys and /proc paths.

Capture file format (little endian):
  b"SBCAP001", u32 n, n bytes JSON metadata, then records:
  u8 kind, u8 src, u16 0, u32 len, s64 t_mono_ns, s64 t_boot_ns, len bytes payload
  kind 1 DATA (src 1 stream0, 2 control0; payload = raw bytes returned by one read()),
  kind 2 MARK (utf-8 label), kind 3 META (JSON), kind 4 EVENT (JSON; incl. {"event": "kmsg"}),
  kind 5 TX (src 3 syncboss0; payload = the exact bytes passed to write(), logged before it).
  A DATA payload is a run of uapi_pkt_v2 records: u8 hver, u8 hlen, u8 from_driver,
  u8 nsync_status, s64 nsync_offset_us, then u8 type, u8 seq, u8 data_len, data.

Type 0x50 (IMU sample) payload: u64 w0 (bits 3:0 imu_id, timestamp = w0 >> 16), f32 accel xyz (g), f32 gyro xyz
(deg/s), i32 aux = 36 bytes. Extra bytes are kept as hex in the CSV.
"""
import argparse
import errno
import json
import os
import re
import select
import signal
import struct
import sys
import time
import types

VERSION = "3-mainline"
MAGIC = b"SBCAP001"
REC = struct.Struct("<BBHIqq")
K_DATA, K_MARK, K_META, K_EVENT, K_TX = 1, 2, 3, 4, 5
SRC_STREAM, SRC_CONTROL, SRC_MCU = 1, 2, 3
SRC_NAMES = {SRC_STREAM: "stream", SRC_CONTROL: "control", SRC_MCU: "syncboss0"}

DEV_MCU = "/dev/syncboss0"
DEV_STREAM = "/dev/syncboss_stream0"
DEV_CONTROL = "/dev/syncboss_control0"
DEV_KMSG = "/dev/kmsg"
BOOT_ID = "/proc/sys/kernel/random/boot_id"
ARM_PARAM = "/sys/module/quest2_syncboss/parameters/identity_dma"
STATE_DIR = "/tmp/lvr/sb-opens"
FORBIDDEN = ("swd", "update_firmware", "update_status", "powerstate", "reset", "ttyhs",
             "directchannel", "spi1.0", "/control/")

# ---- The complete set of bytes this tool may ever write to the MCU -------------------
# Nothing else. No 0x77
# (rate), no 0x74/0x75 (mag), no camera/controller/powerstate/reset/SWD.
APPROVED_TX = types.MappingProxyType({
    "imu_enable": bytes((0x6E, 0x01, 0x01, 0x00)),
    "imu_disable": bytes((0x6F, 0x02, 0x01, 0x00)),
})
APPROVED_HEX = ("6e010100", "6f020100")
assert tuple(v.hex() for v in APPROVED_TX.values()) == APPROVED_HEX
# What the kernel may log as `sent command`: the two packets above and its own SHUTDOWN.
KERNEL_SENT_OK = ("6e010100", "6f020100", "5a0000")
REPLY_TYPE = 0x7D            # firmware status reply, u32 LE status (0 = OK), echoes seq
REPLY_SEQ = types.MappingProxyType({"imu_enable": 1, "imu_disable": 2})
IMU_TYPE = 0x50
ENABLE_REPLY_TIMEOUT_S = 3.0
DISABLE_REPLY_TIMEOUT_S = 2.0

MAX_DURATION = 60            # bounded capture
HARD_MARGIN_S = 20           # SIGALRM at duration + this: recording stops even if the loop stalls
CLOSE_LOG_WAIT_S = 6.0       # wait for the driver's `streaming stopped:` line after close
MAX_OPENS = 2                # per boot
RETRYABLE = ("ok", "a4_wake_timeout")
READ_SIZE = 65536
KMSG_READ = 8192
KMSG_KEEP = 2000             # kernel-log lines recorded in the capture at most
REPEAT_LIMIT = 3             # `bad frame` / `spi_sync_locked` lines that count as "keep repeating"
UAPI_HDR = struct.Struct("<BBBBq")  # 12 bytes, header v2
IMU_V = struct.Struct("<Q6fi")      # 36 bytes
IMU_CSV_HEADER = ("t_mono_ns,t_boot_ns,read_idx,seq,nsync_status,nsync_offset_us,data_len,"
                  "imu_id,w0_bits4_15,mcu_ts,accel_x_g,accel_y_g,accel_z_g,"
                  "gyro_x_dps,gyro_y_dps,gyro_z_dps,aux,extra_hex\n")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# ---- kernel-log rules (driver strings of quest2-syncboss.c) ---------
SB_TAG = "quest2-syncboss"
# (code, regex): matched on lines that carry SB_TAG. A3: driver not armed; A4: MCU did not wake;
# A5: refused write or link/protocol errors; A6: MCU did not sleep; FAIL: driver-side failure.
KMSG_ABORT_SB = (
    ("A4", re.compile(r"MCU not awake within \d+ ms")),
    ("A3", re.compile(r"identity_dma is not set; refusing")),
    ("A5", re.compile(r"refused write")),
    ("A5", re.compile(r"consecutive failed transactions")),
    ("A5", re.compile(r"ready IRQ storm")),
    ("A5", re.compile(r"dropping command")),
    ("A5", re.compile(r"data packet overflow")),
    ("A6", re.compile(r"MCU did not go to sleep within")),
    ("FAIL", re.compile(r"stream failed, skipping SHUTDOWN")),
    ("FAIL", re.compile(r"failed to enable supplies|controller resume failed")),
)
KMSG_REPEAT_SB = re.compile(r"bad frame from MCU|spi_sync_locked")
# Any source (A2, A7): oops/BUG/SError/stalls/lockups/WARN.
KMSG_ABORT_ANY = re.compile(r"Internal error|Oops|BUG:|Unable to handle|SError|rcu.{0,40}stall|"
                            r"soft lockup|hard LOCKUP|Kernel panic|WARNING: CPU")
KMSG_AWAKE = re.compile(r"MCU awake after (\d+) ms")
KMSG_ASLEEP = re.compile(r"MCU asleep after (\d+) ms")
KMSG_SENT = re.compile(r"sent command ((?:[0-9a-f]{2} ?)+)\s*$")
KMSG_FULL = re.compile(r"(stream|control) reader full, dropping")
KMSG_STOPPED = re.compile(
    r"streaming stopped: (\d+) transactions, (\d+) busy, (\d+) bad magic, (\d+) bad checksum, "
    r"(\d+) spi errors, (\d+) overflows, (\d+) commands( \(failed\))?")
STOP_KEYS = ("transactions", "busy", "bad_magic", "bad_checksum", "spi_errors", "overflows",
             "commands")

# Motion cues: (start_offset_s, label). Reference pose "level": headset held as if worn,
# visor (front cameras) pointing away from the holder, top up. All end by 58 s (MAX_DURATION).
def _axis(pos, neg):
    return [(0, "level_hold_still"), (6, f"rotate_90_{pos}_and_hold"), (13, "level_hold"),
            (19, f"rotate_90_{neg}_and_hold"), (26, "level_hold"),
            (32, "slow_swing_45_deg_each_way_1_per_2s"), (42, "level_hold"),
            (46, "fast_swing_30_deg_each_way_2_per_s"), (52, "level_hold_still"), (58, "end")]


SCHEDULES = {
    "still": [(0, "still_on_table")],
    "pitch": _axis("visor_up", "visor_down"),
    "yaw": _axis("visor_left", "visor_right"),
    "roll": _axis("right_side_down", "left_side_down"),
    "poses": [(0, "move_to_level"), (3, "hold_level"), (10, "move"), (13, "hold_visor_up"),
              (20, "move"), (23, "hold_visor_down"), (30, "move"),
              (33, "hold_left_side_down"), (40, "move"), (43, "hold_right_side_down"),
              (50, "move"), (52, "hold_level"), (58, "end")],
}


class Refused(Exception):
    """A send or file write that is not allowed."""


class Abort(Exception):
    """Stop the capture early (cleanup still runs)."""


def say(msg):
    """Progress line on stderr (redirect it to a log file)."""
    print(f"[{time.strftime('%H:%M:%S')}] syncboss_capture: {msg}", file=sys.stderr, flush=True)


def now():
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC), time.clock_gettime_ns(time.CLOCK_BOOTTIME)


def guard(path):
    low = path.lower()
    for bad in FORBIDDEN:
        if bad in low:
            sys.exit(f"syncboss_capture: refusing forbidden path {path!r} (contains {bad!r})")
    return path


def _wopen(path, mode):
    """The only place a file is opened for writing. Never /dev, /sys, /proc."""
    real = os.path.realpath(path)
    for seg in ("/dev/", "/sys/", "/proc/"):
        if seg in real + "/":
            raise Refused(f"refusing to write {real!r}")
    guard(real)
    return open(path, mode)


def _read_small(path):
    with open(path, "r") as f:
        return f.read(256).strip()


def _open_mcu(path):
    """The only open of /dev/syncboss0, and the only os.open with a write flag."""
    return os.open(path, os.O_WRONLY)


class Writer:
    def __init__(self, path, meta):
        self.f = _wopen(path, "wb")
        blob = json.dumps(meta, sort_keys=True).encode()
        self.f.write(MAGIC + struct.pack("<I", len(blob)) + blob)

    def rec(self, kind, src, payload, t=None):
        tm, tb = t or now()
        self.f.write(REC.pack(kind, src, 0, len(payload), tm, tb))
        self.f.write(payload)

    def json(self, kind, obj, t=None):
        self.rec(kind, 0, json.dumps(obj, sort_keys=True).encode(), t)

    def close(self):
        self.f.flush()
        os.fsync(self.f.fileno())
        self.f.close()


def try_log(w, kind, obj):
    """Record without letting a full/broken output file stop the MCU cleanup."""
    try:
        w.json(kind, obj)
    except Exception:  # noqa: BLE001 - cleanup must continue
        pass


# ---- per-boot open budget ----------------------------------------------------------------

class OpenBudget:
    """At most MAX_OPENS opens of /dev/syncboss0 per boot (A4-A6), kept on the
    target's /tmp (RAM) and keyed by boot_id. Fail-closed: any doubt stops further opens."""

    def __init__(self, state_dir, boot_id):
        if not UUID.fullmatch(boot_id or ""):
            raise Refused(f"bad boot_id {boot_id!r}")
        self.dir = state_dir
        self.boot_id = boot_id
        self.slot = None

    def _p(self, suffix):
        return os.path.join(self.dir, f"{self.boot_id}.{suffix}")

    def outcome(self, n):
        try:
            with open(self._p(f"open{n}.outcome")) as f:
                return json.load(f).get("outcome")
        except (OSError, ValueError, AttributeError):
            return None

    def latched(self):
        try:
            with open(self._p("stop")) as f:
                return f.read(400).strip() or "stop file present"
        except OSError:
            return None

    def latch(self, why):
        if self.latched():
            return
        try:
            with _wopen(self._p("stop.tmp"), "w") as f:
                f.write(why[:300] + "\n")
            os.replace(self._p("stop.tmp"), self._p("stop"))
        except OSError:
            pass

    def check(self):
        """Raise Refused if no open is allowed now (creates nothing except a stop latch)."""
        why = self.latched()
        if why:
            raise Refused(f"no further opens this boot: {why}")
        for n in range(1, MAX_OPENS + 1):
            if not os.path.exists(self._p(f"open{n}")):
                return n
            oc = self.outcome(n)
            if oc not in RETRYABLE:
                why = (f"open {n} of this boot has outcome {oc!r}" if oc else
                       f"open {n} of this boot has no outcome (killed or still running)")
                self.latch(why)
                raise Refused(f"no further opens this boot: {why}")
        raise Refused(f"no further opens this boot: {MAX_OPENS} opens already used")

    def reserve(self, label):
        n = self.check()
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        try:
            with _wopen(self._p(f"open{n}"), "x") as f:
                f.write(json.dumps({"pid": os.getpid(), "label": label, "t_unix": time.time()}) + "\n")
        except FileExistsError:
            self.latch(f"open {n} slot raced (two capture processes)")
            raise Refused(f"open {n} slot already taken (another capture running?)")
        self.slot = n
        return n

    def finish(self, outcome, detail=""):
        if self.slot is None:
            return
        rec = {"outcome": outcome, "detail": detail[:300], "t_unix": time.time()}
        tmp = self._p(f"open{self.slot}.outcome.tmp")
        with _wopen(tmp, "w") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
        os.replace(tmp, self._p(f"open{self.slot}.outcome"))
        if outcome not in RETRYABLE:
            self.latch(f"open {self.slot}: {outcome} {detail}"[:300])
        elif self.slot >= MAX_OPENS:
            self.latch(f"{MAX_OPENS} opens used")


# ---- kernel log ---------------------------------------------------------------------------

class KmsgWatch:
    """Follows /dev/kmsg from 'now' with its own read position (no effect on other readers).
    Collects the driver's lines and classifies abort lines."""

    def __init__(self, path, w):
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            os.lseek(self.fd, 0, os.SEEK_END)
        except OSError:
            pass
        self.w = w
        self.part = b""
        self.lines = []          # (t_mono_ns, text) of recorded lines
        self.codes = []          # [(code, text)] abort codes in order
        self.repeat = 0
        self.overruns = 0
        self.awake_ms = self.asleep_ms = None
        self.sent = []
        self.reader_full = 0
        self.stopped = None      # dict from the `streaming stopped:` line

    @property
    def abort(self):
        """First code that means: stop now, no further writes."""
        for c, t in self.codes:
            if c in ("A2", "A5", "FAIL"):
                return (c, t)
        return None

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def poll(self):
        while self.fd is not None:
            try:
                b = os.read(self.fd, KMSG_READ)
            except BlockingIOError:
                break
            except OSError as e:
                if e.errno == errno.EPIPE:      # overrun: next read continues with the next record
                    self.overruns += 1
                    continue
                self.codes.append(("FAIL", f"kmsg read error {e.errno}"))
                self.close()
                break
            if not b:
                break
            self.part += b
            *done, self.part = self.part.split(b"\n")
            for raw in done:
                self.line(raw.decode(errors="replace"))

    def line(self, raw):
        if not raw or raw.startswith(" "):
            return
        text = raw.split(";", 1)[1] if ";" in raw else raw
        sb = SB_TAG in text
        hit_any = KMSG_ABORT_ANY.search(text)
        if not sb and not hit_any:
            return
        t = now()
        if len(self.lines) < KMSG_KEEP:
            self.lines.append((t[0], text))
            try_log(self.w, K_EVENT, {"event": "kmsg", "line": text[:400]})
        if hit_any:
            self.codes.append(("A2", text[:200]))
        if not sb:
            return
        for code, rx in KMSG_ABORT_SB:
            if rx.search(text):
                self.codes.append((code, text[:200]))
        if KMSG_REPEAT_SB.search(text):
            self.repeat += 1
            if self.repeat == REPEAT_LIMIT:
                self.codes.append(("A5", f"{REPEAT_LIMIT} bad frame/spi_sync_locked lines: {text[:120]}"))
        m = KMSG_AWAKE.search(text)
        if m:
            self.awake_ms = int(m.group(1))
        m = KMSG_ASLEEP.search(text)
        if m:
            self.asleep_ms = int(m.group(1))
        m = KMSG_SENT.search(text)
        if m:
            hx = m.group(1).replace(" ", "")
            self.sent.append(hx)
            if hx not in KERNEL_SENT_OK:
                self.codes.append(("A5", f"kernel sent an unexpected command {hx}"))
        if KMSG_FULL.search(text):
            self.reader_full += 1
        m = KMSG_STOPPED.search(text)
        if m:
            self.stopped = dict(zip(STOP_KEYS, (int(x) for x in m.groups()[:7])))
            self.stopped["failed"] = bool(m.group(8))


# ---- device link ------------------------------------------------------------------------

class McuLink:
    """The only path from this tool to /dev/syncboss0. Holds the fd; sends by name only."""

    def __init__(self, fd, w):
        self._fd = fd
        self._w = w
        self.sent = []               # names, in order, recorded before each write()
        self.result = {}             # name -> {"written": n | "errno": e}
        self.enable_written = False

    def send(self, name):
        if not isinstance(name, str) or name not in APPROVED_TX:
            raise Refused(f"not an approved packet: {name!r}")
        pkt = APPROVED_TX[name]
        if pkt.hex() not in APPROVED_HEX or len(pkt) != 4:
            raise Refused(f"approved table corrupted: {pkt.hex()}")
        if name in self.sent:
            raise Refused(f"{name} already sent on this open")
        if name == "imu_enable" and self.sent:
            raise Refused("imu_enable must be the first packet")
        if name == "imu_disable" and not self.enable_written:
            raise Refused("imu_disable without a successful imu_enable write")
        self.sent.append(name)
        try_log(self._w, K_EVENT, {"event": "tx_begin", "name": name, "hex": pkt.hex()})
        try:
            self._w.rec(K_TX, SRC_MCU, pkt)
        except Exception:  # noqa: BLE001 - output broken: no enable, but disable still goes out
            if name == "imu_enable":
                raise
        try:
            n = os.write(self._fd, pkt)
        except OSError as e:
            self.result[name] = {"errno": e.errno, "err": e.strerror}
            try_log(self._w, K_EVENT, {"event": "tx_error", "name": name, "errno": e.errno})
            return False
        self.result[name] = {"written": n}
        try_log(self._w, K_EVENT, {"event": "tx_done", "name": name, "written": n})
        if n == len(pkt) and name == "imu_enable":
            self.enable_written = True
        return n == len(pkt)


class Reader:
    """Polls stream0/control0, records every read(), collects 0x7d replies from control0."""

    def __init__(self, w, fds):
        self.w = w
        self.fds = fds
        self.poller = select.poll()
        for fd in fds:
            self.poller.register(fd, select.POLLIN)
        self.replies = {}            # seq -> list of {"status", "len", "hex", "t_mono_ns"}
        self.stats = {"reads": {"stream": 0, "control": 0}, "bytes": {"stream": 0, "control": 0},
                      "eagain": 0, "empty": 0, "errors": [], "rec_errors": 0}

    def drain(self, timeout_ms):
        for fd, _ev in self.poller.poll(timeout_ms):
            src = self.fds[fd]
            name = SRC_NAMES[src]
            while True:
                try:
                    buf = os.read(fd, READ_SIZE)
                except BlockingIOError:
                    self.stats["eagain"] += 1
                    break
                except OSError as e:
                    self.stats["errors"].append([name, e.errno])
                    try_log(self.w, K_EVENT, {"event": "read_error", "src": src, "errno": e.errno})
                    break
                t = now()
                if not buf:
                    self.stats["empty"] += 1
                    time.sleep(0.001)
                    break
                try:
                    self.w.rec(K_DATA, src, buf, t)
                except Exception:  # noqa: BLE001 - keep parsing replies; the main loop aborts
                    self.stats["rec_errors"] += 1
                self.stats["reads"][name] += 1
                self.stats["bytes"][name] += len(buf)
                if src == SRC_CONTROL:
                    for item in split_packets(buf):
                        if item[0] != "error" and item[2] == REPLY_TYPE:
                            self.replies.setdefault(item[3], []).append(reply_info(item[4], t[0]))

    def wait_reply(self, seq, timeout_s, stop=None):
        """Return the first 0x7d reply with this seq, 'interrupted', or None on timeout."""
        end = time.monotonic() + timeout_s
        while True:
            if self.replies.get(seq):
                return self.replies[seq].pop(0)
            if stop is not None and stop():
                return "interrupted"
            left = end - time.monotonic()
            if left <= 0:
                return None
            self.drain(int(min(left, 0.02) * 1000) + 1)


def reply_info(data, t_mono):
    st = struct.unpack_from("<I", data)[0] if len(data) >= 4 else None
    return {"status": st, "len": len(data), "hex": data.hex(), "t_mono_ns": t_mono}


def decide_outcome(open_errno, rc, kw, enable_ok, disable_ok):
    """Outcome recorded in the open budget. Only 'ok' and 'a4_wake_timeout' allow another
    open this boot. Pure (kw: KmsgWatch-like)."""
    codes = [c for c, _ in kw.codes]
    if "A2" in codes:
        return "a2_kernel_error"
    if open_errno == errno.ETIMEDOUT:
        return "a4_wake_timeout" if set(codes) <= {"A4"} else "a4_plus_" + "_".join(sorted(set(codes)))
    if open_errno == errno.EPERM or "A3" in codes:
        return "a3_open_refused"
    if open_errno is not None:
        return f"open_failed_errno_{open_errno}"
    for c in ("A5", "FAIL", "A6", "A4"):
        if c in codes:
            return {"A5": "a5_abort_line", "FAIL": "stream_failed", "A6": "a6_no_sleep",
                    "A4": "a4_line_after_open"}[c]
    if kw.stopped is None:
        return "no_streaming_stopped_line"
    if kw.stopped["failed"]:
        return "stream_failed"
    if any(kw.stopped[k] for k in ("bad_magic", "bad_checksum", "spi_errors", "overflows")):
        return "anomaly_kernel_counters"
    if kw.asleep_ms is None:
        return "no_asleep_line"
    if rc != 0 or not (enable_ok and disable_ok):
        return f"not_clean_rc{rc}"
    return "ok"


def _status(r):
    """0x7d status of a reply, or the reason there is none."""
    return r.get("status") if isinstance(r, dict) else r


def write_counters(path, d):
    with _wopen(path, "w") as f:
        for k, v in d.items():
            if isinstance(v, (list, tuple)):
                v = ",".join(str(x) for x in v)
            f.write(f"{k}={'' if v is None else v}\n")


def cmd_capture(a, root=""):
    """root is for the offline tests only (not a command-line option)."""
    if not 1 <= a.duration <= MAX_DURATION:
        sys.exit(f"--duration must be 1..{MAX_DURATION} s")
    if a.schedule and a.schedule not in SCHEDULES:
        sys.exit(f"unknown schedule {a.schedule!r}; have {', '.join(SCHEDULES)}")
    mcu_p = guard(root + DEV_MCU)
    stream_p = guard(root + DEV_STREAM)
    control_p = guard(root + DEV_CONTROL)
    kmsg_p = guard(root + DEV_KMSG)
    marker_p = guard(a.marker_file)
    for p in (mcu_p, stream_p, control_p, kmsg_p):
        if not os.path.exists(p):
            sys.exit(f"missing {p}: not on the headset, driver not probed, or not in stage 2. "
                     "Nothing was opened.")
    try:
        arm = _read_small(root + ARM_PARAM)
    except OSError as e:
        sys.exit(f"cannot read {ARM_PARAM}: {e.strerror}. Nothing was opened.")
    if arm != "Y":
        sys.exit(f"identity_dma is {arm!r}, need 'Y' (armed boot; A3). Nothing was opened.")
    try:
        boot_id = _read_small(root + BOOT_ID)
        budget = OpenBudget(root + STATE_DIR, boot_id)
        budget.check()
    except (OSError, Refused) as e:
        sys.exit(f"refused: {e}. Nothing was opened.")

    os.makedirs(a.out, exist_ok=True)
    cap_path = os.path.join(a.out, "capture.sbcap")
    meta = {"tool": "syncboss_capture", "version": VERSION, "label": a.label,
            "duration_s": a.duration, "schedule": a.schedule, "boot_id": boot_id,
            "approved_tx": dict((k, v.hex()) for k, v in APPROVED_TX.items()),
            "uname": " ".join(os.uname()), "argv": sys.argv, "start_unix": time.time()}
    w = Writer(cap_path, meta)
    try:
        kw = KmsgWatch(kmsg_p, w)
    except OSError as e:
        w.close()
        sys.exit(f"cannot read {DEV_KMSG}: {e.strerror}. Nothing was opened.")

    stop = {"flag": False, "why": "duration"}

    def on_signal(signum, _frame):
        if not stop["flag"]:
            stop["flag"], stop["why"] = True, ("deadline" if signum == signal.SIGALRM else f"signal {signum}")
    old_handlers = {s: signal.signal(s, on_signal)
                    for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGALRM)}
    signal.alarm(int(a.duration + HARD_MARGIN_S))

    def tick():
        """Poll the kernel log; True if the capture must stop (signal, deadline, abort line)."""
        kw.poll()
        ab = kw.abort
        if ab and not stop["flag"]:
            stop["flag"], stop["why"] = True, f"kernel log {ab[0]}: {ab[1]}"
        return stop["flag"]

    mcu_fd, link, rd, open_errno = None, None, None, None
    stream_fds = {}
    result, rc = "ok", 0
    replies = {}
    t0 = time.monotonic()
    t_rec = None
    try:
        # 1. Readers first (no MCU effect; records go only to readers open before the packet).
        for src, p in ((SRC_STREAM, stream_p), (SRC_CONTROL, control_p)):
            try:
                stream_fds[os.open(p, os.O_RDONLY | os.O_NONBLOCK)] = src
            except OSError as e:
                raise Abort(f"open {p} failed: {e.strerror}")
        rd = Reader(w, stream_fds)
        if tick():
            raise Abort(f"{stop['why']} before opening syncboss0")
        # 2. Open budget, then syncboss0 (powers and boots the MCU; <= ~5 s).
        try:
            slot = budget.reserve(a.label)
        except (OSError, Refused) as e:
            raise Abort(f"refused: {e}")
        w.json(K_EVENT, {"event": "mcu_open_begin", "slot": slot})
        say(f"opening {DEV_MCU} (open {slot}/{MAX_OPENS} this boot; powers and boots the MCU)")
        try:
            mcu_fd = _open_mcu(mcu_p)
        except OSError as e:
            open_errno = e.errno
            w.json(K_EVENT, {"event": "mcu_open_failed", "errno": e.errno, "err": e.strerror})
            raise Abort(f"open {mcu_p} failed: {e.strerror}" +
                        (" (A4: MCU not awake)" if e.errno == errno.ETIMEDOUT else "") +
                        (" (A3: not armed)" if e.errno == errno.EPERM else ""))
        w.json(K_EVENT, {"event": "mcu_open_done"})
        link = McuLink(mcu_fd, w)
        say("stream0, control0, syncboss0 open")
        if tick():
            raise Abort(f"{stop['why']} before enable")

        # 3. IMU enable, then its 0x7d reply.
        if not link.send("imu_enable"):
            raise Abort(f"imu_enable write failed: {link.result.get('imu_enable')}")
        r = rd.wait_reply(REPLY_SEQ["imu_enable"], ENABLE_REPLY_TIMEOUT_S, tick)
        replies["imu_enable"] = r
        try_log(w, K_EVENT, {"event": "reply", "name": "imu_enable", "reply": r})
        if r is None:
            raise Abort(f"no 0x7d reply to imu_enable within {ENABLE_REPLY_TIMEOUT_S} s")
        if r == "interrupted":
            raise Abort(f"{stop['why']} while waiting for the imu_enable reply")
        if r["status"] != 0:
            raise Abort(f"imu_enable reply status {r['status']!r} (hex {r['hex']})")
        if rd.stats["rec_errors"]:
            raise Abort("cannot write the capture file (tmpfs full?)")

        # 4. Record until duration, signal, deadline or an abort line.
        say(f"imu_enable acknowledged (status 0); RECORDING {a.duration:g} s")
        t_rec = time.monotonic()
        sched = list(SCHEDULES.get(a.schedule, []))
        next_flush = t_rec + 2.0
        while not tick():
            if rd.stats["rec_errors"]:
                raise Abort("cannot write the capture file (tmpfs full?)")
            el = time.monotonic() - t_rec
            if el >= a.duration:
                break
            while sched and sched[0][0] <= el:
                w.rec(K_MARK, 0, ("sched:" + sched.pop(0)[1]).encode())
            rd.drain(50)
            if rd.stats["rec_errors"]:
                continue        # abort at the top of the loop, before any other write
            if os.path.exists(marker_p):
                try:
                    with open(marker_p, "r") as f:
                        label = f.read(200).strip()
                    os.unlink(marker_p)
                except OSError:
                    label = "<unreadable>"
                w.rec(K_MARK, 0, ("user:" + label).encode())
            if time.monotonic() >= next_flush:
                w.f.flush()
                next_flush += 2.0
        if rd.stats["rec_errors"]:
            raise Abort("cannot write the capture file (tmpfs full?)")
        if stop["flag"]:
            result, rc = f"stopped early: {stop['why']}", 1
    except Abort as e:
        result, rc = f"aborted: {e}", 2
    except Exception as e:  # noqa: BLE001 - always clean up the MCU
        result, rc = f"error: {e!r}", 3
    finally:
        signal.alarm(0)
        try:
            kw.poll()
        except Exception:  # noqa: BLE001
            pass
        # 5. Disable if (and only if) the enable write succeeded and the kernel log shows no
        #    abort line (A5: close without further writes); wait for the reply before closing.
        if link is not None and link.enable_written:
            if kw.abort:
                r = f"not sent: kernel log {kw.abort[0]}"
            else:
                try:
                    ok = link.send("imu_disable")
                    r = rd.wait_reply(REPLY_SEQ["imu_disable"], DISABLE_REPLY_TIMEOUT_S) if ok else "write failed"
                except Exception as e:  # noqa: BLE001
                    r = f"error: {e!r}"
            replies["imu_disable"] = r
            try_log(w, K_EVENT, {"event": "reply", "name": "imu_disable", "reply": r})
            if not (isinstance(r, dict) and r.get("status") == 0) and rc == 0:
                result, rc = f"imu_disable not acknowledged: {r!r}", 4
        # 6. Drain BEFORE closing syncboss0: stream stop clears unread fifo data.
        if rd is not None:
            try:
                rd.drain(0)
            except Exception:  # noqa: BLE001
                pass
        if mcu_fd is not None:
            try_log(w, K_EVENT, {"event": "mcu_close_begin"})
            os.close(mcu_fd)
            try_log(w, K_EVENT, {"event": "mcu_close_done"})
            end = time.monotonic() + CLOSE_LOG_WAIT_S
            while kw.stopped is None and time.monotonic() < end:
                kw.poll()
                time.sleep(0.05)
            kw.poll()
        if rd is not None:
            try:
                rd.drain(100)
            except Exception:  # noqa: BLE001
                pass
        for fd in stream_fds:
            os.close(fd)
        for s, h in old_handlers.items():
            signal.signal(s, h)
        enable_ok = isinstance(replies.get("imu_enable"), dict) and replies["imu_enable"].get("status") == 0
        disable_ok = isinstance(replies.get("imu_disable"), dict) and replies["imu_disable"].get("status") == 0
        outcome = decide_outcome(open_errno, rc, kw, enable_ok, disable_ok) if budget.slot else "no_open"
        try:
            budget.finish(outcome, result)
        except Exception as e:  # noqa: BLE001 - a missing outcome already latches the budget
            result += f"; budget write failed: {e!r}"
        try_log(w, K_EVENT, {"event": "end", "result": result, "outcome": outcome,
                             "kmsg_codes": kw.codes, "kernel_stats": kw.stopped})
        kw.close()
        w.close()
        say(f"closed; tx_sent={link.sent if link else []}; outcome={outcome}; result: {result}")

    res = summarise(cap_path, quiet=True, imu_csv=os.path.join(a.out, "imu.csv"))
    imu = res["imu"]
    stream_imu = res["by_type_brief"].get(f"stream:{IMU_TYPE}", {})
    summary = {"label": a.label, "capture": cap_path, "result": result, "rc": rc,
               "outcome": outcome, "open_slot": budget.slot, "boot_id": boot_id,
               "stop": stop["why"],
               "elapsed_s": round(time.monotonic() - t0, 3),
               "recorded_s": round(time.monotonic() - t_rec, 3) if t_rec else 0,
               "tx_sent": link.sent if link else [], "tx_result": link.result if link else {},
               "replies": replies,
               "reads": rd.stats["reads"] if rd else {}, "bytes": rd.stats["bytes"] if rd else {},
               "empty_reads": rd.stats["empty"] if rd else 0,
               "read_errors": rd.stats["errors"] if rd else [],
               "kernel": {"stopped": kw.stopped, "awake_ms": kw.awake_ms, "asleep_ms": kw.asleep_ms,
                          "sent": kw.sent, "codes": kw.codes, "reader_full_lines": kw.reader_full,
                          "kmsg_overruns": kw.overruns, "lines": [t for _, t in kw.lines[:60]]},
               "packets": res["by_type_brief"], "imu": imu,
               "n_framing_errors": res["n_framing_errors"]}
    with _wopen(os.path.join(a.out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=1, sort_keys=True)
    ks = kw.stopped or {}
    counters = {
        "result": result, "outcome": outcome, "rc": rc, "open_slot": budget.slot,
        "tx_sent": link.sent if link else [],
        "reply_enable_status": _status(replies.get("imu_enable")),
        "reply_disable_status": _status(replies.get("imu_disable")),
        "packets_total": sum(v["n"] for v in res["by_type_brief"].values()),
        "packets_by_type": [f"{k}:{v['n']}" for k, v in res["by_type_brief"].items()],
        "imu_packets": imu["n"], "imu_rate_hz": stream_imu.get("rate_hz"),
        "accel_norm_g_median": imu["accel_norm_g_median"],
        "gyro_norm_dps_median": imu["gyro_norm_dps_median"],
        "framing_errors": res["n_framing_errors"],
        "k_transactions": ks.get("transactions"), "k_busy": ks.get("busy"),
        "k_bad_magic": ks.get("bad_magic"), "k_bad_checksum": ks.get("bad_checksum"),
        "k_spi_errors": ks.get("spi_errors"), "k_overflows": ks.get("overflows"),
        "k_commands": ks.get("commands"), "k_failed": ks.get("failed"),
        "k_sent": kw.sent, "mcu_awake_ms": kw.awake_ms, "mcu_asleep_ms": kw.asleep_ms,
        "kmsg_codes": [c for c, _ in kw.codes], "reader_full_lines": kw.reader_full,
        "elapsed_s": summary["elapsed_s"], "recorded_s": summary["recorded_s"],
    }
    write_counters(os.path.join(a.out, "counters.txt"), counters)
    print(json.dumps(counters, sort_keys=True))
    return rc


# ---- parsing (host or device; pure) ------------------------------------------------

def iter_records(path):
    with open(path, "rb") as f:
        if f.read(8) != MAGIC:
            raise ValueError("not an sbcap file")
        (n,) = struct.unpack("<I", f.read(4))
        meta = json.loads(f.read(n))
        yield ("META_FILE", meta)
        while True:
            h = f.read(REC.size)
            if not h:
                return
            if len(h) < REC.size:
                yield ("TRUNC", len(h))
                return
            kind, src, _, ln, tm, tb = REC.unpack(h)
            p = f.read(ln)
            if len(p) < ln:
                yield ("TRUNC", len(p))
                return
            yield (kind, src, tm, tb, p)


def split_packets(buf):
    """Yield (offset, hdr_tuple, type, seq, data) or ('error', offset, reason)."""
    off = 0
    while off < len(buf):
        if len(buf) - off < UAPI_HDR.size + 3:
            yield ("error", off, "short tail %d bytes" % (len(buf) - off))
            return
        hver, hlen, from_drv, nstat, noff = UAPI_HDR.unpack_from(buf, off)
        if hlen < UAPI_HDR.size or off + hlen + 3 > len(buf):
            yield ("error", off, "bad header_length %d (version %d)" % (hlen, hver))
            return
        typ, seq, dlen = buf[off + hlen], buf[off + hlen + 1], buf[off + hlen + 2]
        end = off + hlen + 3 + dlen
        if end > len(buf):
            yield ("error", off, "data_len %d overruns read" % dlen)
            return
        yield (off, (hver, hlen, from_drv, nstat, noff), typ, seq, bytes(buf[off + hlen + 3:end]))
        off = end


def decode_imu(data):
    """Type 0x50 payload -> dict, or None if shorter than 36 bytes."""
    if len(data) < IMU_V.size:
        return None
    w0, ax, ay, az, gx, gy, gz, aux = IMU_V.unpack_from(data, 0)
    return {"imu_id": w0 & 0xF, "w0_bits4_15": (w0 >> 4) & 0xFFF, "mcu_ts": w0 >> 16,
            "accel_g": (ax, ay, az), "gyro_dps": (gx, gy, gz), "aux": aux,
            "extra": data[IMU_V.size:]}


def _median(xs):
    s = sorted(xs)
    return s[len(s) // 2] if s else None


class ImuStats:
    def __init__(self, csv_path=None):
        self.f = _wopen(csv_path, "w") if csv_path else None
        if self.f:
            self.f.write(IMU_CSV_HEADER)
        self.n = self.short = self.nonmono = self.dups = 0
        self.lens, self.ids, self.anorm, self.gnorm, self.dts = {}, {}, [], [], []
        self.last_ts, self.t_first, self.t_last = {}, None, None

    def add(self, tm, tb, ridx, seq, hdr, data):
        self.lens[len(data)] = self.lens.get(len(data), 0) + 1
        d = decode_imu(data)
        if d is None:
            self.short += 1
            return
        self.n += 1
        self.t_first = tm if self.t_first is None else self.t_first
        self.t_last = tm
        i = d["imu_id"]
        self.ids[i] = self.ids.get(i, 0) + 1
        last = self.last_ts.get(i)
        if last is not None:
            if d["mcu_ts"] == last:
                self.dups += 1
            elif d["mcu_ts"] < last:
                self.nonmono += 1
            elif len(self.dts) < 100000:
                self.dts.append(d["mcu_ts"] - last)
        self.last_ts[i] = d["mcu_ts"]
        if len(self.anorm) < 100000:
            self.anorm.append(sum(x * x for x in d["accel_g"]) ** 0.5)
            self.gnorm.append(sum(x * x for x in d["gyro_dps"]) ** 0.5)
        if self.f:
            a, g = d["accel_g"], d["gyro_dps"]
            self.f.write(f"{tm},{tb},{ridx},{seq},{hdr[3]},{hdr[4]},{len(data)},{i},"
                         f"{d['w0_bits4_15']},{d['mcu_ts']},{a[0]:.6g},{a[1]:.6g},{a[2]:.6g},"
                         f"{g[0]:.6g},{g[1]:.6g},{g[2]:.6g},{d['aux']},{d['extra'].hex()}\n")

    def result(self):
        if self.f:
            self.f.close()
        span = ((self.t_last or 0) - (self.t_first or 0)) / 1e9
        r = lambda x: None if x is None else round(x, 4)  # noqa: E731
        return {"n": self.n, "too_short": self.short, "data_lens": self.lens, "imu_ids": self.ids,
                "host_rate_hz": round((self.n - 1) / span, 2) if span > 0 else None,
                "mcu_ts_delta_median": _median(self.dts), "mcu_ts_nonmonotonic": self.nonmono,
                "mcu_ts_duplicates": self.dups, "accel_norm_g_median": r(_median(self.anorm)),
                "gyro_norm_dps_median": r(_median(self.gnorm))}


def summarise(path, dump=0, csv_out=None, quiet=False, imu_csv=None):
    by_type = {}
    marks, metas, events, errors, txs = [], [], [], [], []
    meta_file = None
    imu = ImuStats(imu_csv)
    csvf = _wopen(csv_out, "w") if csv_out else None
    if csvf:
        csvf.write("t_mono_ns,t_boot_ns,src,read_idx,type,seq,data_len,from_driver,nsync_status,nsync_offset_us,data_hex\n")
    ridx = 0
    for r in iter_records(path):
        if r[0] == "META_FILE":
            meta_file = r[1]
            continue
        if r[0] == "TRUNC":
            errors.append("truncated file")
            break
        kind, src, tm, tb, p = r
        if kind == K_MARK:
            marks.append((tm, p.decode(errors="replace")))
        elif kind == K_META:
            metas.append((tm, json.loads(p)))
        elif kind == K_EVENT:
            events.append((tm, json.loads(p)))
        elif kind == K_TX:
            txs.append((tm, p.hex()))
        elif kind == K_DATA:
            ridx += 1
            for item in split_packets(p):
                if item[0] == "error":
                    errors.append(f"read {ridx} src {src} off {item[1]}: {item[2]}")
                    continue
                off, hdr, typ, seq, data = item
                key = (SRC_NAMES.get(src, src), typ)
                e = by_type.setdefault(key, {"n": 0, "lens": {}, "seqs": set(), "t0": tm,
                                             "t1": tm, "nsync": {}, "from_driver": 0, "ex": []})
                e["n"] += 1
                e["lens"][len(data)] = e["lens"].get(len(data), 0) + 1
                e["seqs"].add(seq)
                e["t1"] = tm
                e["nsync"][hdr[3]] = e["nsync"].get(hdr[3], 0) + 1
                e["from_driver"] += hdr[2]
                if len(e["ex"]) < dump:
                    e["ex"].append((tm, seq, data.hex()))
                if src == SRC_STREAM and typ == IMU_TYPE:
                    imu.add(tm, tb, ridx, seq, hdr, data)
                if csvf:
                    csvf.write(f"{tm},{tb},{src},{ridx},{typ},{seq},{len(data)},{hdr[2]},{hdr[3]},{hdr[4]},{data.hex()}\n")
    if csvf:
        csvf.close()
    brief = {}
    for (src, typ), e in sorted(by_type.items()):
        span = (e["t1"] - e["t0"]) / 1e9
        brief[f"{src}:{typ}"] = {"n": e["n"], "data_lens": sorted(e["lens"]),
                                 "seqs": sorted(e["seqs"])[:8],
                                 "rate_hz": round((e["n"] - 1) / span, 2) if span > 0 else None}
    res = {"meta": meta_file, "by_type_brief": brief, "framing_errors": errors[:20],
           "n_framing_errors": len(errors), "markers": len(marks), "tx": [h for _, h in txs],
           "imu": imu.result(), "events": [ev for _, ev in events]}
    if quiet:
        return res
    print(f"file: {path}")
    print("meta:", json.dumps(meta_file, sort_keys=True))
    for tm, h in txs:
        print(f"  TX     {tm/1e9:14.6f}  syncboss0 <- {h}")
    for tm, ev in events:
        print(f"  event  {tm/1e9:14.6f}  {json.dumps(ev, sort_keys=True)}")
    for tm, m in metas:
        print(f"  meta   {tm/1e9:14.6f}  {m.get('what')}: {json.dumps(m.get('attrs'), sort_keys=True)}")
    for tm, m in marks:
        print(f"  mark   {tm/1e9:14.6f}  {m}")
    print(f"packets by (source, type): {len(by_type)} kinds")
    for (src, typ), e in sorted(by_type.items()):
        span = (e["t1"] - e["t0"]) / 1e9
        rate = f"{(e['n'] - 1) / span:9.2f} Hz" if span > 0 else "        - "
        print(f"  {src:7s} type {typ:3d} (0x{typ:02x}): n={e['n']:7d} {rate} "
              f"lens={dict(sorted(e['lens'].items()))} seqs={sorted(e['seqs'])[:8]} "
              f"nsync={e['nsync']} from_driver={e['from_driver']}")
        for tm, seq, hx in e["ex"]:
            print(f"      {tm/1e9:14.6f} seq={seq:3d} {hx}")
    print("imu (type 0x50):", json.dumps(res["imu"], sort_keys=True))
    if errors:
        print(f"framing errors: {len(errors)}")
        for x in errors[:20]:
            print("  " + x)
    return res


def cmd_parse(a):
    summarise(a.file, dump=a.dump, csv_out=a.csv, imu_csv=a.imu_csv)
    return 0


def cmd_cues(a):
    if a.name not in SCHEDULES:
        sys.exit(f"unknown schedule {a.name!r}; have {', '.join(SCHEDULES)}")
    sched = SCHEDULES[a.name]
    print(f"Schedule '{a.name}'. Reference pose 'level': hold the headset as if worn, visor "
          "pointing away from you, top up. Move smoothly; keep the strap clear.")
    input("Press Enter when ready... ")
    t0 = time.monotonic()
    for i, (off, label) in enumerate(sched):
        while time.monotonic() - t0 < off:
            time.sleep(0.05)
        nxt = sched[i + 1][0] - off if i + 1 < len(sched) else 0
        print(f"[{off:4d}s] NOW: {label.replace('_', ' ')}" + (f"  (for {nxt}s)" if nxt else ""),
              flush=True)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="op", required=True)
    c = sub.add_parser("capture")
    c.add_argument("--out", required=True, help="output directory (tmpfs)")
    c.add_argument("--duration", type=float, required=True,
                   help=f"seconds after the enable reply (1..{MAX_DURATION})")
    c.add_argument("--label", default="")
    c.add_argument("--schedule", default=None, choices=sorted(SCHEDULES))
    c.add_argument("--marker-file", default="/tmp/sbmark",
                   help="if this file appears, its text is recorded as a marker and it is removed")
    p = sub.add_parser("parse")
    p.add_argument("file")
    p.add_argument("--dump", type=int, default=3, help="hex examples per type")
    p.add_argument("--csv", default=None, help="every packet, raw hex")
    p.add_argument("--imu-csv", default=None, help="decoded type 0x50")
    q = sub.add_parser("cues")
    q.add_argument("name", choices=sorted(SCHEDULES))
    a = ap.parse_args()
    return {"capture": cmd_capture, "parse": cmd_parse, "cues": cmd_cues}[a.op](a)


if __name__ == "__main__":
    sys.exit(main())
