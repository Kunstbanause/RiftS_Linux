#!/usr/bin/env python3
"""Guided controller tracking protocol (~4 min, spoken cues on the Rift S speakers).

Runs a fixed sequence of movements so that recordings can be compared run against run, and writes
the start / end of every segment to segments.csv in the active RIFT_S_CTRL_RECORD directory
(analyse with ctrl-analyze.py). Start the recording first (vrstatus "Start controller recording"
restarts Monado, so do it before launching a game).

Segments: still, smooth (slow circles), fast, watch (WayVR watch gesture), close (hands together near
the face), reach (sides / behind the back and back).

Usage: ctrl-protocol.py [--label NAME] [--dir RECORDING_DIR] [--delay S] [--quiet] [--short]
  --label  name of this run in segments.csv (default: "alyx" if Half-Life: Alyx runs, else "idle")
  --delay  seconds to wait before the first cue (time to put the headset on)
  --short  5 s per segment, for testing the script
"""
import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def say(text, quiet):
    print(">>", text, flush=True)
    if quiet:
        return
    sinks = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True, text=True).stdout
    sinks = sorted({line.split()[1] for line in sinks.splitlines() if "Rift_S" in line})
    with tempfile.NamedTemporaryFile(suffix=".wav") as wav:
        subprocess.run(["espeak-ng", "-s", "150", "-w", wav.name, text], check=False)
        procs = [subprocess.Popen(["paplay", "--device=" + s, wav.name]) for s in sinks]
        for p in procs:
            p.wait()


def active_recording_dir():
    """RIFT_S_CTRL_RECORD of the running monado.service, from the unit's environment.

    (Not /proc/<pid>/environ: monado-service has CAP_SYS_NICE, which makes it unreadable.)"""
    run = lambda *a: subprocess.run(["systemctl", "--user", "show", "monado.service", *a],
                                    capture_output=True, text=True).stdout.strip()
    if run("-p", "ActiveState", "--value") != "active":
        return None
    for kv in run("-p", "Environment", "--value").split():
        if kv.startswith("RIFT_S_CTRL_RECORD="):
            value = kv.split("=", 1)[1]
            return Path(value) if value else None
    return None


def alyx_running():
    return subprocess.run(["pgrep", "-x", "hlvr"], capture_output=True).returncode == 0


# (name, intro, duration_s, [(offset_s, cue), ...])
def segments(scale):
    watch = []
    for i in range(6):
        watch.append((i * 7.5, "Watch."))
        watch.append((i * 7.5 + 5, "Down."))
    return [
        ("still", "Hold both controllers still in front of you, arms relaxed.", 30 * scale, []),
        ("smooth", "Slow, smooth circles and figure eights with both hands.", 45 * scale, []),
        ("fast", "Fast moves. Flicks, punches and shakes.", 30 * scale, []),
        ("watch", "Watch gesture. On watch, turn your left wrist up like looking at a watch, and point at it "
                  "with the right hand. On down, lower both.", 45 * scale, [(t * scale, c) for t, c in watch]),
        ("close", "Hands close together in front of your face. Keep swapping them over and under.", 30 * scale,
         []),
        ("reach", "Reaching.", 30 * scale,
         [(t * scale, c) for t, c in [(0, "Right hand far to the side."), (5, "Back."),
                                      (8, "Left hand far to the side."), (13, "Back."),
                                      (16, "Right hand behind your back."), (21, "Back."),
                                      (24, "Left hand behind your back."), (29, "Back.")]]),
    ]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--label")
    ap.add_argument("--dir", type=Path)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--short", action="store_true")
    ap.add_argument("--delay", type=float, default=0)
    args = ap.parse_args()

    rec = args.dir or active_recording_dir()
    if rec is None or not rec.is_dir():
        # stdout, so vrstatus can show it
        print("No active controller recording: start one first (vrstatus Tools tab, "
              "'Start controller recording'), then launch the game.")
        sys.exit(1)
    label = args.label or ("alyx" if alyx_running() else "idle")
    out = rec / "segments.csv"
    new = not out.exists() or out.stat().st_size == 0
    print(f"Protocol run '{label}', segments -> {out}")

    time.sleep(args.delay)
    say("Tracking protocol, about four minutes. Follow the instructions.", args.quiet)
    with open(out, "a") as f:
        if new:
            f.write("run,segment,start_ns,end_ns\n")
        for name, intro, duration, cues in segments(0.17 if args.short else 1.0):
            say(intro, args.quiet)
            say("Go.", args.quiet)
            start = time.monotonic_ns()
            for offset, cue in cues:
                wait = start / 1e9 + offset - time.monotonic_ns() / 1e9
                if wait > 0:
                    time.sleep(wait)
                say(cue, args.quiet)
            wait = start / 1e9 + duration - time.monotonic_ns() / 1e9
            if wait > 0:
                time.sleep(wait)
            end = time.monotonic_ns()
            f.write(f"{label},{name},{start},{end}\n")
            f.flush()
            say("Stop.", args.quiet)
    say("Done. Thank you.", args.quiet)
    print(f"Analyse: tools/ctrl-analyze.py {rec}")


if __name__ == "__main__":
    main()
