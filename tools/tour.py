#!/usr/bin/env python3
"""Guided tour of the newer fixes (patches 0017-0046, the patched Basalt): provokes the situations the
new code handles and checks the result from the driver status file and the Monado journal. ~10 min, spoken cues on the Rift S
speakers (laptop speakers when the headset's sink is gone, e.g. during a restart).

Steps (--steps picks some, comma separated):
  preflight  installed build, patched Basalt, CAP_SYS_NICE, power profile, headset on USB 3 (no headset needed)
  starts     3 Monado cold starts until head tracking (camera clock resync patch) + 1 stop right after
             start (Basalt safe stop: used to segfault); counts clock warnings and core dumps
  load       45 s looking around: Monado CPU and memory (patched Basalt: 4-thread cap, no growing stats)
  radio      battery pulled from the left controller while it vibrates: the right one must keep vibrating
             and the left must come back and vibrate again (radio queue: deadline, retries, error checks)
  lens       blind A / B / A of the c1 and the new "mesh" lens model, answered at the end

Restarts Monado (WayVR follows); the lens step uses a temporary drop-in (monado.service.d/90-tour.conf),
removed at the end. Report: ~/.local/share/rift-s-linux/recordings/tour-<date>/report.txt.

Usage: tour.py [--steps a,b] [--quiet]   (run in a terminal; no game running: hapticstest needs the focus)
"""
import argparse
import datetime
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from paths import BASALT_LIB as BASALT, BIN, MONADO_BIN, RECORDINGS, STATUS_FILE as STATUS, SYSTEMD_USER  # noqa: E402

DROPIN = SYSTEMD_USER / "monado.service.d/90-tour.conf"
# Monado CPU with the stock Basalt, headset still, 5 SLAM cameras (HANDOFF, measure-slam-cpu.sh)
OLD_CPU_CORES = 2.58

QUIET = False
report_lines = []


def log(text=""):
    print(text, flush=True)
    report_lines.append(text)


def say(text):
    print(">>", text, flush=True)
    if QUIET:
        return
    out = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True, text=True).stdout
    sinks = sorted({ln.split()[1] for ln in out.splitlines() if "Rift_S" in ln})
    with tempfile.NamedTemporaryFile(suffix=".wav") as wav:
        subprocess.run(["espeak-ng", "-s", "150", "-w", wav.name, text], check=False)
        cmd = [["paplay", "--device=" + s, wav.name] for s in sinks] or [["paplay", wav.name]]
        for p in [subprocess.Popen(c) for c in cmd]:
            p.wait()


