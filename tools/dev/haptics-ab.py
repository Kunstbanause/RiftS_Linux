#!/usr/bin/env python3
"""Vibration A/B: replay a stretch of a RIFT_S_HAPTICS_LOG recording on both controllers at once,
each through a different version of the driver's vibration logic (simulated here, then played as the
exact motor timeline).

  haptics-ab.py [log] [--start S] [--length S] [--source R|L] [--left old] [--right new] [--repeat N]

Policies:  old = patch 0015 (last request wins, 20 ms minimum, stops cut the pulse)
           new = patch 0016 (one frame's requests merged by max, stops can't cut below 40 ms)
--start: seconds into the log, default = the most intense window of --source.
Restarts Monado with RIFT_S_HAPTICS_MIN_MS=1 for the playback (so the driver passes the timeline
through unchanged) and restarts it with the normal settings afterwards. Needs no game running.
"""
import argparse
import csv
import json
import os
import time
import subprocess
import sys
import tempfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
POLICIES = {
    "old": dict(merge_ms=0.0, min_ms=20.0, stops_cut=True),
    "new": dict(merge_ms=3.0, min_ms=40.0, stops_cut=False),
}
SNAP_MS = 3.0  # the driver merges requests closer than this, drop shorter motor steps


def load(path, hand):
    out = []
    for r in csv.reader(open(path)):
        if r and r[0] != "t_ms" and r[1] == hand:
            out.append((float(r[0]), float(r[2]), float(r[3])))
    return out


def most_intense(reqs, length_ms):
    best, best_t, j, s = -1.0, reqs[0][0], 0, 0.0
    for i in range(len(reqs)):
        s += reqs[i][1]
        while reqs[i][0] - reqs[j][0] > length_ms:
            s -= reqs[j][1]
            j += 1
        if s > best:
            best, best_t = s, reqs[j][0]
    return best_t


def motor_timeline(reqs, merge_ms, min_ms, stops_cut):
    """Driver logic -> [(t_ms, amplitude)] change points of the motor amplitude."""
    amp, start, end, events = 0.0, -1e18, -1e18, []
    for t, a, d in reqs:
        if a > 0:
            d = max(d, min_ms)
            if t - start < merge_ms and t < end:
                amp, end = max(amp, a), max(end, t + d)
            else:
                start, amp, end = t, a, t + d
        elif stops_cut:
            end = t
        elif end > t:
            end = min(end, start + min_ms) if start + min_ms > t else t
        events.append((t, amp, end))
    changes = []
    for k, (t, a, e) in enumerate(events):
        nxt = events[k + 1][0] if k + 1 < len(events) else e
        changes.append((t, a if t < e else 0.0))
        if e < nxt:
            changes.append((e, 0.0))
    merged = []
    for t, a in changes:
        if merged and t - merged[-1][0] < SNAP_MS:
            merged[-1] = (merged[-1][0], a)
        elif not merged or merged[-1][1] != a:
            merged.append((t, a))
    return [c for k, c in enumerate(merged) if k == 0 or c[1] != merged[k - 1][1]]


def stats(timeline, t_end):
    on = area = 0.0
    for (t, a), nxt in zip(timeline, timeline[1:] + [(t_end, 0.0)]):
        if a > 0:
            on += nxt[0] - t
            area += a * (nxt[0] - t)
    switches = sum(1 for (_, a), (_, b) in zip(timeline, timeline[1:]) if (a > 0) != (b > 0))
    return on / 1e3, area / 1e3, switches // 2


def wait_for_controllers(timeout_s=30):
    """After a Monado restart: wait until both controllers send reports again."""
    status = Path(os.environ.get("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())) / "monado-rift-s-status.json"
    end = time.time() + timeout_s
    while time.time() < end:
        try:
            c = json.loads(status.read_text())["controllers"]
            if all(c.get(s) and c[s]["report_age_ms"] >= 0 and c[s]["report_age_ms"] < 200 for s in ("left", "right")):
                return True
        except (OSError, ValueError, KeyError, TypeError):
            pass
        time.sleep(0.25)
    return False


def systemctl(*args):
    subprocess.run(["systemctl", "--user", *args], check=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log", nargs="?", default=str(Path.home() / ".local/share/rift-s-linux/recordings/haptics.csv"))
    ap.add_argument("--start", type=float)
    ap.add_argument("--length", type=float, default=15.0)
    ap.add_argument("--source", default="R", choices="LR")
    ap.add_argument("--left", default="old", choices=POLICIES)
    ap.add_argument("--right", default="new", choices=POLICIES)
    ap.add_argument("--repeat", type=int, default=2)
    ap.add_argument("--dry-run", action="store_true", help="only print the comparison")
    args = ap.parse_args()

    reqs = load(args.log, args.source)
    if not reqs:
        sys.exit(f"no {args.source} requests in {args.log}")
    t_log0 = reqs[0][0]
    length_ms = args.length * 1e3
    t0 = t_log0 + args.start * 1e3 if args.start is not None else most_intense(reqs, length_ms)
    window = [(t - t0, a, d) for t, a, d in reqs if t0 <= t < t0 + length_ms]
    print(f"{args.source} hand, {len(window)} requests from +{(t0 - t_log0) / 1e3:.1f} s, {args.length:.0f} s long")

    lines = []
    for hand, name in (("L", args.left), ("R", args.right)):
        tl = motor_timeline(window, **POLICIES[name])
        on, area, sw = stats(tl, length_ms)
        print(f"  {hand} = {name}: motor on {on:.1f} s, strength x time {area:.1f}, {sw} on/off switches, "
              f"{len(tl)} motor changes")
        for (t, a), nxt in zip(tl, tl[1:] + [(length_ms, 0.0)]):
            if t >= 0:
                lines.append((t, hand, a, (nxt[0] - t + 2) if a > 0 else 0))
    lines.sort()
    if args.dry_run:
        return

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        for t, hand, a, d in lines:
            f.write(f"{t:.2f} {hand} {a:.3f} {d:.2f}\n")
        script = f.name

    print("Restarting Monado with RIFT_S_HAPTICS_MIN_MS=1 for the playback")
    systemctl("set-environment", "RIFT_S_HAPTICS_MIN_MS=1")
    try:
        systemctl("restart", "monado.service")
        if not wait_for_controllers():
            print("Controllers did not come back (asleep? press a button on each), playing anyway")
        rc = subprocess.run([str(TOOLS / "hapticsplay"), script, str(args.repeat)]).returncode
    finally:
        systemctl("unset-environment", "RIFT_S_HAPTICS_MIN_MS")
        systemctl("restart", "monado.service")
        print("Monado restarted with the normal settings")
        Path(script).unlink()
    sys.exit(rc)


if __name__ == "__main__":
    main()
