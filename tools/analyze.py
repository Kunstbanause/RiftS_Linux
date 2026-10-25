#!/usr/bin/env python3
"""Summarise a posrec CSV (80 Hz controller poses) into smoothness / jitter / dropout metrics.

Usage: analyze.py <posrec.csv>
"""
import csv
import math
import sys


def load(path):
    rows = {"L": [], "R": []}
    with open(path) as f:
        for r in csv.DictReader(f):
            try:
                rows[r["side"]].append(
                    (float(r["t_s"]), int(r["tracked"]), float(r["px"]), float(r["py"]), float(r["pz"]))
                )
            except (KeyError, ValueError, TypeError):
                pass
    return rows


def dist(a, b):
    return math.sqrt((a[2] - b[2]) ** 2 + (a[3] - b[3]) ** 2 + (a[4] - b[4]) ** 2)


def summarise(side, s):
    if len(s) < 10:
        print(f"{side}: not enough samples")
        return
    steps = [dist(s[i], s[i - 1]) for i in range(1, len(s)) if s[i][1] and s[i - 1][1]]
    frozen = sum(1 for d in steps if d < 1e-6)
    tracked = sum(1 for r in s if r[1])

    # Rest jitter: 0.5 s windows with little motion, RMS deviation from the window mean
    jitters = []
    win = 40
    for i in range(0, len(s) - win, win):
        w = [r for r in s[i : i + win] if r[1]]
        if len(w) < win * 0.9:
            continue
        mean = [sum(r[k] for r in w) / len(w) for k in (2, 3, 4)]
        dev = math.sqrt(
            sum((r[2] - mean[0]) ** 2 + (r[3] - mean[1]) ** 2 + (r[4] - mean[2]) ** 2 for r in w) / len(w)
        )
        if dev < 0.01:
            jitters.append(dev)

    # Dropouts: runs of untracked samples
    runs, cur = [], 0
    for r in s:
        if not r[1]:
            cur += 1
        elif cur:
            runs.append(cur)
            cur = 0
    if cur:
        runs.append(cur)

    steps_sorted = sorted(steps)
    p99 = steps_sorted[int(0.99 * (len(steps_sorted) - 1))] if steps_sorted else 0
    jit = 1000 * sum(jitters) / len(jitters) if jitters else float("nan")
    print(
        f"{side}: tracked {100.0 * tracked / len(s):5.1f}% | frozen frames {100.0 * frozen / max(1, len(steps)):5.1f}% | "
        f"rest jitter {jit:5.2f} mm ({len(jitters)} windows) | step p99 {1000 * p99:5.1f} mm, "
        f"max {1000 * (steps_sorted[-1] if steps_sorted else 0):6.1f} mm | dropouts {len(runs)}, "
        f"longest {max(runs) / 80.0 if runs else 0:.2f} s"
    )


if __name__ == "__main__":
    data = load(sys.argv[1])
    for side in ("L", "R"):
        summarise(side, data[side])
