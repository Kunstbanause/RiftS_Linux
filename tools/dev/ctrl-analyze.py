#!/usr/bin/env python3
"""Analyse a controller recording (RIFT_S_CTRL_RECORD directory), optionally against a second one.

Per controller, for the whole recording and for every protocol segment (segments.csv from
ctrl-protocol.py):
  fixes      optical fix rate, accepted / rejected / reset %, exposure-to-arrival latency
  dropouts   gaps without an accepted fix > 0.2 s (per minute, longest, total)
  wrong      fixes > 20 deg away from the filter's own orientation at that time (wrong LED matches)
  implaus.   fixes > 1 m from the head or inside it (< 8 cm)
  excursion  filter reset onto a fix > 30 cm from the last accepted one ("wanders off"), and how many
             of them reset back within 3 s ("jumps back")
  jitter     high-frequency output noise: replayed 80 Hz filter output minus a 110 ms quadratic fit
             (meaningful in the still / smooth segments, fast moves add real motion)
  tracked    share of the replayed output with position tracked
  tilt err   at rest (accelerometer = gravity), angle between measured gravity and the gravity direction
             of the replayed orientation, p50 / p90: catches the filter following wrong (flipped) poses
  tracker    constellation tracker: dropped camera frames/s, fast analysis and full search times
Also writes glitches.csv into the recording directory (one row per event, with context).

Old recordings (before patch 0012) lack the head / prior / tracker columns; those metrics are skipped.
Everything that depends on the filter (accepted / rejected / resets, dropouts, wrong matches, excursions,
tracked, jitter) comes from replaying the recording through tools/replay with the current filter code,
so filter changes can be compared on the same recording (--replay-args, --compare-args). Source, latency
and head distance are what the live tracker recorded.

Usage: ctrl-analyze.py <recording dir> [--compare <other dir>] [--segments-only]
                       [--replay-args "k=v ..."] [--compare-args "k=v ..."]
  --compare-args  replay the same recording again with these parameters and compare (filter A/B)
"""
import argparse
import csv
import math
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

TOOLS = Path(__file__).resolve().parent
REPLAY = TOOLS / "replay"
SOURCES = ["prior", "last_seen", "labelled", "shallow", "deep", "prior_refined"]
WRONG_DEG = 20.0
DROPOUT_S = 0.2
EXCURSION_M = 0.30
RETURN_S = 3.0
RETURN_M = 0.15
FAR_M = 1.0
INSIDE_M = 0.08


def read_csv(path):
    """CSV with a header into a dict of float numpy arrays (empty dict if no rows)."""
    with open(path) as f:
        rows = list(csv.reader(f))
    if len(rows) < 2:
        return {}
    header = rows[0]
    width = len(header)
    data = [r for r in rows[1:] if len(r) == width]
    arr = np.array(data, dtype=np.float64) if data else np.zeros((0, width))
    return {name: arr[:, i] for i, name in enumerate(header)}


def quat_angle_deg(qa, qb):
    """Angle between two arrays of quaternions (N, 4) in degrees."""
    dot = np.abs(np.sum(qa * qb, axis=1))
    return np.degrees(2 * np.arccos(np.clip(dot, 0.0, 1.0)))


def run_replay(imu, opt, args):
    """Replay a controller through the current filter: (80 Hz output, per-fix trace), {} if unavailable."""
    if not REPLAY.exists():
        print(f"note: {REPLAY} not built, filter metrics are the live recorded ones", file=sys.stderr)
        return {}, {}
    with tempfile.TemporaryDirectory() as tmp:
        out, trace = Path(tmp) / "out.csv", Path(tmp) / "trace.csv"
        subprocess.run([str(REPLAY), str(imu), str(opt), *args, "--out", str(out), "--trace", str(trace)],
                       capture_output=True, check=False)
        return (read_csv(out) if out.exists() else {}), (read_csv(trace) if trace.exists() else {})


