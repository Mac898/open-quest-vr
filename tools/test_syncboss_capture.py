#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Offline tests for tools/syncboss_capture.py.

A fake quest2-syncboss driver (FIFOs for /dev/syncboss0, stream0 and control0, a regular file
for /dev/kmsg, fake boot_id and identity_dma files, all under a temporary directory) replays
synthetic IMU records generated here at ~987 Hz, answers the two approved writes like the MCU
(0x7d status 0) and logs the driver's kernel lines. It checks:
  * the write allowlist (only 6e 01 01 00 / 6f 02 01 00 ever reach the fake, in that order);
  * the per-boot open budget (<= 2 opens, one wake-timeout retry, latch after other failures);
  * the kernel-log abort lines (stop, no further writes, close, latch);
  * counters (packets, rate, bad magic/checksum, accel norm) against the replayed frames;
  * an AST check of the tool, and the tools/dev command lines used to run it (denylist + line length).
Nothing here touches the network or a device; it refuses to run where /dev/syncboss0 exists.
Run: python3 -I tools/test_syncboss_capture.py
"""
import argparse
import ast
import errno
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import shutil
import signal
import socket
import stat
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE_REAL = [None]      # set by main(): the temporary directory all fake devices live in
TOOL = os.path.join(HERE, "syncboss_capture.py")
BOOT_ID_A = "0f1e2d3c-4b5a-4968-8776-655443322110"
BOOT_ID_B = "11111111-2222-4333-8444-555555555555"


def _no_network(*a, **k):
    raise AssertionError("test tried to open a network connection")


socket.create_connection = _no_network
socket.socket.connect = _no_network


def load_tool():
    spec = importlib.util.spec_from_file_location("sbcap", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_dev():
    loader = importlib.machinery.SourceFileLoader("dev", os.path.join(HERE, "dev"))
    spec = importlib.util.spec_from_loader("dev", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


sb = load_tool()
EN, DIS = sb.APPROVED_HEX
FAILS = []


def check(name, cond, info=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f"  {str(info)[:300]}"), flush=True)
    if not cond:
        FAILS.append(name)


# ---- replay source ---------------------------------------------------------------------

def synthetic_replay(max_s, rate=987.0):
    out, n = [], int(max_s * rate)
    for i in range(n):
        w0 = ((1000 + 2 * i) << 16) | (i % 8)
        data = sb.IMU_V.pack(w0, 0.0, 0.6, 0.8, 0.5, -0.5, 0.0, i)
        out.append((i / rate, sb.UAPI_HDR.pack(2, 12, 0, 0, 0) + bytes([sb.IMU_TYPE, 0, len(data)]) + data))
    return out


# ---- fake driver -----------------------------------------------------------------------

def rec(typ, seq, data):
    return sb.UAPI_HDR.pack(2, 12, 0, 0, 0) + bytes([typ, seq, len(data)]) + data


class FakeDriver(threading.Thread):
    """Mainline quest2-syncboss as seen from userspace. Only touches FIFOs/files under the
    temporary root it is given (checked)."""

    def __init__(self, root, frames, mode="normal", inject_after=150, mcu=True):
        super().__init__(daemon=True)
        self.root, self.frames, self.mode, self.inject_after, self.mcu = root, frames, mode, inject_after, mcu
        self.rx = []                 # every 4-byte chunk written to syncboss0, hex
        self.sent_imu = []           # 0x50 records actually delivered to the stream reader
        self.dropped = 0
        self.done = threading.Event()
        self.kseq = 1000
        for p in (sb.DEV_MCU, sb.DEV_STREAM, sb.DEV_CONTROL):
            full = root + p
            if not (stat.S_ISFIFO(os.stat(full).st_mode) and full.startswith(BASE_REAL[0]) and "/sbcap-test-" in full):
                raise SystemExit(f"fake: {full} is not our temporary FIFO")

    def kmsg(self, text, tag=True):
        self.kseq += 1
        line = f"6,{self.kseq},{int(time.monotonic() * 1e6)},-;" + (f"quest2-syncboss spi19.0: {text}" if tag else text)
        with open(self.root + sb.DEV_KMSG, "a") as f:
            f.write(line + "\n SUBSYSTEM=spi\n DEVICE=+spi:spi19.0\n")

    def run(self):
        ws = os.open(self.root + sb.DEV_STREAM, os.O_WRONLY)
        wc = os.open(self.root + sb.DEV_CONTROL, os.O_WRONLY)
        os.set_blocking(ws, False)
        if not self.mcu:
            self.done.wait(30)
            os.close(ws)
            os.close(wc)
            return
        rm = os.open(self.root + sb.DEV_MCU, os.O_RDONLY)
        self.kmsg("MCU awake after 87 ms")
        self.kmsg("streaming started")
        self.kmsg(f"syncboss opened by python3 ({os.getpid()}), client 1")
        import select
        replaying, t_start, idx, injected, cmds, buf = False, 0.0, 0, False, 0, b""
        while True:
            r, _, _ = select.select([rm], [], [], 0.002)
            if r:
                b = os.read(rm, 64)
                if not b:
                    break
                buf += b
                while len(buf) >= 4:
                    chunk, buf = buf[:4], buf[4:]
                    hx = chunk.hex()
                    self.rx.append(hx)
                    if hx not in (EN, DIS):        # the kernel would refuse (-EPERM)
                        self.kmsg(f"refused write {' '.join(hx[i:i + 2] for i in range(0, 8, 2))} from python3")
                        continue
                    if self.mode == "no_reply" and hx == EN:
                        continue
                    cmds += 1
                    self.kmsg("sent command " + " ".join(hx[i:i + 2] for i in range(0, 8, 2)))
                    st = 0x102 if (self.mode == "bad_status" and hx == EN) else 0
                    os.write(wc, rec(sb.REPLY_TYPE, chunk[1], st.to_bytes(4, "little")))
                    if hx == EN and not st:
                        replaying, t_start = True, time.monotonic()
                    if hx == DIS:
                        replaying = False
            if replaying:
                el = time.monotonic() - t_start
                while idx < len(self.frames) and self.frames[idx][0] <= el:
                    try:
                        os.write(ws, self.frames[idx][1])
                        if self.frames[idx][1][12] == sb.IMU_TYPE:
                            self.sent_imu.append(self.frames[idx][1])
                    except BlockingIOError:
                        self.dropped += 1
                    idx += 1
                if not injected and idx >= self.inject_after:
                    injected = True
                    self.inject()
        # last close of syncboss0: SHUTDOWN, sleep wait, reset (sb_stream_stop)
        failed = self.mode in ("a5_consec", "a5_storm")
        if failed:
            self.kmsg("stream failed, skipping SHUTDOWN")
        elif self.mode == "a6":
            cmds += 1
            self.kmsg("sent command 5a 00 00")
            self.kmsg("MCU did not go to sleep within 5000 ms")
        else:
            cmds += 1
            self.kmsg("sent command 5a 00 00")
            self.kmsg("MCU asleep after 115 ms")
        bad = 1 if self.mode == "one_bad_frame" else 0
        self.kmsg(f"streaming stopped: {12000 + idx} transactions, 3 busy, {bad} bad magic, 0 bad checksum, "
                  f"0 spi errors, 0 overflows, {cmds} commands" + (" (failed)" if failed else ""))
        time.sleep(0.05)
        for fd in (ws, wc, rm):
            os.close(fd)

    def inject(self):
        m = self.mode
        if m == "signal":
            os.kill(os.getpid(), signal.SIGTERM)
        elif m == "a5_refused":
            self.kmsg("refused write 77 01 01 00 from python3")
        elif m == "a5_consec":
            self.kmsg("2001 consecutive failed transactions, stopping stream")
        elif m == "a5_storm":
            self.kmsg("ready IRQ storm, stopping stream")
        elif m == "a5_drop":
            self.kmsg("dropping command 1 after 20 attempts")
        elif m == "a5_overflow":
            self.kmsg("data packet overflow")
        elif m == "a5_badframe":
            for _ in range(sb.REPEAT_LIMIT):
                self.kmsg("bad frame from MCU: -74 (magic 0x00000000)")
        elif m == "a5_spi":
            for _ in range(sb.REPEAT_LIMIT):
                self.kmsg("spi_sync_locked: -110")
        elif m == "one_bad_frame":
            self.kmsg("bad frame from MCU: -5 (magic 0xdeadbeef)")
        elif m == "a5_unexpected_sent":
            self.kmsg("sent command 77 00 00 00")
        elif m == "a2_oops":
            self.kmsg("Unable to handle kernel paging request at virtual address 000000000000dead", tag=False)
        elif m == "a2_warn":
            self.kmsg("WARNING: CPU: 3 PID: 77 at drivers/spi/spi.c:1234 spi_map_buf+0x1/0x2", tag=False)


# ---- harness -----------------------------------------------------------------------------

class Env:
    def __init__(self, base, boot_id=BOOT_ID_A, arm="Y"):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="sbcap-test-", dir=base))
        for d in ("/dev", "/proc/sys/kernel/random", "/sys/module/quest2_syncboss/parameters", "/tmp"):
            os.makedirs(self.root + d)
        for p in (sb.DEV_MCU, sb.DEV_STREAM, sb.DEV_CONTROL):
            os.mkfifo(self.root + p)
        open(self.root + sb.DEV_KMSG, "w").close()
        self.set_boot_id(boot_id)
        with open(self.root + sb.ARM_PARAM, "w") as f:
            f.write(arm + "\n")
        self.n = 0

    def set_boot_id(self, b):
        with open(self.root + sb.BOOT_ID, "w") as f:
            f.write(b + "\n")

    def state(self, suffix=""):
        d = self.root + sb.STATE_DIR
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def run(self, frames=None, mode="normal", duration=1.0, schedule="still", seam=None,
            fake=True, inject_after=150):
        """One capture. seam: replace _open_mcu (A3/A4: the open fails; fake runs readers-only)."""
        self.n += 1
        out = os.path.join(self.root, f"out{self.n}")
        ns = argparse.Namespace(out=out, duration=duration, label=f"{mode}{self.n}",
                                schedule=schedule, marker_file=os.path.join(self.root, "mark"))
        with open(ns.marker_file, "w") as f:
            f.write("go")
        fk = None
        devs = (sb.DEV_MCU, sb.DEV_STREAM, sb.DEV_CONTROL)
        if not fake:
            # Runs expected to refuse: regular files instead of FIFOs, so a tool that opens
            # anyway fails the test (opens/bytes counted) instead of blocking it.
            for d in devs:
                os.rename(self.root + d, self.root + d + ".fifo")
                open(self.root + d, "w").close()
        if fake:
            fk = FakeDriver(self.root, frames if frames is not None else synthetic_replay(duration + 1),
                            mode, inject_after, mcu=seam is None)
            fk.start()
        real_open = sb._open_mcu
        calls = []

        def counting_open(p):
            calls.append(p)
            if seam is not None:
                return seam(self, fk, p)
            return real_open(p)
        sb._open_mcu = counting_open
        t0 = time.monotonic()
        try:
            rc = sb.cmd_capture(ns, root=self.root)
        except SystemExit as e:
            rc = ("exit", str(e))
        finally:
            sb._open_mcu = real_open
        el = time.monotonic() - t0
        trapped = 0
        if not fake:
            for d in devs:
                trapped += os.path.getsize(self.root + d)
                os.unlink(self.root + d)
                os.rename(self.root + d + ".fifo", self.root + d)
        if fk is not None:
            fk.done.set()
            fk.join(10)
        summ, counters = None, None
        if os.path.exists(os.path.join(out, "summary.json")):
            with open(os.path.join(out, "summary.json")) as f:
                summ = json.load(f)
            with open(os.path.join(out, "counters.txt")) as f:
                counters = dict(line.rstrip("\n").split("=", 1) for line in f if "=" in line)
        return types_ns(rc=rc, fake=fk, summ=summ, counters=counters, opens=len(calls) + trapped, elapsed=el, out=out)

    def cleanup(self):
        shutil.rmtree(self.root)


def types_ns(**kw):
    return argparse.Namespace(**kw)


def seam_a4(env, fk, p):
    fk.kmsg("MCU not awake within 5000 ms")
    raise OSError(errno.ETIMEDOUT, "Connection timed out")


def seam_a3(env, fk, p):
    fk.kmsg("frames use SE DMA but identity_dma is not set; refusing")
    raise OSError(errno.EPERM, "Operation not permitted")


def outcome_of(env, n):
    p = os.path.join(env.root + sb.STATE_DIR, f"{BOOT_ID_A}.open{n}.outcome")
    try:
        with open(p) as f:
            return json.load(f)["outcome"]
    except OSError:
        return None


def latched(env, boot_id=BOOT_ID_A):
    return os.path.exists(os.path.join(env.root + sb.STATE_DIR, f"{boot_id}.stop"))


# ---- tests ---------------------------------------------------------------------------------

def test_replay(base):
    """A synthetic 987 Hz IMU stream through one full capture."""
    dur, rate = 2.0, 987.0
    frames = synthetic_replay(dur + 1.0, rate)
    env = Env(base)
    r = env.run(frames, "normal", duration=dur, schedule="pitch")
    c, fk = r.counters or {}, r.fake
    exp_imu = len(fk.sent_imu)
    norms = sorted(sum(x * x for x in sb.decode_imu(b[15:])["accel_g"]) ** 0.5 for b in fk.sent_imu)
    exp_norm = round(norms[len(norms) // 2], 4) if norms else None
    check("replay: rc 0, outcome ok", r.rc == 0 and c.get("outcome") == "ok", (r.rc, c.get("result")))
    check("replay: MCU got exactly enable, disable", fk.rx == [EN, DIS], fk.rx)
    check("replay: TX records == approved",
          sb.summarise(os.path.join(r.out, "capture.sbcap"), quiet=True)["tx"] == [EN, DIS])
    check(f"replay: 0x50 count == replayed ({exp_imu}), none dropped",
          int(c.get("imu_packets", -1)) == exp_imu and exp_imu > 500 and fk.dropped == 0,
          (c.get("imu_packets"), exp_imu, fk.dropped))
    check(f"replay: accel norm median {exp_norm} g", c.get("accel_norm_g_median") == str(exp_norm)
          and 0.9 < float(c.get("accel_norm_g_median") or 0) < 1.1, (c.get("accel_norm_g_median"), exp_norm))
    check(f"replay: 0x50 rate {c.get('imu_rate_hz')} Hz ~ {rate:.0f}",
          abs(float(c.get("imu_rate_hz") or 0) - rate) < 0.15 * rate, c.get("imu_rate_hz"))
    check("replay: kernel counters parsed, 0 bad magic/checksum",
          c.get("k_bad_magic") == "0" and c.get("k_bad_checksum") == "0" and c.get("k_failed") == "False"
          and int(c.get("k_transactions", 0)) > 0, c)
    check("replay: kernel sent enable, disable, SHUTDOWN; awake 87 ms, asleep 115 ms",
          c.get("k_sent") == f"{EN},{DIS},5a0000" and c.get("mcu_awake_ms") == "87"
          and c.get("mcu_asleep_ms") == "115", c)
    check("replay: replies 0/0, no framing errors, budget slot 1",
          c.get("reply_enable_status") == "0" and c.get("reply_disable_status") == "0"
          and c.get("framing_errors") == "0" and c.get("open_slot") == "1", c)
    check("replay: imu.csv rows == 0x50 count",
          sum(1 for _ in open(os.path.join(r.out, "imu.csv"))) == exp_imu + 1)
    check("replay: marker consumed and recorded", not os.path.exists(os.path.join(env.root, "mark")))
    env.cleanup()


def test_budget(base):
    # B1: ok, ok, then refused before anything is opened.
    env = Env(base)
    r1 = env.run(duration=1)
    r2 = env.run(duration=1)
    check("budget: open 1 ok, open 2 ok", r1.rc == 0 and r2.rc == 0 and outcome_of(env, 1) == "ok"
          and outcome_of(env, 2) == "ok", (r1.rc, r2.rc))
    r3 = env.run(duration=1, fake=False)
    check("budget: third open refused before opening anything", isinstance(r3.rc, tuple)
          and "no further opens" in r3.rc[1] and r3.opens == 0 and not os.path.exists(r3.out), r3.rc)
    check("budget: no open3 slot file, latch present", not any("open3" in x for x in env.state()) and latched(env), env.state())
    env.cleanup()
    # B2: A4 then the one retry, then refused.
    env = Env(base)
    r1 = env.run(seam=seam_a4, duration=1)
    check("A4: open fails ETIMEDOUT, nothing written, outcome a4_wake_timeout",
          r1.rc == 2 and r1.fake.rx == [] and outcome_of(env, 1) == "a4_wake_timeout" and not latched(env)
          and "A4" in r1.counters["kmsg_codes"], (r1.rc, outcome_of(env, 1), r1.counters))
    r2 = env.run(duration=1)
    check("A4: one retry open allowed and ok", r2.rc == 0 and r2.opens == 1 and outcome_of(env, 2) == "ok", r2.rc)
    r3 = env.run(duration=1, fake=False)
    check("A4: no third open", isinstance(r3.rc, tuple) and r3.opens == 0, r3.rc)
    env.cleanup()
    # B3: A4 twice, then refused.
    env = Env(base)
    env.run(seam=seam_a4, duration=1)
    r2 = env.run(seam=seam_a4, duration=1)
    r3 = env.run(duration=1, fake=False)
    check("A4 twice: second is the retry, third refused", r2.opens == 1 and isinstance(r3.rc, tuple)
          and r3.opens == 0 and latched(env), (r2.opens, r3.rc))
    env.cleanup()
    # B4: A3 (-EPERM on open) latches.
    env = Env(base)
    r1 = env.run(seam=seam_a3, duration=1)
    r2 = env.run(duration=1, fake=False)
    check("A3: -EPERM open -> a3_open_refused, latched, no retry", r1.rc == 2 and outcome_of(env, 1) == "a3_open_refused"
          and latched(env) and isinstance(r2.rc, tuple) and r2.opens == 0, (r1.rc, outcome_of(env, 1), r2.rc))
    env.cleanup()
    # B5: killed process (slot without outcome) -> refused and latched.
    env = Env(base)
    os.makedirs(env.root + sb.STATE_DIR)
    with open(os.path.join(env.root + sb.STATE_DIR, f"{BOOT_ID_A}.open1"), "w") as f:
        f.write("{}\n")
    r = env.run(duration=1, fake=False)
    check("killed run (slot, no outcome): refused, latched", isinstance(r.rc, tuple) and r.opens == 0
          and "no outcome" in r.rc[1] and latched(env), r.rc)
    # B6: a new boot (other boot_id) starts a fresh budget.
    env.set_boot_id(BOOT_ID_B)
    r = env.run(duration=1)
    check("new boot_id: fresh budget, open allowed", r.rc == 0 and r.summ["open_slot"] == 1, r.rc)
    env.cleanup()
    # B7: unreadable / malformed boot_id, identity_dma not Y: refused, nothing created.
    for bid in ("", "not-a-uuid", BOOT_ID_A.upper()):
        env = Env(base, boot_id=bid)
        r = env.run(duration=1, fake=False)
        check(f"boot_id {bid!r}: refused, nothing opened or created", isinstance(r.rc, tuple) and r.opens == 0
              and env.state() == [] and not os.path.exists(r.out), r.rc)
        env.cleanup()
    for arm in ("N", "0", ""):
        env = Env(base, arm=arm)
        r = env.run(duration=1, fake=False)
        check(f"identity_dma {arm!r}: refused (A3), nothing created", isinstance(r.rc, tuple) and "identity_dma" in r.rc[1]
              and r.opens == 0 and env.state() == [], r.rc)
        env.cleanup()
    # B8: missing device nodes: refused.
    env = Env(base)
    os.unlink(env.root + sb.DEV_KMSG)
    r = env.run(duration=1, fake=False)
    check("no /dev/kmsg: refused before opening", isinstance(r.rc, tuple) and r.opens == 0, r.rc)
    env.cleanup()
    # B9: OpenBudget.reserve races (slot appears between check and create) -> refused + latch.
    env = Env(base)
    b = sb.OpenBudget(env.root + sb.STATE_DIR, BOOT_ID_A)
    real_check = b.check

    def racing_check():
        n = real_check()
        os.makedirs(b.dir, exist_ok=True)
        open(b._p(f"open{n}"), "w").close()
        return n
    b.check = racing_check
    try:
        b.reserve("x")
        ok = False
    except sb.Refused:
        ok = True
    check("reserve: exclusive create refuses a raced slot and latches", ok and latched(env))
    env.cleanup()


def test_aborts(base):
    for mode in ("a5_refused", "a5_consec", "a5_storm", "a5_drop", "a5_overflow", "a5_badframe",
                 "a5_spi", "a5_unexpected_sent", "a2_oops", "a2_warn"):
        env = Env(base)
        r = env.run(mode=mode, duration=5, inject_after=200)
        c = r.counters or {}
        want = "a2_kernel_error" if mode.startswith("a2") else "a5_abort_line"
        check(f"{mode}: stops early, closes, no write after the line (MCU got enable only)",
              r.rc == 1 and r.fake.rx == [EN] and r.elapsed < 3.5 and "kernel log" in c.get("result", ""),
              (r.rc, r.fake.rx, round(r.elapsed, 2), c.get("result")))
        check(f"{mode}: outcome {want}, latched, next open refused", c.get("outcome") == want and latched(env)
              and isinstance(env.run(duration=1, fake=False).rc, tuple), (c.get("outcome"), env.state()))
        check(f"{mode}: disable reported as not sent", c.get("reply_disable_status", "").startswith("not sent"), c.get("reply_disable_status"))
        env.cleanup()
    # A6: MCU did not go to sleep -> recorded, latched (disable was sent normally before close).
    env = Env(base)
    r = env.run(mode="a6", duration=1)
    c = r.counters
    check("A6: disable sent, outcome a6_no_sleep, latched", r.fake.rx == [EN, DIS] and c["outcome"] == "a6_no_sleep"
          and latched(env) and "A6" in c["kmsg_codes"], (r.fake.rx, c["outcome"]))
    env.cleanup()
    # One isolated bad frame: no mid-capture stop, but kernel counters != 0 -> not ok, latched.
    env = Env(base)
    r = env.run(mode="one_bad_frame", duration=1, inject_after=100)
    c = r.counters
    check("one bad frame line: full capture, outcome anomaly_kernel_counters, latched",
          r.rc == 0 and r.fake.rx == [EN, DIS] and c["outcome"] == "anomaly_kernel_counters" and latched(env), (r.rc, c["outcome"]))
    env.cleanup()
    # No reply / bad status / signal / deadline: disable still sent, closed, latched.
    for mode, want_rc in (("no_reply", 2), ("bad_status", 2), ("signal", 1)):
        env = Env(base)
        r = env.run(mode=mode, duration=20 if mode == "signal" else 1, inject_after=100)
        check(f"{mode}: rc {want_rc}, disable sent, closed, latched", r.rc == want_rc and r.fake.rx == [EN, DIS]
              and latched(env) and r.elapsed < 12, (r.rc, r.fake.rx, round(r.elapsed, 1)))
        if mode == "signal":
            check("signal: handlers restored", signal.getsignal(signal.SIGTERM) == signal.SIG_DFL
                  and signal.getsignal(signal.SIGALRM) == signal.SIG_DFL)
        env.cleanup()
    env = Env(base)
    old = sb.HARD_MARGIN_S
    sb.HARD_MARGIN_S = -1.5      # alarm at int(3 - 1.5) = 1 s, before the 3 s recording ends
    try:
        r = env.run(duration=3)
    finally:
        sb.HARD_MARGIN_S = old
    check("deadline (SIGALRM): stops early, disable sent, closed", r.rc == 1 and "deadline" in r.summ["stop"]
          and r.fake.rx == [EN, DIS] and r.summ["recorded_s"] < 2.5, (r.rc, r.summ and r.summ["stop"]))
    env.cleanup()
    # Output breaks (ENOSPC) mid-capture -> abort, disable sent and acknowledged.
    env = Env(base)
    real_rec = sb.Writer.rec
    broken = {"on": False}

    def failing_rec(self, kind, src, payload, t=None):
        if broken["on"] or (kind == sb.K_DATA and src == sb.SRC_STREAM):
            broken["on"] = True
            raise OSError(28, "No space left on device (test)")
        return real_rec(self, kind, src, payload, t)
    sb.Writer.rec = failing_rec
    try:
        r = env.run(duration=3)
    finally:
        sb.Writer.rec = real_rec
    check("output broken: rc 2, disable sent and acknowledged", r.rc == 2 and r.fake.rx == [EN, DIS]
          and r.summ["replies"]["imu_disable"]["status"] == 0, (r.rc, r.fake.rx))
    env.cleanup()
    # Duration bounds (<= 60 s).
    env = Env(base)
    for d in (0, 0.5, 60.5, 61, 300):
        r = env.run(duration=d, fake=False)
        check(f"duration {d}: refused", isinstance(r.rc, tuple) and "--duration" in r.rc[1] and r.opens == 0, r.rc)
    check("MAX_DURATION is 60", sb.MAX_DURATION == 60)
    env.cleanup()


def test_link(base):
    env = Env(base)
    rfd, wfd = os.pipe()
    w = sb.Writer(os.path.join(env.root, "x.sbcap"), {})
    link = sb.McuLink(wfd, w)
    for bad in ("imu_set_rate", "mag_enable", "shutdown", EN, bytes.fromhex(EN), None, 0, ("imu_enable",)):
        try:
            link.send(bad)
            ok = False
        except (sb.Refused, TypeError):
            ok = True
        check(f"McuLink refuses {bad!r} before any write", ok and not link.sent)
    for name, want in (("imu_disable", False), ("imu_enable", True), ("imu_enable", False),
                       ("imu_disable", True), ("imu_disable", False)):
        try:
            got = link.send(name)
        except sb.Refused:
            got = False
        check(f"McuLink order: {name} -> {'sent' if want else 'refused'}", got == want)
    os.close(wfd)
    data = b""
    while True:
        b = os.read(rfd, 64)
        if not b:
            break
        data += b
    os.close(rfd)
    w.close()
    check("McuLink wrote exactly enable+disable", data.hex() == EN + DIS, data.hex())
    try:
        sb.APPROVED_TX["imu_set_rate"] = b"\x77"
        ok = False
    except TypeError:
        ok = True
    check("APPROVED_TX read-only, two entries, exact bytes", ok and len(sb.APPROVED_TX) == 2
          and sb.APPROVED_HEX == ("6e010100", "6f020100"))
    for p in ("/dev/syncboss0", "/sys/kernel/x", "/proc/sys/x", "/tmp/a/../../dev/null",
              os.path.join(env.root, "a/swd/x"), os.path.join(env.root, "control/reset")):
        try:
            sb._wopen(p, "w")
            ok = False
        except (sb.Refused, SystemExit):
            ok = True
        check(f"_wopen refuses {p if not p.startswith(env.root) else '<root>' + p[len(env.root):]}", ok)
    env.cleanup()


def static_problems(src):
    """os.write only in McuLink.send; os.open with a write/create flag only in _open_mcu;
    builtin open() with a write mode (or a non-literal mode) only in _wopen; no ioctl, pwrite,
    writev, sendfile, os.system/popen/exec*/spawn*, no fcntl/ctypes/mmap/subprocess/socket."""
    tree = ast.parse(src)
    problems = []
    banned_mods = ("fcntl", "ctypes", "mmap", "subprocess", "socket", "pty", "termios")
    banned_os = ("ioctl", "pwrite", "pwritev", "writev", "sendfile", "system", "popen", "execv",
                 "execve", "execvp", "spawnv", "fork", "truncate", "ftruncate", "copy_file_range",
                 "splice")

    class V(ast.NodeVisitor):
        def __init__(self):
            self.scope = []

        def visit_FunctionDef(self, node):
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        visit_ClassDef = visit_FunctionDef

        def where(self):
            return ".".join(self.scope) or "<module>"

        def visit_Import(self, node):
            for n in node.names:
                if n.name.split(".")[0] in banned_mods:
                    problems.append(f"imports {n.name}")

        def visit_ImportFrom(self, node):
            if (node.module or "").split(".")[0] in banned_mods or node.module == "os":
                problems.append(f"imports from {node.module}")

        def visit_Attribute(self, node):
            if isinstance(node.value, ast.Name) and node.value.id == "os":
                if node.attr in banned_os:
                    problems.append(f"os.{node.attr} in {self.where()}")
                if node.attr == "write" and self.where() != "McuLink.send":
                    problems.append(f"os.write in {self.where()}")
            self.generic_visit(node)

        def visit_Call(self, node):
            f = node.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "os" and f.attr == "open":
                names = {n.attr for a in node.args[1:] for n in ast.walk(a) if isinstance(n, ast.Attribute)}
                if names & {"O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND"} and self.where() != "_open_mcu":
                    problems.append(f"os.open with write flags in {self.where()}")
                if any(isinstance(n, ast.Constant) for a in node.args[1:] for n in ast.walk(a)):
                    problems.append(f"os.open with numeric flags in {self.where()}")
            if isinstance(f, ast.Name) and f.id == "open":
                mode = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "mode"), None)
                if mode is not None:
                    lit = mode.value if isinstance(mode, ast.Constant) and isinstance(mode.value, str) else None
                    if (lit is None or set(lit) & set("wax+")) and self.where() != "_wopen":
                        problems.append(f"open() for writing in {self.where()}")
            if isinstance(f, ast.Name) and f.id in ("eval", "exec", "__import__", "getattr", "setattr"):
                problems.append(f"{f.id}() in {self.where()}")
            self.generic_visit(node)

        def visit_Assign(self, node):
            # aliasing os.write / os.open / open defeats the scope rule
            if isinstance(node.value, ast.Attribute) and isinstance(node.value.value, ast.Name) \
                    and node.value.value.id == "os":
                problems.append(f"alias of os.{node.value.attr} in {self.where()}")
            if isinstance(node.value, ast.Name) and node.value.id == "open":
                problems.append(f"alias of open in {self.where()}")
            self.generic_visit(node)
    V().visit(tree)
    return problems


def test_static():
    with open(TOOL) as f:
        src = f.read()
    probs = static_problems(src)
    check("static: write paths only in McuLink.send/_open_mcu/_wopen, no ioctl/fcntl/socket/subprocess", not probs, probs)
    for inj in ("import fcntl\n", "from os import write\n", "def f(fd):\n    os.write(fd, b'\\x77')\n",
                "class McuLink:\n    def other(self, fd):\n        os.write(fd, b'x')\n",
                "w = os.write\n", "def g(fd):\n    os.ioctl(fd, 1)\n",
                "def h(p):\n    return os.open(p, os.O_RDWR)\n", "def h(p):\n    return os.open(p, 1)\n",
                "def h(p):\n    return open(p, 'wb')\n", "def h(p, m):\n    return open(p, m)\n",
                "def h(p):\n    return open(p, mode='a')\n", "o = open\n", "import subprocess\n",
                "def h():\n    os.system('x')\n"):
        check(f"static check flags injected {inj.splitlines()[-1].strip()!r}", bool(static_problems(inj)))
    # No Android-kernel-only interface in code: string constants outside docstrings may
    # name these only inside the FORBIDDEN tuple.
    tree = ast.parse(src)
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body
            and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    forb = {id(c) for n in ast.walk(tree) if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "FORBIDDEN" for t in n.targets)
            for c in ast.walk(n.value)}
    words = ("spi1.0", "/control/", "transaction_length", "ignore_sysfs_fallback", "firmware_config",
             "powerstate", "ttyHS", "swd", "update_firmware")
    hits = [c.value for c in ast.walk(tree) if isinstance(c, ast.Constant) and isinstance(c.value, str)
            and id(c) not in docs and id(c) not in forb and any(w in c.value for w in words)]
    check("static: no spi1.0/control, sysfs fallback, powerstate, ttyHS or SWD names in code", not hits, hits[:3])
    check("static: FORBIDDEN still lists spi1.0 and /control/", "spi1.0" in sb.FORBIDDEN and "/control/" in sb.FORBIDDEN)


# The tools/dev command lines used to run the capture on the headset ({TOOL_SHA} = sha256 of
# tools/syncboss_capture.py): every one must pass the denylist and the transport limits.
COMMANDS = [
    "mkdir -p /tmp/lvr /tmp/sbcap && wget -q -T 20 -O /tmp/lvr/syncboss_capture.py "
    "http://10.42.0.1:8001/syncboss_capture.py && sha256sum /tmp/lvr/syncboss_capture.py",
    "cat /sys/module/quest2_syncboss/parameters/identity_dma; cat /proc/sys/kernel/random/boot_id; "
    "ls -l /dev/syncboss0 /dev/syncboss_stream0 /dev/syncboss_control0 /dev/kmsg; "
    "ls -l /tmp/lvr/sb-opens 2>/dev/null; true",
    "grep -e quest2-syncboss -e 'FIFO mode disabled' /tmp/kernel.log | tail -n 20; echo END",
    "sha256sum /tmp/lvr/syncboss_capture.py | grep -q '^{TOOL_SHA} ' && (setsid chroot /stage2 /usr/bin/python3 -I "
    "/tmp/lvr/syncboss_capture.py capture --out /tmp/sbcap/S0 --duration 15 --schedule still --label S0 "
    "</dev/null >/tmp/sbcap/S0.log 2>&1 &); sleep 12; cat /tmp/sbcap/S0.log",
    "sha256sum /tmp/lvr/syncboss_capture.py | grep -q '^{TOOL_SHA} ' && (setsid chroot /stage2 /usr/bin/python3 -I "
    "/tmp/lvr/syncboss_capture.py capture --out /tmp/sbcap/S1 --duration 60 --schedule still --label S1 "
    "</dev/null >/tmp/sbcap/S1.log 2>&1 &); sleep 12; cat /tmp/sbcap/S1.log",
    "cat /tmp/sbcap/S0.log; cat /tmp/sbcap/S0/counters.txt; cat /tmp/lvr/sb-opens/*.outcome "
    "/tmp/lvr/sb-opens/*.stop 2>/dev/null; ps | grep syncboss_captur[e]; true",
    "grep -e quest2-syncboss -e 'Unable to handle' -e Oops -e 'BUG:' -e SError -e 'soft lockup' "
    "-e 'rcu.*stall' -e 'WARNING: CPU' /tmp/kernel.log | tail -n 40; echo END",
    "cd /tmp/sbcap && tar czf /tmp/sbcap-S0.tgz S0 S0.log && ls -l /tmp/sbcap-S0.tgz",
    "ps | grep syncboss_captur[e]",
    "kill -TERM 4242",
]


def test_commands(dev):
    sha = hashlib.sha256(open(TOOL, "rb").read()).hexdigest()
    n, bad = 0, []
    for raw in COMMANDS:
        c = raw.replace("{TOOL_SHA}", sha)
        n += 1
        why = dev.refusal(c) or dev.transport_refusal(c)
        if why:
            bad.append((c[:70], why))
        if "syncboss_capture.py capture" in c:
            check(f"start line passes the capture gate: {c[60:120]}...", dev.CAPTURE_START.search(c) is not None)
            for part in ("--duration ",):
                d = float(c.split(part)[1].split()[0])
                check(f"start line duration {d} <= {sb.MAX_DURATION}", 1 <= d <= sb.MAX_DURATION)
    check(f"all {n} command lines allowed by tools/dev check and < LINE_MAX", n >= 8 and not bad, bad)
    # Writes to the open budget (or its parent) are refused by tools/dev.
    for c in ("rm /tmp/lvr/sb-opens/x.open1", "rm -f /tmp/lvr/sb-opens/" + BOOT_ID_A + ".stop",
              "echo '{\"outcome\": \"ok\"}' > /tmp/lvr/sb-opens/x.open1.outcome",
              "cd /tmp/lvr/sb-opens && rm x.stop", "mv /tmp/lvr /tmp/old", "cd /tmp && mv lvr old",
              "rmdir /tmp/lvr/sb-opens", "mv /tmp/lvr/sb-opens /tmp/x", "touch /tmp/lvr/sb-opens/x",
              "cd /tmp/lvr && rm sb-opens/x", "python3 -c 'import os; os.unlink(\"/tmp/lvr/sb-opens/x\")'",
              dev.s2_wrap("rm /tmp/lvr/sb-opens/x.stop")):
        check(f"tools/dev refuses budget tampering: {c[:60]}", dev.refusal(c) is not None)
    for c in ("cat /tmp/lvr/sb-opens/*.outcome", "ls -l /tmp/lvr/sb-opens",
              "mkdir -p /tmp/lvr /tmp/sbcap && wget -q -T 20 -O /tmp/lvr/syncboss_capture.py "
              "http://10.42.0.1:8001/syncboss_capture.py && sha256sum /tmp/lvr/syncboss_capture.py"):
        check(f"tools/dev allows: {c[:60]}", dev.refusal(c) is None, dev.refusal(c))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.parse_args()
    if os.path.exists(sb.DEV_MCU):
        sys.exit("host-only test; refusing to run where /dev/syncboss0 exists")
    base = os.path.realpath(tempfile.mkdtemp(prefix="sbcap-base-"))
    BASE_REAL[0] = base + os.sep
    t0 = time.monotonic()
    try:
        test_replay(base)
        test_budget(base)
        test_aborts(base)
        test_link(base)
        test_static()
        test_commands(load_dev())
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print(f"{time.monotonic() - t0:.0f} s")
    print("SBCAP TEST", "PASS" if not FAILS else f"FAIL ({len(FAILS)}): {FAILS[:5]}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