def run(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except FileNotFoundError:
        return -1, ""


def status():
    try:
        with open(STATUS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def wait_for(cond, timeout, poll=0.25):
    """Poll the status file until cond(status) is true; returns (status, seconds) or (None, timeout)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = status()
        if st is not None and cond(st):
            return st, time.time() - t0
        time.sleep(poll)
    return None, timeout


def head_tracked(st):
    return bool(st.get("head", {}).get("position_tracked"))


def ctrl(st, side):
    return (st.get("controllers") or {}).get(side) or {}


def monado_pid():
    rc, out = run(["systemctl", "--user", "show", "-p", "MainPID", "--value", "monado.service"])
    try:
        return int(out.strip())
    except ValueError:
        return 0


def stop_monado():
    rc, _ = run(["systemctl", "--user", "stop", "monado.service"], timeout=15)
    if rc != 0:
        run(["systemctl", "--user", "kill", "-s", "KILL", "monado.service"])
        run(["systemctl", "--user", "stop", "monado.service"], timeout=15)
    run(["systemctl", "--user", "reset-failed", "monado.service", "wayvr.service"])
    for _ in range(20):
        if not STATUS.exists():
            break
        time.sleep(0.25)


def start_monado():
    run(["systemctl", "--user", "restart", "monado.socket"])
    run(["systemctl", "--user", "start", "monado.service"], timeout=30)


def journal_since(epoch):
    return run(["journalctl", "--user", "-u", "monado.service", "--since", f"@{int(epoch)}", "-o", "cat",
                "--no-pager"], timeout=30)[1]


def coredumps_since(epoch):
    rc, out = run(["coredumpctl", "list", "--since", f"@{int(epoch)}", "--no-pager", "-q"], timeout=30)
    return [ln for ln in out.splitlines() if "monado" in ln or "wayvr" in ln.lower()] if rc == 0 else []


def result(ok, name, detail):
    mark = {True: "PASS", False: "FAIL", None: "INFO"}[ok]
    log(f"  [{mark}] {name}: {detail}")
    return ok


# ---------------------------------------------------------------------------------------------- steps

def step_preflight():
    log("Preflight")
    binary = MONADO_BIN.read_bytes() if MONADO_BIN.exists() else b""
    result(b"RIFT_S_POSE_OFFSETS" in binary and b"RIFT_S_LENS_IPD_MM" in binary, "Monado build",
           "patches 0017-0046 installed" if binary else f"{MONADO_BIN} missing")
    rc, env = run(["systemctl", "--user", "show", "monado.service", "-p", "Environment"])
    result(str(BASALT) in env or None, "Basalt", "patched build in the service environment" if str(BASALT) in env
           else "Fedora's basalt-monado (Setup tab: Basalt builds the patched one)")
    rc, cap = run(["getcap", str(MONADO_BIN)])
    result("cap_sys_nice" in cap, "CAP_SYS_NICE", cap.strip() or "missing: sudo setcap CAP_SYS_NICE=eip " + str(MONADO_BIN))
    rc, out = run(["busctl", "--system", "get-property", "org.freedesktop.UPower.PowerProfiles",
                   "/org/freedesktop/UPower/PowerProfiles", "org.freedesktop.UPower.PowerProfiles", "ActiveProfile"])
    m = re.search(r'"(.*)"', out)
    prof = m.group(1) if m else "unknown"
    result(prof == "performance", "Power profile", prof + ("" if prof == "performance" else " (set Performance in KDE)"))
    rc, usb = run(["lsusb"])
    return result("2833:2051" in usb, "Headset on USB 3",
           "hub 2833:2051 found" if "2833:2051" in usb else
           ("only USB 2 devices, replug the USB-A plug" if "2833:" in usb else "no Rift S device (2833:*) on USB"))


def step_starts():
    log("Cold starts (camera clock resync, Basalt safe stop)")
    say("Cold start test. Put the headset on and keep it on. Monado restarts four times, about a minute.")
    t_begin = time.time()
    times = []
    for i in range(3):
        stop_monado()
        t0 = time.time()
        start_monado()
        st, dt = wait_for(head_tracked, 25)
        times.append(dt if st else None)
        log(f"    start {i + 1}: head tracking after {dt:.1f} s" if st else f"    start {i + 1}: no head tracking within 25 s")
        if i < 2:
            time.sleep(2)
    ok = all(t is not None for t in times)
    result(ok, "Head tracking on every start", ", ".join("%.1f s" % t if t else "none" for t in times))
    j = journal_since(t_begin)
    disc = len(re.findall(r"Camera clock discontinuity", j))
    back = len(re.findall(r"Camera frame time went backward", j))
    result(None, "Camera clock", f"{disc} resyncs (new patch acted), {back} 'went backward' warnings")

    say("Now a very quick stop, right after start.")
    stop_monado()
    t_quick = time.time()
    start_monado()
    time.sleep(0.8)
    stop_monado()
    time.sleep(2)
    dumps = coredumps_since(t_quick)
    result(not dumps, "Stop right after start", "no core dump" if not dumps else "core dump: " + dumps[-1])
    start_monado()
    wait_for(head_tracked, 25)


def step_load():
    log("Load (patched Basalt)")
    pid = monado_pid()
    if not pid:
        start_monado()
        wait_for(head_tracked, 25)
        pid = monado_pid()
    if not pid:
        return result(False, "Load", "Monado not running")
    say("Look around the room normally for 45 seconds. Turn your head, look up and down.")
    tick = os.sysconf("SC_CLK_TCK")

    def cpu_rss():
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        with open(f"/proc/{pid}/status") as f:
            rss = next(int(ln.split()[1]) for ln in f if ln.startswith("VmRSS"))
        return (int(fields[11]) + int(fields[12])) / tick, rss / 1024

    c0, m0 = cpu_rss()
    t0 = time.time()
    positions, untracked = [], 0
    while time.time() - t0 < 45:
        st = status()
        if st:
            if head_tracked(st):
                positions.append(st["head"]["position"])
            else:
                untracked += 1
        time.sleep(0.25)
    c1, m1 = cpu_rss()
    cores = (c1 - c0) / (time.time() - t0)
    say("Done. Thank you.")
    result(None, "Monado CPU", f"{cores:.2f} cores (stock Basalt, headset still: {OLD_CPU_CORES}; looking around costs more)")
    result(None, "Monado memory", f"{m0:.0f} -> {m1:.0f} MB over 45 s")
    if positions:
        spread = max(max(p[i] for p in positions) - min(p[i] for p in positions) for i in range(3))
        result(spread < 1.5 and untracked < 8, "Head tracking while looking around",
               f"position spread {spread:.2f} m, untracked samples {untracked}")
    else:
        result(False, "Head tracking while looking around", "never tracked")


def haptic(st, side):
    h = ctrl(st, side).get("haptic") or {}
    return h.get("writes", 0), h.get("failed", 0)


def hapticstest(side):
    return subprocess.Popen([str(BIN / "hapticstest"), side], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def step_radio():
    log("Radio (controller vanishing mid-vibration)")
    st, _ = wait_for(lambda s: ctrl(s, "left").get("imu_age_ms", 1e9) < 200 and ctrl(s, "right").get("imu_age_ms", 1e9) < 200, 10)
    if not st:
        return result(False, "Radio", "both controllers must be on (IMU data) before this step")
    t_begin = time.time()
    say("Controller radio test. Hold the left controller with the battery cover open. "
        "It will start to vibrate. While it vibrates, pull the battery out.")
    p = hapticstest("left")
    gone, _ = wait_for(lambda s: ctrl(s, "left").get("imu_age_ms", 0) > 1500, 20)
    p.wait(timeout=30)
    if not gone:
        say("I did not notice the left controller going away. Skipping the rest of this test.")
        return result(False, "Left controller pulled", "IMU data never stopped")
    result(True, "Left controller pulled", "IMU data stopped")

    say("Good. Keep the battery out. Now the right controller vibrates. Tell me later if you felt it.")
    s0 = status()
    w0, f0 = haptic(s0, "right")
    hapticstest("right").wait(timeout=30)
    time.sleep(1)
    w1, f1 = haptic(status(), "right")
    result(w1 > w0 and f1 == f0, "Right vibrates while the left is gone",
           f"{w1 - w0} writes, {f1 - f0} failed")

    say("Now put the battery back into the left controller.")
    back, dt = wait_for(lambda s: ctrl(s, "left").get("imu_age_ms", 1e9) < 200, 40)
    result(bool(back), "Left controller back", f"IMU data after {dt:.1f} s" if back else "no IMU data within 40 s")
    if back:
        say("Welcome back. Point it at the headset so the cameras see it.")
        tr, dt = wait_for(lambda s: ctrl(s, "left").get("position_tracked"), 20)
        result(bool(tr), "Left controller tracked again", f"after {dt:.1f} s" if tr else "not within 20 s")
        say("And the left one vibrates again.")
        w0, f0 = haptic(status(), "left")
        hapticstest("left").wait(timeout=30)
        time.sleep(1)
        w1, f1 = haptic(status(), "left")
        result(w1 > w0 and f1 == f0, "Left vibrates again", f"{w1 - w0} writes, {f1 - f0} failed")
    j = journal_since(t_begin)
    result(None, "Radio queue", f"{len(re.findall('radio command timed out', j))} timed-out commands, "
           f"{len(re.findall('failing head', j))} commands given up (new: deadline / retry limit)")


def set_lens(mode):
    DROPIN.parent.mkdir(parents=True, exist_ok=True)
    DROPIN.write_text("[Service]\n" + (f'Environment="RIFT_S_DISTORTION={mode}"\n' if mode else ""))
    run(["systemctl", "--user", "daemon-reload"])


def step_lens():
    log("Lens model (blind A / B / A)")
    variants = ["", "mesh"]
    random.shuffle(variants)
    names = {"": "c1 = 0.47 (older model)", "mesh": "mesh (default)"}
    say("Lens test. You will see three versions, A, B, and A again, 25 seconds each. "
        "Look at straight lines and text near the edges of your view, for example a WayVR panel moved "
        "to the side, and the floor grid. Remember which one looks straighter at the edges.")
    try:
        for label, mode in (("A", variants[0]), ("B", variants[1]), ("A", variants[0])):
            set_lens(mode)
            stop_monado()
            start_monado()
            wait_for(head_tracked, 25)
            time.sleep(3)  # WayVR
            say(f"Version {label}.")
            time.sleep(25)
    finally:
        DROPIN.unlink(missing_ok=True)
        run(["systemctl", "--user", "daemon-reload"])
    say("Done. Take the headset off and answer in the terminal.")
    stop_monado()
    start_monado()
    ans = input("Which looked straighter at the edges? a / b / same: ").strip().lower()
    picked = {"a": variants[0], "b": variants[1]}.get(ans)
    log(f"    A was {names[variants[0]]}, B was {names[variants[1]]}")
    result(None, "Lens preference", f"{names[picked]}" if picked is not None else "no difference")
    if picked == "":
        log("    -> to switch back to c1: Tools tab, Lens model, Apply c1 (0.47)")


def main():
    global QUIET
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--steps", default="preflight,starts,load,radio,lens")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    QUIET = args.quiet
    steps = [s.strip() for s in args.steps.split(",") if s.strip()]
    funcs = {"preflight": step_preflight, "starts": step_starts, "load": step_load, "radio": step_radio,
             "lens": step_lens}
    for s in steps:
        if s not in funcs:
            sys.exit(f"unknown step {s}; known: {', '.join(funcs)}")
    out = RECORDINGS / ("tour-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    log(f"Tour {datetime.datetime.now():%Y-%m-%d %H:%M}, steps: {', '.join(steps)}")
    try:
        for s in steps:
            log()
            ok = funcs[s]()
            if s == "preflight" and ok is False and len(steps) > 1:
                log("\nThe headset must be connected on USB 3 for the other steps. Stopping here.")
                break
    except KeyboardInterrupt:
        log("\ninterrupted")
    finally:
        DROPIN.unlink(missing_ok=True)
        run(["systemctl", "--user", "daemon-reload"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.txt").write_text("\n".join(report_lines) + "\n")
        log(f"\nReport: {out / 'report.txt'}")
    log("Not in the tour: the Oculus grip / aim offsets. In Alyx your hands should sit and angle differently "
        "(patch 0043; RIFT_S_POSE_OFFSETS=wmr in rift-s.conf restores the old ones).")


if __name__ == "__main__":
    main()