def apply_trace(o, trace):
    """Replace the recorded (live) filter results with the replayed ones, matched by exposure time."""
    if not trace or "frame_ns" not in o:
        return
    index = {int(t): i for i, t in enumerate(o["frame_ns"])}
    res = np.full(len(o["frame_ns"]), 3.0)  # not replayed -> "ignored"
    rot = np.full(len(o["frame_ns"]), -1.0)
    for t, r, e in zip(trace["frame_ns"], trace["result"], trace["prior_rot_err_deg"]):
        i = index.get(int(t))
        if i is not None:
            res[i], rot[i] = r, e
    o["live_result"] = o["result"]
    o["result"] = res
    o["replay_rot_err"] = rot


def rest_gravity(imu_path):
    """Times and unit gravity directions (IMU frame) where the controller is at rest: 0.1 s windows with
    a steady |accel| near g and |gyro| below 0.2 rad/s."""
    d = read_csv(imu_path)
    if not d:
        return {}
    t = d["mono_ns"]
    a = np.stack([d["ax"], d["ay"], d["az"]], axis=1)
    g = np.stack([d["gx"], d["gy"], d["gz"]], axis=1)
    n = 50  # 0.1 s at 500 Hz
    m = len(t) // n
    if m == 0:
        return {}
    a = a[: m * n].reshape(m, n, 3)
    g = g[: m * n].reshape(m, n, 3)
    an = np.linalg.norm(a, axis=2)
    gn = np.linalg.norm(g, axis=2)
    # Steady acceleration near g (the left controller's accelerometer reads ~3 % high, so not a tight |a| = g)
    rest = ((an.max(axis=1) - an.min(axis=1)) < 0.4) & (np.abs(an.mean(axis=1) - 9.80665) < 0.6) & \
        (gn.max(axis=1) < 0.2)
    mean_a = a.mean(axis=1)
    up = mean_a / np.linalg.norm(mean_a, axis=1, keepdims=True)
    tc = t[: m * n].reshape(m, n).mean(axis=1)
    return {"t": tc[rest], "up": up[rest]}


def tilt_errors(out, rest, t0, t1):
    """Degrees between measured 'up' and the replayed orientation's up at rest moments in [t0, t1]."""
    if not out or not rest or len(rest["t"]) == 0:
        return np.zeros(0)
    w = (rest["t"] >= t0) & (rest["t"] <= t1)
    if not np.any(w):
        return np.zeros(0)
    ts, up = rest["t"][w], rest["up"][w]
    idx = np.clip(np.searchsorted(out["t_ns"], ts), 0, len(out["t_ns"]) - 1)
    ok = (np.abs(out["t_ns"][idx] - ts) < 20e6) & (out["tracked"][idx] == 1)
    if not np.any(ok):
        return np.zeros(0)
    q = np.stack([out["qx"], out["qy"], out["qz"], out["qw"]], axis=1)[idx[ok]]
    # World up (0, 1, 0) in the body frame: R^T * y
    x, y, z, w_ = q[:, 0], q[:, 1], q[:, 2], q[:, 3]
    body_up = np.stack([2 * (x * y + w_ * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w_ * x)], axis=1)
    dot = np.clip(np.sum(body_up * up[ok], axis=1), -1, 1)
    return np.degrees(np.arccos(dot))


class Recording:
    def __init__(self, path, replay_args=()):
        self.path = Path(path)
        self.sides = {}
        for side in ("left", "right"):
            imu = sorted(self.path.glob(f"rift_s_{side}_*_imu.csv"))
            opt = sorted(self.path.glob(f"rift_s_{side}_*_optical.csv"))
            if not imu or not opt:
                continue
            o = read_csv(opt[-1])
            if not o:
                continue
            out, trace = run_replay(imu[-1], opt[-1], replay_args)
            apply_trace(o, trace)
            self.sides[side] = {"opt": o, "out": out, "rest": rest_gravity(imu[-1])}
        trk = sorted(self.path.glob("rift_s_tracker_*.csv"))
        self.tracker = read_csv(trk[-1]) if trk else {}
        self.segments = []
        seg = self.path / "segments.csv"
        if seg.exists():
            with open(seg) as f:
                for r in csv.DictReader(f):
                    self.segments.append((f"{r['run']}/{r['segment']}", int(r["start_ns"]), int(r["end_ns"])))
        times = [s["opt"]["frame_ns"] for s in self.sides.values() if len(s["opt"].get("frame_ns", []))]
        self.t0 = min(t[0] for t in times) if times else 0
        self.t1 = max(t[-1] for t in times) if times else 0

    def scopes(self, segments_only=False):
        out = [] if segments_only and self.segments else [("all", self.t0, self.t1)]
        return out + self.segments


