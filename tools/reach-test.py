#!/usr/bin/env python3
"""Guided reach test for the "T-rex arms" problem: where are the controllers relative to the head?

Spoken cues on the Rift S speakers, then 2 s of samples per pose from the driver status file
(patch 0008, head = device pose, controllers = fusion IMU position, same space, 4 Hz).
Prints each controller in head coordinates: right / up / forward in cm.

Expected with correct geometry:
  ring touching the headset front -> forward ~ +10..+15 cm, right/up ~ 0
  controllers side by side, parallel, rings touching -> L-R distance ~ 9..10 cm
                                     (ring width 8.6 cm; IMUs sit just below the ring bases, so any V-shaped tilt adds cm)
  arm stretched forward           -> forward ~ eye-to-fist distance (tape measure it, ~60-70 cm)

Usage: reach-test.py [--quiet]   (needs Monado running, headset worn, controllers tracked)
"""
import json
import os
import subprocess
import sys
import tempfile
import time

STATUS = os.environ.get("RIFT_S_STATUS_FILE") or os.path.join(
    os.environ.get("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid()), "monado-rift-s-status.json")
QUIET = "--quiet" in sys.argv


def say(text):
    print(">>", text, flush=True)
    if QUIET:
        return
    sinks = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True, text=True).stdout
    sinks = sorted({line.split()[1] for line in sinks.splitlines() if "Rift_S" in line})
    with tempfile.NamedTemporaryFile(suffix=".wav") as wav:
        subprocess.run(["espeak-ng", "-s", "150", "-w", wav.name, text], check=False)
        procs = [subprocess.Popen(["paplay", "--device=" + s, wav.name]) for s in sinks]
        for p in procs:
            p.wait()


def quat_conj_rotate(q, v):
    """Rotate v by the inverse of unit quaternion q = (x, y, z, w): world -> head frame."""
    x, y, z, w = -q[0], -q[1], -q[2], q[3]
    # v' = v + 2w(u x v) + 2 u x (u x v), u = (x, y, z)
    ux = (y * v[2] - z * v[1], z * v[0] - x * v[2], x * v[1] - y * v[0])
    uux = (y * ux[2] - z * ux[1], z * ux[0] - x * ux[2], x * ux[1] - y * ux[0])
    return [v[i] + 2 * w * ux[i] + 2 * uux[i] for i in range(3)]


def read_status():
    with open(STATUS) as f:
        return json.load(f)


def sample(duration=2.0):
    """Median controller positions in head coordinates (right, up, forward) in cm."""
    out = {"left": [], "right": []}
    seen = set()
    end = time.time() + duration
    while time.time() < end:
        try:
            st = read_status()
        except (OSError, ValueError):
            time.sleep(0.05)
            continue
        if st["time_ns"] in seen:
            time.sleep(0.05)
            continue
        seen.add(st["time_ns"])
        head = st["head"]
        for side in ("left", "right"):
            c = st["controllers"].get(side)
            if not c or not c.get("position_tracked"):
                continue
            d = [c["position"][i] - head["position"][i] for i in range(3)]
            h = quat_conj_rotate(head["orientation"], d)
            out[side].append((100 * h[0], 100 * h[1], -100 * h[2]))
        time.sleep(0.05)

    def med(vals):
        if not vals:
            return None
        return tuple(sorted(v[i] for v in vals)[len(vals) // 2] for i in range(3))

    return {side: med(v) for side, v in out.items()}, {side: len(v) for side, v in out.items()}


def fmt(p):
    if p is None:
        return "not tracked"
    return "right %+6.1f  up %+6.1f  forward %+6.1f cm" % p


STEPS = [
    ("front_right", "Right controller: touch the ring to the middle of the headset front. Hold still."),
    ("front_left", "Left controller: touch the ring to the middle of the headset front. Hold still."),
    ("together", "Hold both controllers side by side and parallel in front of you, rings touching. Hold still."),
    ("reach_right", "Stretch your right arm straight forward at eye height, fully extended. Hold still."),
    ("reach_left", "Stretch your left arm straight forward at eye height, fully extended. Hold still."),
]


def main():
    try:
        read_status()
    except OSError:
        sys.exit("no status file at %s - is Monado running?" % STATUS)

    say("Reach test. Five poses. After each instruction you have four seconds to get into position.")
    results = {}
    for key, text in STEPS:
        say(text)
        time.sleep(4)
        results[key] = sample()
        say("Thanks.")
    say("Done.")

    print()
    for key, _ in STEPS:
        pos, n = results[key]
        print("%-12s L %s  (%d)" % (key, fmt(pos["left"]), n["left"]))
        print("%-12s R %s  (%d)" % ("", fmt(pos["right"]), n["right"]))
        if key == "together" and pos["left"] and pos["right"]:
            d = sum((pos["left"][i] - pos["right"][i]) ** 2 for i in range(3)) ** 0.5
            print("%-12s L-R distance %.1f cm" % ("", d))


if __name__ == "__main__":
    main()