def window(d, key, t0, t1):
    if not d or key not in d:
        return np.zeros(0, dtype=bool)
    t = d[key]
    return (t >= t0) & (t <= t1)


def dropouts(times, t0, t1, scope_is_segment):
    """Gaps between accepted fixes longer than DROPOUT_S, as (start_ns, length_s)."""
    edges = np.concatenate(([t0], times, [t1])) if scope_is_segment else times
    if len(edges) < 2:
        return [(t0, (t1 - t0) / 1e9)] if scope_is_segment else []
    gaps = np.diff(edges) / 1e9
    idx = np.nonzero(gaps > DROPOUT_S)[0]
    return [(edges[i], gaps[i]) for i in idx]


def side_metrics(rec, side, t0, t1, scope_is_segment, glitches, scope_name):
    s = rec.sides[side]
    o, out = s["opt"], s["out"]
    m = {}
    dur = max((t1 - t0) / 1e9, 1e-6)
    w = window(o, "frame_ns", t0, t1)
    res = o["result"][w].astype(int)
    n = len(res)
    m["fixes/s"] = n / dur
    if n:
        m["accepted %"] = 100 * np.mean(res == 0)
        m["rejected %"] = 100 * np.mean(res == 1)
        m["resets"] = int(np.sum(res == 2))
        lat = (o["arrival_ns"][w] - o["frame_ns"][w]) / 1e6
        m["latency p50 ms"] = float(np.percentile(lat, 50))
        m["latency p95 ms"] = float(np.percentile(lat, 95))

    # Dropouts: accepted fixes and resets count as tracking
    good = w & ((o["result"] == 0) | (o["result"] == 2))
    drops = dropouts(o["frame_ns"][good], t0, t1, scope_is_segment)
    m["dropouts/min"] = len(drops) / dur * 60
    m["longest gap s"] = max((g for _, g in drops), default=0.0)
    m["lost s"] = sum(g for _, g in drops)
    for start, g in drops:
        glitches.append((start, side, "dropout", f"{g:.2f} s", scope_name))

    pos = np.stack([o["px"], o["py"], o["pz"]], axis=1)
    quat = np.stack([o["qx"], o["qy"], o["qz"], o["qw"]], axis=1)

    if "replay_rot_err" in o or "prior_valid" in o:
        if "replay_rot_err" in o:  # against the replayed filter
            pv = w & (o["replay_rot_err"] >= 0)
            ang = o["replay_rot_err"][pv]
        else:  # against the live filter
            pv = w & (o["prior_valid"] == 1)
            prior_q = np.stack([o["prior_qx"], o["prior_qy"], o["prior_qz"], o["prior_qw"]], axis=1)
            ang = quat_angle_deg(quat[pv], prior_q[pv])
        if np.any(pv):
            wrong = ang > WRONG_DEG
            m["wrong %"] = 100 * np.mean(wrong)
            for i in np.nonzero(wrong)[0]:
                k = np.nonzero(pv)[0][i]
                details = f"{ang[i]:.0f} deg, result {int(o['result'][k])}"
                if "source" in o:
                    src = int(o["source"][k])
                    details += (f", {SOURCES[src] if src < len(SOURCES) else src}, cam {int(o['view'][k])}, "
                                f"{int(o['matched'][k])}/{int(o['visible'][k])} LEDs")
                glitches.append((o["frame_ns"][k], side, "wrong_match", details, scope_name))
        if "source" in o and n:
            src_all = o["source"][w].astype(int)
            parts = []
            for i, name in enumerate(SOURCES):
                sel = src_all == i
                if np.any(sel):
                    parts.append(f"{name} {100 * np.mean(sel):.0f}%/{100 * np.mean(res[sel] == 0):.0f}%ok")
            m["sources"] = " ".join(parts)

    if "head_px" in o and n:
        head = np.stack([o["head_px"], o["head_py"], o["head_pz"]], axis=1)
        dist = np.linalg.norm(pos[w] - head[w], axis=1)
        far = dist > FAR_M
        inside = dist < INSIDE_M
        m["implausible %"] = 100 * np.mean(far | inside)
        for k in np.nonzero(w)[0][far | inside]:
            d = np.linalg.norm(pos[k] - head[k])
            glitches.append((o["frame_ns"][k], side, "implausible",
                             f"{d:.2f} m from head, result {int(o['result'][k])}", scope_name))

    # Excursions: resets that jump far from the last accepted fix, and whether they come back
    idx = np.nonzero(w)[0]
    last_ok = None
    exc, back = 0, 0
    for k in idx:
        r = o["result"][k]
        if r == 0:
            last_ok = k
        elif r == 2 and last_ok is not None:
            jump = np.linalg.norm(pos[k] - pos[last_ok])
            if jump > EXCURSION_M:
                exc += 1
                later = idx[(o["frame_ns"][idx] > o["frame_ns"][k]) &
                            (o["frame_ns"][idx] < o["frame_ns"][k] + RETURN_S * 1e9) & (o["result"][idx] == 2)]
                returned = any(np.linalg.norm(pos[j] - pos[last_ok]) < RETURN_M for j in later)
                back += returned
                glitches.append((o["frame_ns"][k], side, "excursion",
                                 f"jump {jump:.2f} m{', back' if returned else ''}", scope_name))
            last_ok = k
    m["excursions"] = exc
    m["  ...came back"] = back

    if out:
        wo = window(out, "t_ns", t0, t1)
        if np.any(wo):
            tr = out["tracked"][wo] == 1
            m["tracked %"] = 100 * np.mean(tr)
            p = np.stack([out["px"], out["py"], out["pz"]], axis=1)[wo]
            jit = []
            # Jitter within tracked runs only
            runs = np.split(np.arange(len(tr)), np.nonzero(np.diff(tr.astype(int)))[0] + 1)
            for run in runs:
                if len(run) >= 15 and tr[run[0]]:
                    q = p[run]
                    # 9-point quadratic Savitzky-Golay: follows smooth accelerating motion, not noise
                    k = np.array([-21, 14, 39, 54, 59, 54, 39, 14, -21]) / 231.0
                    smooth = np.stack([np.convolve(q[:, i], k, mode="valid") for i in range(3)], axis=1)
                    jit.append(q[4:-4] - smooth)
            if jit:
                j = np.concatenate(jit)
                m["jitter rms mm"] = 1000 * float(np.sqrt(np.mean(np.sum(j * j, axis=1))))
        te = tilt_errors(out, s.get("rest"), t0, t1)
        if len(te) >= 3:
            m["tilt err p50/p90"] = f"{np.percentile(te, 50):.1f} / {np.percentile(te, 90):.1f} deg (n {len(te)})"
    return m


def tracker_metrics(rec, t0, t1):
    t = rec.tracker
    w = window(t, "frame_ns", t0, t1)
    if np.count_nonzero(w) < 2:
        return {}
    dur = (t["frame_ns"][w][-1] - t["frame_ns"][w][0]) / 1e9
    m = {}
    rcv = np.diff(t["received"][w]).sum()
    prc = np.diff(t["processed"][w]).sum()
    m["frames/s"] = rcv / dur
    m["dropped/s"] = max(0.0, (rcv - prc) / dur)
    m["fast p50 ms"] = float(np.percentile(t["fast_ms"][w], 50))
    m["fast p95 ms"] = float(np.percentile(t["fast_ms"][w], 95))
    ran = np.nonzero(np.diff(t["long_run"][w]) > 0)[0] + 1
    m["full search/s"] = np.diff(t["long_run"][w]).sum() / dur
    m["superseded/s"] = np.diff(t["long_superseded"][w]).sum() / dur
    if len(ran):
        m["search p95 ms"] = float(np.percentile(t["long_ms"][w][ran], 95))
    return m


def separation(rec, t0, t1):
    """Median distance between the controllers' replayed outputs (both tracked)."""
    if "left" not in rec.sides or "right" not in rec.sides:
        return {}
    lo, ro = rec.sides["left"]["out"], rec.sides["right"]["out"]
    if not lo or not ro:
        return {}
    wl = window(lo, "t_ns", t0, t1)
    if not np.any(wl):
        return {}
    t = lo["t_ns"][wl]
    both = (lo["tracked"][wl] == 1) & (np.interp(t, ro["t_ns"], ro["tracked"]) > 0.99)
    m = {"both tracked %": 100 * np.mean(both)}
    if np.any(both):
        lp = np.stack([lo[c][wl] for c in ("px", "py", "pz")], axis=1)
        rp = np.stack([np.interp(t, ro["t_ns"], ro[c]) for c in ("px", "py", "pz")], axis=1)
        m["separation cm"] = 100 * float(np.median(np.linalg.norm(lp[both] - rp[both], axis=1)))
    return m


def analyse(rec, segments_only):
    """{scope: {section: {metric: value}}} plus glitch rows."""
    results, glitches = {}, []
    for name, t0, t1 in rec.scopes(segments_only):
        seg = name != "all"
        r = {}
        for side in rec.sides:
            r[side] = side_metrics(rec, side, t0, t1, seg, glitches if seg or not rec.segments else [], name)
        r["both"] = separation(rec, t0, t1)
        r["tracker"] = tracker_metrics(rec, t0, t1)
        r["_dur"] = (t1 - t0) / 1e9
        results[name] = r
    return results, glitches


def fmt(v):
    if v is None:
        return "-"
    if isinstance(v, str):
        return v
    if isinstance(v, (int, np.integer)):
        return str(int(v))
    return f"{v:.1f}"


def report(a, b=None, names=("A", "B")):
    scopes = list(a.keys()) + [s for s in (b or {}) if s not in a]
    for scope in scopes:
        ra, rb = a.get(scope, {}), (b or {}).get(scope, {})
        dur = ra.get("_dur", rb.get("_dur", 0))
        print(f"\n=== {scope}  ({dur:.0f} s)")
        for section in ("left", "right", "both", "tracker"):
            ma, mb = ra.get(section, {}), rb.get(section, {})
            keys = list(ma.keys()) + [k for k in mb if k not in ma]
            if not keys:
                continue
            print(f"  [{section}]")
            for k in keys:
                if b is None:
                    print(f"    {k:<16} {fmt(ma.get(k))}")
                else:
                    print(f"    {k:<16} {fmt(ma.get(k)):>12} | {fmt(mb.get(k))}")
    if b is not None:
        print(f"\n(left column: {names[0]}, right column: {names[1]})")


def write_glitches(rec, glitches):
    path = rec.path / "glitches.csv"
    with open(path, "w") as f:
        f.write("t_s,controller,type,details,scope\n")
        for t, side, kind, details, scope in sorted(glitches):
            f.write(f"{(t - rec.t0) / 1e9:.2f},{side},{kind},\"{details}\",{scope}\n")
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("recording", type=Path)
    ap.add_argument("--compare", type=Path)
    ap.add_argument("--segments-only", action="store_true")
    ap.add_argument("--replay-args", default="")
    ap.add_argument("--compare-args")
    args = ap.parse_args()

    rec = Recording(args.recording, shlex.split(args.replay_args))
    if not rec.sides:
        sys.exit(f"no controller recordings in {args.recording}")
    a, glitches = analyse(rec, args.segments_only)
    if args.compare or args.compare_args is not None:
        other = args.compare or args.recording
        rec_b = Recording(other, shlex.split(args.compare_args or ""))
        b, _ = analyse(rec_b, args.segments_only)
        names = (f"{args.recording.name} {args.replay_args}".strip(), f"{other.name} {args.compare_args or ''}".strip())
        report(a, b, names)
    else:
        report(a)
    path = write_glitches(rec, glitches)
    counts = {}
    for g in glitches:
        counts[g[2]] = counts.get(g[2], 0) + 1
    print(f"\n{len(glitches)} glitches -> {path}  " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))


if __name__ == "__main__":
    main()
