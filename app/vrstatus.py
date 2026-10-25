#!/usr/bin/env python3
"""Rift S / Monado live status window with first-time setup.

Usage: rift-s-linux            window + tray icon (own venv with pip PySide6, see the launcher)
       rift-s-linux --hidden   start in the tray
       rift-s-linux --once     print one text report and exit (no Qt needed)
       rift-s-linux setup ...  setup in the terminal (app/setup_steps.py)

Data sources (no OpenXR client is ever opened for polling):
- $XDG_RUNTIME_DIR/monado-rift-s-status.json: head pose + controller link / tracking / battery,
  written by the Rift S driver 4x per second (patches/monado/0008).
- One persistent libmonado connection for the client list (opened only while monado.service is
  already active, because connecting to the socket would otherwise start Monado through socket
  activation, and never reconnected in a loop: connect / disconnect churn was involved in a
  compositor freeze).
- systemctl (monado.service, wayvr.service), journalctl, sysfs, lsusb, sensors, /proc.
Only the explicit test buttons (head stillness, guided controller test) open OpenXR sessions.
"""

import collections
import ctypes
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import *  # noqa: E402,F401,F403
import setup_steps  # noqa: E402

# rift-s.conf defaults (config/systemd/monado.service.d/rift-s.conf)
DEFAULTS = setup_steps.conf_env((CONFIG / "systemd/monado.service.d/rift-s.conf").read_text())

# Client names that are tools / probes, not games ("libmonado" = us and WayVR's control link)
PROBE_NAMES = ("monado-ctl", "libmonado", "posrec", "headrec")
GAME_NAMES = {"hlvr": "Half-Life: Alyx", "Beat Saber": "Beat Saber", "HelloXR": "hello_xr"}

# Journal lines that mean Monado is broken (restart needed)
SERIOUS_ERRORS = (
    "VK_ERROR_DEVICE_LOST",
    "VK_ERROR_SURFACE_LOST_KHR",
    "Dequeue failed",
    "Didn't find Rift S camera device",
    "Failed to initialise Oculus Rift S driver",
)
# Journal lines worth showing in Details, but seen in healthy sessions too
MINOR_ERRORS = ("vkRegisterDisplayEventEXT",)
SLAM_WARN_RE = re.compile(r"det\(Q1Jl\)|d_res_d_p is not valid")

GREY, GREEN, YELLOW, RED = 0, 1, 2, 3
STATE_NAMES = {GREY: "-", GREEN: "OK", YELLOW: "WARN", RED: "FAIL"}
COLORS = {GREY: "#8b949e", GREEN: "#2ea043", YELLOW: "#d29922", RED: "#e5534b"}


@dataclass
class Check:
    key: str
    label: str
    state: int
    value: str = ""
    hint: str = ""


@dataclass
class Row:
    """One dashboard line: what it is, how it is, and what to do about it."""
    key: str
    icon: str
    category: str
    name: str
    state: int
    status: str
    detail: str = ""                                # small second line under the status
    info: list = field(default_factory=list)        # shown when the row is opened
    todo: str = ""                                  # suggestion
    actions: list = field(default_factory=list)     # [(button label, action name)]
    speak: str = ""                                 # spoken in the headset when it turns red


# ----------------------------------------------------------------------------------------------
# Small helpers


def run(cmd, timeout=5):
    """Run a command, return (rc, stdout, stderr); never raises."""
    try:
        r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, timeout=timeout,
                           errors="replace")
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, "", str(e)


def read_text(path, default=""):
    try:
        return Path(path).read_text(errors="replace").strip()
    except OSError:
        return default


def pid_alive(pid):
    return pid > 0 and Path(f"/proc/{pid}").exists()


def proc_cpu_seconds(pid):
    """utime + stime of a process in seconds, None if gone."""
    try:
        rest = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return (int(rest[11]) + int(rest[12])) / os.sysconf("SC_CLK_TCK")
    except (OSError, IndexError, ValueError):
        return None


def find_pids(match):
    """PIDs of our own processes for which match(pid, comm, argv, exe) is true. Never matches us."""
    out = []
    me = os.getpid()
    for d in Path("/proc").iterdir():
        if not d.name.isdigit() or int(d.name) == me:
            continue
        pid = int(d.name)
        try:
            if d.stat().st_uid != os.getuid():
                continue
            comm = (d / "comm").read_text().strip()
            exe = os.readlink(d / "exe") if (d / "exe").exists() else ""
            argv = [a.decode(errors="replace") for a in (d / "cmdline").read_bytes().split(b"\0") if a]
        except OSError:
            continue
        if match(pid, comm, argv, exe):
            out.append(pid)
    return out


def wayvr_manual_pids():
    """WayVR processes started by hand (AppImage / menu), i.e. not by wayvr.service."""
    def match(pid, comm, argv, exe):
        argv0 = os.path.basename(argv[0]).lower() if argv else ""
        if not ("wayvr" in argv0 or "wayvr" in exe.lower() or comm.lower().startswith("wayvr")):
            return False
        return "wayvr.service" not in read_text(f"/proc/{pid}/cgroup")
    return find_pids(match)


def unit_props(unit, *props):
    rc, out, _ = run(["systemctl", "--user", "show", unit, "--timestamp=unix"] +
                     [a for p in props for a in ("-p", p)])
    return dict(ln.split("=", 1) for ln in out.splitlines() if "=" in ln)


def read_conf():
    """Environment lines of rift-s.conf as a dict."""
    env = {}
    for line in read_text(RIFT_S_CONF).splitlines():
        m = re.match(r'\s*Environment="?([A-Za-z0-9_]+)=(.*?)"?\s*$', line)
        if m:
            env[m.group(1)] = m.group(2)
    return env


def set_conf(changes):
    """Set (value) or remove (None) Environment lines in rift-s.conf, keeping lines in place."""
    out = []
    done = set()
    for ln in read_text(RIFT_S_CONF).splitlines():
        m = re.match(r'\s*Environment="?([A-Za-z0-9_]+)=', ln)
        key = m.group(1) if m else None
        if key in changes:
            if changes[key] is not None and key not in done:
                out.append(f'Environment="{key}={changes[key]}"')
            done.add(key)
            continue
        out.append(ln)
    for k, v in changes.items():
        if v is not None and k not in done:
            out.append(f'Environment="{k}={v}"')
    RIFT_S_CONF.write_text("\n".join(out) + "\n")


def power_profile():
    """Active power profile as KDE shows it (performance / balanced / power-saver)."""
    rc, out, _ = run(["busctl", "--system", "get-property", "org.freedesktop.UPower.PowerProfiles",
                      "/org/freedesktop/UPower/PowerProfiles", "org.freedesktop.UPower.PowerProfiles",
                      "ActiveProfile"], timeout=3)
    m = re.match(r's "(.*)"', out.strip())
    if m:
        return m.group(1)
    return {"low-power": "power-saver"}.get(read_text("/sys/firmware/acpi/platform_profile"),
                                            read_text("/sys/firmware/acpi/platform_profile"))


def power_supplies():
    """(charger connected, battery %); (None, None) on a desktop without battery."""
    ac = bat = None
    for d in sorted(Path("/sys/class/power_supply").glob("*")):
        kind = read_text(d / "type")
        if kind in ("Mains", "USB"):
            ac = bool(ac) or read_text(d / "online") == "1"
        elif kind == "Battery" and read_text(d / "scope") != "Device":
            cap = read_text(d / "capacity")
            bat = int(cap) if cap.isdigit() else bat
    if bat is None:
        return None, None
    return bool(ac), bat


def rift_s_sinks():
    rc, out, _ = run(["pactl", "list", "short", "sinks"])
    return sorted({ln.split()[1] for ln in out.splitlines() if "Rift_S" in ln and len(ln.split()) > 1})


def say(text):
    """Speak text on the Rift S speakers (espeak-ng + paplay). Blocking."""
    sinks = rift_s_sinks()
    if not sinks or not shutil.which("espeak-ng"):
        return False
    with tempfile.NamedTemporaryFile(suffix=".wav") as f:
        run(["espeak-ng", "-s", "150", "-w", f.name, text], timeout=10)
        procs = [subprocess.Popen(["paplay", f"--device={s}", f.name]) for s in sinks]
        for p in procs:
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
    return True


def fmt_age(ms):
    if ms is None or ms < 0:
        return "never"
    if ms < 1000:
        return f"{ms:.0f} ms"
    if ms < 120000:
        return f"{ms / 1000:.1f} s"
    return f"{ms / 60000:.0f} min"


def fmt_duration(s):
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


# ----------------------------------------------------------------------------------------------
# libmonado: one persistent IPC connection for the client list and recentering


class MonadoLink:
    PRIMARY, ACTIVE, VISIBLE, FOCUSED, OVERLAY = 1, 2, 4, 8, 16

    def __init__(self):
        self.lock = threading.Lock()
        self.root = None
        self.lib = None
        try:
            lib = ctypes.CDLL(str(LIBMONADO))
        except OSError:
            return
        P, U32P = ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)
        sigs = {
            "mnd_root_create": [ctypes.POINTER(P)],
            "mnd_root_destroy": [ctypes.POINTER(P)],
            "mnd_root_update_client_list": [P],
            "mnd_root_get_number_clients": [P, U32P],
            "mnd_root_get_client_id_at_index": [P, ctypes.c_uint32, U32P],
            "mnd_root_get_client_name": [P, ctypes.c_uint32, ctypes.POINTER(ctypes.c_char_p)],
            "mnd_root_get_client_state": [P, ctypes.c_uint32, U32P],
            "mnd_root_recenter_local_spaces": [P],
        }
        for name, args in sigs.items():
            fn = getattr(lib, name)
            fn.argtypes = args
            fn.restype = None if name == "mnd_root_destroy" else ctypes.c_int
        self.lib = lib

    def close(self):
        with self.lock:
            self._close()

    def _close(self):
        if self.root is not None:
            self.lib.mnd_root_destroy(ctypes.byref(self.root))
            self.root = None

    def _ensure(self):
        if self.lib is None:
            return False
        if self.root is None:
            root = ctypes.c_void_p()
            if self.lib.mnd_root_create(ctypes.byref(root)) != 0 or not root:
                return False
            self.root = root
        return True

    def clients(self):
        """List of client dicts, None when not connected. Call only while monado.service is active."""
        with self.lock:
            if not self._ensure():
                return None
            lib, root = self.lib, self.root
            if lib.mnd_root_update_client_list(root) != 0:
                self._close()  # Monado went away; the caller reconnects next time it is active
                return None
            n = ctypes.c_uint32()
            lib.mnd_root_get_number_clients(root, ctypes.byref(n))
            res = []
            for i in range(n.value):
                cid, flags, name = ctypes.c_uint32(), ctypes.c_uint32(), ctypes.c_char_p()
                if lib.mnd_root_get_client_id_at_index(root, i, ctypes.byref(cid)) != 0:
                    continue
                lib.mnd_root_get_client_name(root, cid.value, ctypes.byref(name))
                lib.mnd_root_get_client_state(root, cid.value, ctypes.byref(flags))
                f = flags.value
                res.append({"id": cid.value, "name": (name.value or b"?").decode(errors="replace"),
                            "act": bool(f & self.ACTIVE), "disp": bool(f & self.VISIBLE),
                            "foc": bool(f & self.FOCUSED), "ovly": bool(f & self.OVERLAY)})
            return res

    def recenter(self):
        with self.lock:
            if not self._ensure():
                return False
            return self.lib.mnd_root_recenter_local_spaces(self.root) == 0


# ----------------------------------------------------------------------------------------------
# Monado journal (incremental, per service invocation)


class JournalTail:
    def __init__(self):
        self.invocation = None
        self.cursor = None
        self.reset()

    def reset(self):
        self.missed = collections.deque()   # poll times of "missed frame" lines
        self.slam = collections.deque()     # poll times of Basalt warnings
        self.slam_total = 0
        self.errors = {}                    # serious pattern -> first matching line
        self.minor = {}                     # minor pattern -> first matching line
        self.queue_priority = None
        self.lens_warning = False

    def update(self, invocation, start_ts):
        if invocation != self.invocation:
            self.invocation = invocation
            self.cursor = None
            self.reset()
        if not invocation:
            return
        cmd = ["journalctl", "--user", "-u", "monado.service", "-o", "cat", "--no-pager", "--show-cursor"]
        initial = self.cursor is None
        if initial:
            cmd += ["--since", f"@{int(start_ts)}"]
        else:
            cmd += ["--after-cursor", self.cursor]
        rc, out, _ = run(cmd, timeout=10)
        now = time.monotonic()
        for line in out.splitlines():
            if line.startswith("-- cursor: "):
                self.cursor = line[len("-- cursor: "):]
                continue
            if "Compositor probably missed frame" in line:
                if not initial:
                    self.missed.append(now)
            elif SLAM_WARN_RE.search(line):
                self.slam_total += 1
                if not initial:
                    self.slam.append(now)
            elif "QUEUE_GLOBAL_PRIORITY_" in line and "Created device" in line:
                self.queue_priority = line.split("QUEUE_GLOBAL_PRIORITY_")[1].split()[0].rstrip(".")
            elif "Could not read the lens calibration" in line:
                self.lens_warning = True
            for pats, store in ((SERIOUS_ERRORS, self.errors), (MINOR_ERRORS, self.minor)):
                for pat in pats:
                    if pat in line and pat not in store:
                        store[pat] = line.strip()
        for dq, window in ((self.missed, 10), (self.slam, 60)):
            while dq and dq[0] < now - window:
                dq.popleft()


# ----------------------------------------------------------------------------------------------
# Collector: all checks, run in a worker thread


class Collector:
    SLOW_INTERVAL = 3.0

    def __init__(self):
        self.journal = JournalTail()
        self.link = MonadoLink()
        self.cpu_prev = {}            # pid -> (cpu_s, t)
        self.slow_at = 0.0
        self.slow_cache = {}
        self.ctrl_hist = {"left": collections.deque(), "right": collections.deque()}
        self.ctrl_battery = {"left": None, "right": None}
        self.head_hist = collections.deque()
        self.tracker_hist = collections.deque()  # (t, received, processed, long_run) from the status file
        self.head_test = None         # Check from the last head stillness test

    # -- helpers --------------------------------------------------------------------------

    def cpu_cores(self, pid):
        """CPU use of a process in cores since the previous call for that pid."""
        now = time.monotonic()
        cur = proc_cpu_seconds(pid)
        if cur is None:
            self.cpu_prev.pop(pid, None)
            return None
        prev = self.cpu_prev.get(pid)
        self.cpu_prev[pid] = (cur, now)
        if prev is None or now - prev[1] <= 0:
            return None
        return (cur - prev[0]) / (now - prev[1])

    def service(self):
        props = unit_props("monado.service", "ActiveState", "SubState", "MainPID", "InvocationID",
                           "ActiveEnterTimestamp")
        start = props.get("ActiveEnterTimestamp", "")
        start_ts = float(start[1:]) if start.startswith("@") else 0.0
        rc, sock, _ = run(["systemctl", "--user", "is-active", "monado.socket"])
        return {
            "state": props.get("ActiveState", "unknown"),
            "sub": props.get("SubState", ""),
            "pid": int(props.get("MainPID", "0") or 0),
            "invocation": props.get("InvocationID", ""),
            "start_ts": start_ts,
            "socket": sock.strip(),
        }

    def wayvr_service(self):
        props = unit_props("wayvr.service", "LoadState", "ActiveState", "SubState", "UnitFileState",
                           "MainPID", "Result")
        return {
            "installed": props.get("LoadState") == "loaded",
            "state": props.get("ActiveState", "unknown"),
            "enabled": props.get("UnitFileState") == "enabled",
            "pid": int(props.get("MainPID", "0") or 0),
            "result": props.get("Result", ""),
            "desktop": WAYVR_MODE_CONF.exists(),
            "manual_pids": self.slow_cache.get("wayvr_manual", []),
        }

    # -- main entry -----------------------------------------------------------------------

    def collect(self):
        t0 = time.monotonic()
        svc = self.service()
        active = svc["state"] == "active"
        svc["active"] = active
        svc["uptime"] = time.time() - svc["start_ts"] if active and svc["start_ts"] else 0
        svc["cpu"] = self.cpu_cores(svc["pid"]) if active and svc["pid"] else None

        self.journal.update(svc["invocation"] if active else None, svc["start_ts"])

        if t0 - self.slow_at >= self.SLOW_INTERVAL:
            self.slow_at = t0
            self.slow_cache = {
                "usb": self.check_usb(),
                "suspend": self.check_suspend(svc) if active else None,
                "sensors": self.read_sensors(),
                "getcap": run(["getcap", MONADO_BIN])[1],
                "steam_env": self.steam_env(),
                "wayvr_manual": wayvr_manual_pids(),
                "profile": power_profile(),
            }
        if active:
            clients = self.link.clients()
        else:
            self.link.close()
            clients = None
        clients = self.annotate_clients(clients or [])
        wayvr = self.wayvr_service()
        status = self.read_status_file()

        sections = collections.OrderedDict()
        tracking = self.tracking(svc, status)
        sections["Hardware"] = self.hardware(active)
        laptop, metrics = self.laptop(svc, clients)
        sections["Power / thermals"] = laptop
        sections["Monado"] = self.monado(svc)
        sections["Clients"] = self.client_checks(svc, clients, wayvr)
        sections["Steam / xrizer"] = self.steam(svc, clients)
        sections["Tracking"] = tracking["checks"]

        metrics["missed_per_s"] = len(self.journal.missed) / 10.0 if active else None
        metrics["monado_cpu"] = svc["cpu"]
        snap = {
            "time": time.time(),
            "service": svc,
            "wayvr": wayvr,
            "sections": sections,
            "cards": tracking["cards"],
            "clients": clients,
            "metrics": metrics,
        }
        snap["dashboard"] = self.dashboard(snap)
        snap["collect_ms"] = (time.monotonic() - t0) * 1000
        return snap

    def annotate_clients(self, clients):
        for c in clients:
            low = c["name"].lower()
            c["probe"] = any(p in low for p in PROBE_NAMES)
            c["wayvr"] = "wayvr" in low or "wlx" in low
            c["game"] = not c["probe"] and not c["wayvr"]
            c["title"] = GAME_NAMES.get(c["name"], c["name"])
            c["pid"] = 0
            c["cpu"] = None
            if c["game"]:
                pids = find_pids(lambda pid, comm, argv, exe, n=c["name"][:15]: comm == n)
                if pids:
                    c["pid"] = pids[0]
                    c["cpu"] = self.cpu_cores(pids[0])
        return clients

    # -- dashboard ------------------------------------------------------------------------

    def dashboard(self, snap):
        svc, cards, clients = snap["service"], snap["cards"], snap["clients"]
        usb = self.slow_cache.get("usb") or {"ids": set(), "hidraw": [], "dp": []}
        game = next((c for c in clients if c["game"]), None)
        playing = game is not None
        headset = self.row_headset(svc, usb, cards["head"])
        rows = [headset,
                self.row_controller("left", svc, cards["left"], playing),
                self.row_controller("right", svc, cards["right"], playing),
                self.row_monado(svc, game),
                self.row_wayvr(svc, snap["wayvr"], clients),
                self.row_laptop(svc, playing)]
        if headset.state == RED and "2833:0051" not in usb["ids"]:
            for r in rows[1:3]:
                r.state, r.status, r.detail, r.info, r.todo = GREY, "Waiting for headset", "", [], ""
        return rows

    def row_headset(self, svc, usb, head):
        r = Row("headset", "headset", "VR headset", "Oculus Rift S", GREEN, "Connected")
        ids = usb["ids"]
        if "2833:0051" not in ids:
            r.state, r.status = RED, "Not connected"
            r.info = ["The Rift S does not show up on USB."]
            r.todo = "Check the USB plug (a USB 3 port, directly or via a USB-C to USB-A adapter) and the DisplayPort cable / adapter."
            return r
        if "2833:2051" not in ids:
            r.state, r.status = RED, "USB 2 only"
            r.info = ["The headset came up without its USB 3 hub, so the cameras cannot work."]
            r.todo = "Replug the USB-A plug firmly (happens after reboots), then Restart all."
            r.actions = [("Restart all", "restart_all")]
            return r
        bad = [d for d, ok in usb["hidraw"] if not ok]
        if bad:
            r.state, r.status = RED, "No permission"
            r.info = [f"No access to {', '.join(bad)}."]
            r.todo = "Install the Fedora packages on the Setup tab (xr-hardware has the udev rules), then replug the headset."
            return r
        if not svc["active"]:
            r.status, r.detail = "Connected", "Idle until a game starts Monado"
            return r
        r.info.append(f"Monado running for {fmt_duration(svc['uptime'])}.")
        if "2833:4051" not in ids:
            r.state, r.status = RED, "Cameras missing"
            r.info.append("Monado runs, but the camera device is missing: no tracking.")
            r.todo = "Replug the USB-A plug, then Restart all."
            r.actions = [("Restart all", "restart_all")]
            return r
        if not usb["dp"]:
            r.state, r.status = RED, "No picture"
            r.info.append("No DisplayPort output is connected while Monado runs.")
            r.todo = "Check the USB-C to DisplayPort adapter and its port, then Restart all."
            r.actions = [("Restart all", "restart_all")]
            return r
        r.info += head["lines"]
        kind = head["extra"].get("kind")
        if kind == "diverged":
            r.state, r.status = RED, "Tracking lost"
            r.info.append("The head position runs away (SLAM diverged): the world floats off.")
            r.todo = "Restart all, then restart the game. Start Monado with the headset on your head."
            r.actions = [("Restart all", "restart_all")]
            r.speak = "Head tracking lost. Restart all."
        elif kind == "nodata":
            r.state, r.status = YELLOW, "No tracking data"
            r.todo = ("The installed Monado does not write the status file (patch 0008). "
                      "Rebuild Monado on the Setup tab.")
        elif kind == "drifting":
            r.state, r.status = YELLOW, "Drifting"
            r.info.append("The headset is not worn, but its position keeps moving.")
            r.todo = "If this continues, Restart all (seen before head tracking ran away)."
            r.actions = [("Restart all", "restart_all")]
        elif kind == "3dof":
            r.state, r.status = YELLOW, "Rotation only"
            r.todo = "Head tracking gives no position. Restart all."
            r.actions = [("Restart all", "restart_all")]
        elif kind == "tracked":
            r.status = "Tracking"
            r.detail = "On your head" if head["extra"].get("worn") else "Not worn (display off)"
        else:
            r.state, r.status = GREY, "Starting"
        return r

    def row_controller(self, side, svc, card, playing):
        name = f"Controller {side[0].upper()}"
        r = Row(side, f"controller_{side}", f"Controller ({side})", name, GREEN, "Tracking")
        bat = card["battery"] if card["battery"] is not None else self.ctrl_battery[side]
        bat_txt = f"Battery ~{bat} %" if bat is not None else ""
        r.detail = bat_txt
        r.info = list(card["lines"])
        kind = card["extra"].get("kind")
        if not svc["active"]:
            r.state, r.status = GREY, "Idle"
            r.detail = "Connects when Monado runs" + (f" · last battery ~{bat} %" if bat is not None else "")
            r.info = []
            return r
        if kind in ("nodata", None):
            r.state, r.status = GREY, "No data"
            return r
        if kind in ("offline", "lost_conn"):
            r.state = YELLOW if playing else GREY
            r.status = "Asleep" if kind == "offline" else "Disconnected"
            r.todo = "Press any button to wake it. If it stays dead, replace the AA battery."
            if kind == "lost_conn" and bat is not None and bat <= 10:
                r.state, r.status = RED, "Battery empty"
                r.todo = "Replace the AA battery."
                r.speak = f"{side} controller battery empty."
            return r
        if bat is not None and bat <= 10:
            r.state, r.status = RED, f"Battery empty ({bat} %)"
            r.todo = "Replace the AA battery now: it will drop out soon (shows as IMU gaps first)."
            r.speak = f"{side} controller battery empty."
            return r
        if bat is not None and bat <= 20:
            r.state, r.status = YELLOW, f"Battery low ({bat} %)"
            r.todo = "Have a fresh AA battery ready."
            return r
        if kind == "imu_gaps":
            r.state, r.status = YELLOW, "Weak signal"
            r.todo = "Motion data has gaps: weak battery or radio interference. Try a fresh AA battery."
        elif kind in ("searching", "lost"):
            r.state = YELLOW if playing else GREY
            r.status = "Not tracked"
            r.todo = ("The cameras cannot see it: bring it in front of the headset. If it stays lost while "
                      "in view, put it down for a second, or Restart all.")
        elif kind == "unstable":
            r.state = YELLOW if playing else GREEN
            r.status = "Unstable" if playing else "Tracking"
            r.todo = "Tracking keeps correcting itself (wrong LED matches). Usually recovers by itself."
        else:
            r.status = "Tracking" if kind == "tracked" else "Tracking (IMU)"
        return r

    def row_monado(self, svc, game):
        r = Row("monado", "monado", "Monado service", "OpenXR runtime", GREEN, "Running")
        j = self.journal
        sock = svc["socket"]
        if not MONADO_BIN.exists():
            r.state, r.status = GREY, "Not installed"
            r.todo = "Open the Setup tab."
            return r
        if sock != "active":
            r.state, r.status = RED, "Not available"
            r.info = [f"monado.socket is {sock}: games cannot start Monado."]
            r.todo = "Restart Monado."
            r.actions = [("Restart Monado", "restart_monado")]
            return r
        problems = self.runtime_problems()
        if problems:
            r.state, r.status = RED, "Games can't find it"
            r.info = problems
            r.todo = "Run 'Connect everything' on the Setup tab, then restart the game."
            return r
        if svc["state"] == "failed":
            r.state, r.status = RED, "Crashed"
            r.info = [f"monado.service failed ({svc['sub']})."] + list(j.errors.values())[:3]
            r.todo = "Restart all."
            r.actions = [("Restart all", "restart_all")]
            r.speak = "Monado crashed. Restart all."
            return r
        if svc["state"] in ("activating", "deactivating", "reloading"):
            r.state, r.status = YELLOW, svc["state"].capitalize()
            return r
        if not svc["active"]:
            r.state, r.status = GREY, "Stopped"
            r.detail = "Starts automatically when a game launches"
            r.actions = [("Start Monado", "start_monado")]
            return r
        r.detail = f"{game['title']} running" if game else f"No game · up {fmt_duration(svc['uptime'])}"
        cpu = svc["cpu"]
        r.info = [f"Running for {fmt_duration(svc['uptime'])}" + (f", {cpu:.1f} CPU cores" if cpu else "") + "."]
        if game:
            st = "active" if game["act"] and game["foc"] else "attached, not active (loading?)"
            r.info.append(f"Game: {game['title']} ({st}).")
        events = self.slow_cache.get("suspend") or []
        if j.errors:
            r.state, r.status = RED, "Error"
            r.info += list(j.errors.values())[:3]
            r.todo = "Restart all, then restart the game."
            r.actions = [("Restart all", "restart_all")]
            r.speak = "Monado has a problem. Restart all."
        elif any("suspend entry" in e or "sleep operation" in e for e in events):
            r.state, r.status = RED, "Lost display after sleep"
            r.info.append("The laptop suspended while Monado ran: the headset stays black.")
            r.todo = "Restart all."
            r.actions = [("Restart all", "restart_all")]
        elif len(j.missed) / 10.0 > 5:
            r.state, r.status = YELLOW, "Stuttering"
            r.info.append(f"{len(j.missed) / 10:.1f} missed frames per second (last 10 s).")
            r.todo = "Check the Laptop row (power profile, temperature); close other heavy programs."
        if not game:
            r.actions = r.actions or [("Stop Monado", "stop_monado")]
            if r.state == GREEN:
                r.todo = "Monado keeps ~3 CPU cores busy: stop it when you are done playing."
        return r

    def runtime_problems(self):
        out = []
        target = ACTIVE_RUNTIME.resolve() if ACTIVE_RUNTIME.exists() else None
        if target != MONADO_MANIFEST.resolve():
            ok = False
            try:
                lib = json.loads(target.read_text())["runtime"]["library_path"]
                ok = str(MONADO) in str((target.parent / lib).resolve())
            except (OSError, ValueError, KeyError, AttributeError):
                pass
            if not ok:
                out.append(f"OpenXR runtime points to {target or 'nothing'}: "
                           f"ln -sf {MONADO_MANIFEST} {ACTIVE_RUNTIME}")
        try:
            rt = json.loads(VRPATH.read_text()).get("runtime") or []
        except (OSError, ValueError):
            rt = []
        if not rt or Path(rt[0]).resolve() != XRIZER.resolve():
            out.append(f"SteamVR took over the OpenVR runtime ({rt[0] if rt else 'none'}): "
                       f"put {XRIZER} first in {VRPATH}")
        return out

    def row_wayvr(self, svc, wv, clients):
        r = Row("wayvr", "wayvr", "WayVR service", "In-VR menu & overlays", GREEN, "Running")
        client = next((c for c in clients if c["wayvr"]), None)
        mode = "desktop capture" if wv["desktop"] else "headless"
        if not wv["installed"]:
            r.state = YELLOW if svc["active"] else GREY
            r.status = "Not set up"
            r.info = [f"{WAYVR_UNIT} is missing."]
            r.todo = "Run 'Connect everything' on the Setup tab."
            return r
        r.info = [f"wayvr.service: {wv['state']}, {'starts with Monado' if wv['enabled'] else 'manual start'}, "
                  f"{mode}."]
        if wv["manual_pids"]:
            r.info.append(f"Also a WayVR started by hand (pid {', '.join(map(str, wv['manual_pids']))}).")
        if not svc["active"]:
            r.state = GREY
            r.status = "Waiting for Monado" if wv["enabled"] else "Off"
            r.detail = "Starts together with Monado" if wv["enabled"] else "Not started with Monado"
            if not wv["enabled"]:
                r.actions = [("Start with Monado", "wayvr_enable")]
            return r
        if wv["state"] == "failed":
            r.state, r.status = RED, "Crashed"
            r.info.append(f"Result: {wv['result']}. See the WayVR log (Tools tab).")
            r.todo = "Restart WayVR."
            r.actions = [("Restart WayVR", "restart_wayvr")]
            return r
        if client is None:
            if wv["state"] in ("active", "activating") or wv["manual_pids"]:
                r.state, r.status = YELLOW, "Connecting"
                r.todo = "If this does not turn green within a few seconds, restart WayVR."
                r.actions = [("Restart WayVR", "restart_wayvr")]
            else:
                r.state, r.status = YELLOW, "Not running"
                r.todo = "Start WayVR to get the in-VR menu, watch and playspace tools."
                r.actions = [("Start WayVR", "start_wayvr")]
            return r
        if not (client["act"] and client["ovly"]):
            r.state, r.status = YELLOW, "Not visible"
            r.info.append("Connected to Monado, but its overlay session is not active (stuck, e.g. after sleep).")
            r.todo = "Restart WayVR."
            r.actions = [("Restart WayVR", "restart_wayvr")]
            return r
        r.detail = f"Overlay active · {mode}"
        r.actions = [("Restart WayVR", "restart_wayvr")]
        return r

    def row_laptop(self, svc, playing):
        ac, bat = power_supplies()
        laptop = bat is not None
        r = Row("laptop", "laptop", "Laptop" if laptop else "Computer", "Power & temperature", GREEN, "Ready")
        prof = self.slow_cache.get("profile", "")
        tctl = (self.slow_cache.get("sensors") or {}).get("tctl")
        nice = {"performance": "Performance", "balanced": "Balanced", "power-saver": "Power saver"}
        parts = [nice.get(prof, prof)] if prof else []
        if laptop:
            parts.append("charger" if ac else f"battery {bat} %")
        if tctl is not None:
            parts.append(f"{tctl:.0f} °C")
        r.detail = " · ".join(parts)
        r.info = [f"Power profile: {nice.get(prof, prof) or 'not available'}." +
                  (f" Charger: {'connected' if ac else 'no'}." if laptop else "") +
                  (f" CPU: {tctl:.0f} °C." if tctl is not None else "")]
        if prof and prof != "performance":
            r.state = RED if svc["active"] and laptop else YELLOW
            r.status = nice.get(prof, prof)
            r.info.append("Balanced / power saver can hold the GPU clock down (on an AMD laptop iGPU: "
                          "~600 MHz, games at ~1 fps).")
            r.todo = "Switch to Performance before playing (KDE may restore power saver after a reboot)."
            r.actions = [("Set Performance", "set_performance")]
        elif tctl is not None and tctl > 95:
            r.state, r.status = RED, "Overheating"
            r.todo = "Let it cool down; make sure the vents are free."
        elif tctl is not None and tctl > 90:
            r.state, r.status = YELLOW, "Hot"
            r.todo = "Normal in heavy games (~80-90 °C), but keep the vents free."
        elif laptop and not ac:
            r.state, r.status = YELLOW, "On battery"
            r.todo = "Connect the charger: the power budget drops on battery."
        else:
            r.status = "Ready"
        return r

    # -- hardware -------------------------------------------------------------------------

    def check_usb(self):
        rc, out, _ = run(["lsusb"])
        ids = set(re.findall(r"ID ([0-9a-f]{4}:[0-9a-f]{4})", out))
        hidraw = []
        for h in sorted(Path("/sys/class/hidraw").glob("hidraw*")):
            uevent = read_text(h / "device/uevent")
            if "HID_NAME=Oculus VR Rift S" in uevent:
                dev = Path("/dev") / h.name
                hidraw.append((str(dev), os.access(dev, os.R_OK | os.W_OK)))
        dp = []
        for st in sorted(Path("/sys/class/drm").glob("card*-DP-*/status")):
            if read_text(st) == "connected":
                dp.append(st.parent.name.split("-", 1)[1])
        return {"ids": ids, "hidraw": hidraw, "dp": dp}

    def hardware(self, active):
        usb = self.slow_cache.get("usb") or {"ids": set(), "hidraw": [], "dp": []}
        ids = usb["ids"]
        checks = []
        on_usb = "2833:0051" in ids and "2833:2052" in ids
        checks.append(Check("hw.usb", "Headset on USB", GREEN if on_usb else RED,
                            "Rift S + hub found" if on_usb else "not found",
                            "" if on_usb else "Check the USB cable (needs a USB 3 port)"))
        if on_usb:
            usb3 = "2833:2051" in ids
            checks.append(Check("hw.usb3", "USB 3 link", GREEN if usb3 else RED,
                                "5 Gbit hub present" if usb3 else "USB 2 only",
                                "" if usb3 else "Replug the USB-A plug firmly (seen after reboots)"))
        cams = "2833:4051" in ids
        if active:
            checks.append(Check("hw.cams", "Cameras", GREEN if cams else RED,
                                "Rift S Sensor present" if cams else "missing",
                                "" if cams else "Monado runs but the camera device is missing: replug USB, restart Monado"))
        else:
            checks.append(Check("hw.cams", "Cameras", GREY, "appear once Monado runs"))
        if usb["hidraw"]:
            bad = [d for d, ok in usb["hidraw"] if not ok]
            checks.append(Check("hw.hidraw", "hidraw access", RED if bad else GREEN,
                                f"{len(usb['hidraw'])} nodes, {'no access: ' + ', '.join(bad) if bad else 'user ACL ok'}",
                                "Install the xr-hardware udev rules, replug" if bad else ""))
        else:
            checks.append(Check("hw.hidraw", "hidraw access", GREY if not on_usb else RED, "no Rift S hidraw nodes"))
        if active:
            checks.append(Check("hw.dp", "DisplayPort", GREEN if usb["dp"] else RED,
                                ", ".join(usb["dp"]) + " connected" if usb["dp"] else "no DP connector connected",
                                "" if usb["dp"] else "Check the USB-C -> DP adapter / slot"))
        else:
            checks.append(Check("hw.dp", "DisplayPort", GREY, "connects once Monado powers the headset"))
        return checks

    # -- laptop ---------------------------------------------------------------------------

    def read_sensors(self):
        rc, out, _ = run(["sensors", "-j"])
        res = {}
        try:
            data = json.loads(out)
        except ValueError:
            return res
        for chip, vals in data.items():
            if chip.startswith("k10temp") and "Tctl" in vals:
                res["tctl"] = vals["Tctl"].get("temp1_input")
            elif chip.startswith("coretemp") and "Package id 0" in vals and "tctl" not in res:
                res["tctl"] = vals["Package id 0"].get("temp1_input")
            if chip.startswith("amdgpu") and "PPT" in vals:
                ppt = vals["PPT"]
                res["ppt"] = ppt.get("power1_average", ppt.get("power1_input"))
        return res

    def gpu(self):
        for dev in sorted(Path("/sys/class/drm").glob("card[0-9]/device")):
            sclk = read_text(dev / "pp_dpm_sclk")
            if not sclk:
                continue
            mhz = None
            for line in sclk.splitlines():
                if line.strip().endswith("*"):
                    m = re.search(r"(\d+)\s*Mhz", line, re.I)
                    mhz = int(m.group(1)) if m else None
            busy = read_text(dev / "gpu_busy_percent")
            return mhz, int(busy) if busy.isdigit() else None
        return None, None

    def check_suspend(self, svc):
        if not svc["start_ts"]:
            return None
        rc, out, _ = run(["journalctl", "-b", "--since", f"@{int(svc['start_ts'])}", "-o", "short", "-q",
                          "--no-pager", "-g", "Lid closed|PM: suspend entry|Performing sleep operation"], timeout=8)
        return [ln for ln in out.splitlines() if ln.strip()]

    def laptop(self, svc, clients):
        checks = []
        metrics = {}
        prof = self.slow_cache.get("profile", "")
        if prof:
            checks.append(Check("pw.profile", "Power profile", GREEN if prof == "performance" else YELLOW, prof,
                                "" if prof == "performance" else
                                "Set Performance (otherwise the GPU clock may be held down)"))
        else:
            checks.append(Check("pw.profile", "Power profile", GREY, "power-profiles-daemon not available"))
        ac, bat = power_supplies()
        if bat is not None:
            checks.append(Check("pw.ac", "AC power", GREEN if ac else YELLOW,
                                ("connected" if ac else "on battery") + f", battery {bat} %",
                                "" if ac else "Connect the charger: the power budget drops on battery"))
        sens = self.slow_cache.get("sensors") or {}
        tctl = sens.get("tctl")
        metrics["tctl"] = tctl
        if tctl is not None:
            st = GREEN if tctl < 80 else YELLOW if tctl <= 90 else RED
            checks.append(Check("pw.tctl", "CPU temperature", st, f"{tctl:.0f} °C",
                                "" if st == GREEN else "80-90 °C is normal in heavy games"))
        ppt = sens.get("ppt")
        if ppt is not None:
            checks.append(Check("pw.ppt", "APU power", GREY, f"{ppt:.1f} W  (cap ~25 W balanced, ~30 W performance)"))
        mhz, busy = self.gpu()
        metrics["gpu_mhz"] = mhz
        if mhz is not None:
            game = any(c["game"] and c["act"] for c in clients)
            stuck = game and mhz < 800
            checks.append(Check("pw.gpu", "GPU", YELLOW if stuck else GREY,
                                f"{mhz} MHz, {busy if busy is not None else '?'} % busy",
                                "GPU clock stuck low while a game runs: check the power profile" if stuck else ""))
        if svc["active"]:
            events = self.slow_cache.get("suspend")
            if events is None:
                pass
            elif any("suspend entry" in e or "sleep operation" in e for e in events):
                checks.append(Check("pw.suspend", "Suspend since Monado start", RED, "laptop suspended",
                                    "Monado lost the headset display: Restart all"))
            elif events:
                checks.append(Check("pw.suspend", "Suspend since Monado start", YELLOW, "lid was closed",
                                    "Lid sensor triggered (headset magnets?). Enable 'Block lid/sleep' in Tools"))
            else:
                checks.append(Check("pw.suspend", "Suspend since Monado start", GREEN, "none"))
        return checks, metrics

    # -- Monado ---------------------------------------------------------------------------

    def monado(self, svc):
        checks = []
        j = self.journal
        sock = svc["socket"]
        checks.append(Check("mo.socket", "Socket", GREEN if sock == "active" else RED, sock,
                            "" if sock == "active" else "systemctl --user restart monado.socket"))
        if svc["active"]:
            cpu = svc["cpu"]
            checks.append(Check("mo.service", "Service", GREEN,
                                f"running {fmt_duration(svc['uptime'])}, pid {svc['pid']}" +
                                (f", {cpu:.1f} cores" if cpu is not None else "")))
        elif svc["state"] in ("activating", "deactivating", "reloading"):
            checks.append(Check("mo.service", "Service", YELLOW, svc["state"]))
        elif svc["state"] == "failed":
            checks.append(Check("mo.service", "Service", RED, f"failed ({svc['sub']})",
                                "Restart Monado (does reset-failed)"))
        else:
            checks.append(Check("mo.service", "Service", GREY, "stopped (starts on demand via the socket)"))

        problems = self.runtime_problems()
        checks.append(Check("mo.runtime", "Runtime registration", RED if problems else GREEN,
                            "; ".join(problems) if problems else "OpenXR -> monado-6dof, OpenVR -> xrizer"))

        cap = self.slow_cache.get("getcap", "")
        has_cap = "cap_sys_nice" in cap
        checks.append(Check("mo.setcap", "CAP_SYS_NICE", GREEN if has_cap else YELLOW,
                            "set" if has_cap else "missing (lost on every reinstall)",
                            "" if has_cap else "Setup tab: Realtime GPU priority"))
        if svc["active"] and j.queue_priority:
            rt = j.queue_priority == "REALTIME"
            checks.append(Check("mo.queue", "GPU queue priority", GREEN if rt else YELLOW, j.queue_priority,
                                "" if rt else "Needs CAP_SYS_NICE on monado-service, then restart Monado"))

        env = read_conf()
        broken = [k for k in setup_steps.REQUIRED_ENV if env.get(k) != DEFAULTS[k]]
        diffs = [k for k, v in DEFAULTS.items() if env.get(k) != v and k not in broken]
        val = ", ".join(f"{k}={v}" for k, v in env.items() if k.startswith(("XRT", "U_", "RIFT_S")))
        checks.append(Check("mo.config", "Config", RED if broken else GREEN, val or "no rift-s.conf",
                            ("Required: " + ", ".join(f"{k}={DEFAULTS[k]}" for k in broken) +
                             " (Setup tab: Connect everything)") if broken else
                            ("Changed from the defaults: " + ", ".join(f"{k}={DEFAULTS[k]}" for k in diffs)
                             if diffs else "")))

        if svc["active"]:
            checks.append(Check("mo.lens", "Lens calibration", YELLOW if j.lens_warning else GREEN,
                                "fallback values" if j.lens_warning else "read from firmware"))
            rate = len(j.missed) / 10.0
            st = GREEN if rate < 1 else YELLOW if rate <= 5 else RED
            checks.append(Check("mo.missed", "Missed frames", st, f"{rate:.1f}/s (last 10 s)",
                                "" if st == GREEN else "Head-turn judder: check U_PACING_COMP_MIN_TIME_MS, CPU/GPU load"))
            if j.errors:
                first = next(iter(j.errors.values()))
                checks.append(Check("mo.errors", "Serious errors", RED, ", ".join(j.errors),
                                    f"{first[:160]}  -> Restart all"))
            else:
                checks.append(Check("mo.errors", "Serious errors", GREEN, "none this run"))
            if j.minor:
                checks.append(Check("mo.minor", "Other errors", GREY, ", ".join(j.minor),
                                    next(iter(j.minor.values()))[:160] + "  (seen in healthy sessions too)"))
            checks.append(Check("mo.slam", "SLAM warnings", GREY, f"{len(j.slam)}/min (total {j.slam_total})",
                                "Basalt warnings; bursts also happen without divergence"))
        return checks

    # -- clients --------------------------------------------------------------------------

    def client_checks(self, svc, clients, wv):
        checks = []
        if not svc["active"]:
            return [Check("cl.none", "Clients", GREY, "Monado not running")]
        if self.link.lib is None:
            checks.append(Check("cl.lib", "libmonado", RED, f"cannot load {LIBMONADO}"))
        games = [c for c in clients if c["game"]]
        if not games:
            checks.append(Check("cl.game", "Game", GREY, "none attached"))
        for c in games:
            full = c["act"] and c["disp"] and c["foc"]
            cpu = c["cpu"]
            checks.append(Check(f"cl.game.{c['id']}", f"Game: {c['title']}", GREEN if full else YELLOW,
                                f"act {int(c['act'])} disp {int(c['disp'])} foc {int(c['foc'])}" +
                                (f", pid {c['pid']}" if c["pid"] else "") +
                                (f", {cpu:.1f} cores" if cpu is not None else ""),
                                "" if full else "Attached but not active/focused (loading, or stuck)"))
        wvc = [c for c in clients if c["wayvr"]]
        if wvc:
            c = wvc[0]
            ok = c["ovly"] and c["act"]
            checks.append(Check("cl.wayvr", "WayVR overlay", GREEN if ok else YELLOW,
                                f"overlay {int(c['ovly'])}, active {int(c['act'])}, wayvr.service {wv['state']}",
                                "" if ok else "Attached but not active (stuck, e.g. after suspend): restart WayVR"))
        else:
            checks.append(Check("cl.wayvr", "WayVR overlay", GREY, f"not attached, wayvr.service {wv['state']}"))
        others = [c["name"] for c in clients if c["probe"]]
        if others:
            checks.append(Check("cl.tools", "Tool connections", GREY, ", ".join(others)))
        return checks

    # -- Steam / xrizer -------------------------------------------------------------------

    def steam_env(self):
        pids = find_pids(lambda pid, comm, argv, exe: comm == "steam")
        for pid in pids:
            try:
                env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
            except OSError:
                continue
            for e in env:
                if e.startswith(b"PRESSURE_VESSEL_FILESYSTEMS_RW="):
                    return pid, e.split(b"=", 1)[1].decode(errors="replace")
            return pid, ""
        return None, None

    def steam(self, svc, clients):
        checks = []
        if XRIZER_SO.is_symlink():
            checks.append(Check("st.xrizer", "xrizer binary", YELLOW, "symlink (into the build dir?)",
                                "Reinstall xrizer from the Setup tab (it copies the real file)"))
        elif XRIZER_SO.is_file():
            mt = time.strftime("%Y-%m-%d %H:%M", time.localtime(XRIZER_SO.stat().st_mtime))
            checks.append(Check("st.xrizer", "xrizer binary", GREEN, f"regular file, {mt}"))
        else:
            checks.append(Check("st.xrizer", "xrizer binary", RED, "missing"))

        pid, rw = self.slow_cache.get("steam_env") or (None, None)
        if pid is None:
            checks.append(Check("st.container", "Container socket access", GREY, "Steam not running"))
        else:
            ok = "monado_comp_ipc" in rw
            checks.append(Check("st.container", "Container socket access", GREEN if ok else RED,
                                "PRESSURE_VESSEL_FILESYSTEMS_RW ok" if ok else "monado_comp_ipc not shared",
                                "" if ok else "Re-login (environment.d) or add the variable to the launch options"))

        games = [c for c in clients if c["game"]]
        try:
            st = XRIZER_LOG.stat()
        except OSError:
            st = None
        if st and svc["active"] and st.st_mtime >= svc["start_ts"] - 120:
            tail = self.log_tail(XRIZER_LOG)
            bad = [ln for ln in tail if "panicked at" in ln or "not yet implemented" in ln]
            if bad:
                checks.append(Check("st.xrlog", "xrizer errors", RED, bad[-1][-140:],
                                    "Unimplemented OpenVR call or crash: see ~/.local/state/xrizer/xrizer.txt"))
            else:
                checks.append(Check("st.xrlog", "xrizer errors", GREEN, "none in this run"))
            # Alyx grey-room hang detector
            last = next((ln for ln in reversed(tail) if ln.strip()), "")
            silent = time.time() - st.st_mtime
            for c in games:
                if "recreating swapchain" not in last or silent < 60:
                    continue
                if c["cpu"] is not None and c["cpu"] < 0.5:
                    checks.append(Check("st.hang", "Game hang", YELLOW,
                                        f"{c['title']}: log silent {fmt_duration(silent)} after 'recreating swapchain'",
                                        "Probably stuck loading (grey room): Restart all + restart the game"))
        else:
            checks.append(Check("st.xrlog", "xrizer errors", GREY, "no xrizer game this run"))
        return checks

    @staticmethod
    def log_tail(path, size=200000):
        try:
            with open(path, "rb") as f:
                f.seek(0, 2)
                n = f.tell()
                f.seek(max(0, n - size))
                return f.read().decode(errors="replace").splitlines()
        except OSError:
            return []

    # -- tracking (driver status file) ----------------------------------------------------

    def read_status_file(self):
        try:
            st = STATUS_FILE.stat()
            data = json.loads(STATUS_FILE.read_text())
            data["_age_s"] = time.time() - st.st_mtime
            return data
        except (OSError, ValueError):
            return None

    def tracking(self, svc, status):
        checks = [self.head_test or Check("tr.headtest", "Head stillness test", GREY,
                                          "not run yet (Tools tab)")]
        cards = {}
        if not svc["active"]:
            for side in ("left", "right"):
                cards[side] = self.card(GREY, "Monado stopped", [], None)
                self.ctrl_hist[side].clear()
            cards["head"] = self.card(GREY, "Monado stopped", [], None)
            self.head_hist.clear()
            return {"checks": checks, "cards": cards}
        if status is None or status["_age_s"] > 3:
            why = "no status file" if status is None else f"status file stale ({status['_age_s']:.0f} s)"
            starting = svc["uptime"] <= 15
            state = GREY if starting else YELLOW
            hint = "Monado is starting" if starting else \
                "Installed Monado lacks patch 0008 (status file)? Rebuild it on the Setup tab"
            checks.insert(0, Check("tr.status", "Driver status", state, why, hint))
            kind = "starting" if starting else "nodata"
            for side in ("left", "right"):
                cards[side] = self.card(state, "No data", [why], None, {"kind": kind})
            cards["head"] = self.card(state, "No data", [why], None, {"kind": kind})
            return {"checks": checks, "cards": cards}

        checks.insert(0, Check("tr.status", "Driver status", GREEN,
                               f"live ({status['_age_s'] * 1000:.0f} ms old)"))
        checks.insert(1, self.passthrough_check(status.get("passthrough")))
        t = status["time_ns"] / 1e9
        checks.insert(2, self.tracker_check(status.get("tracker"), t))
        cards["head"] = self.eval_head(status, t)
        for side in ("left", "right"):
            cards[side] = self.eval_controller(side, status["controllers"].get(side), t)
        return {"checks": checks, "cards": cards}

    def tracker_check(self, tr, t):
        """Details only: controller camera tracker load (patch 0012)."""
        label = "Controller camera tracker"
        if tr is None:
            self.tracker_hist.clear()
            return Check("tr.ctracker", label, GREY, "no counters in this Monado build (patch 0012)")
        hist = self.tracker_hist
        if not hist or hist[-1][0] != t:
            hist.append((t, tr["frames_received"], tr["frames_processed"], tr["long_run"]))
        while len(hist) > 2 and hist[0][0] < t - 5.0:
            hist.popleft()
        if len(hist) < 2 or hist[-1][0] - hist[0][0] < 0.9:
            return Check("tr.ctracker", label, GREY, "measuring")
        (t0, r0, p0, l0), (t1, r1, p1, l1) = hist[0], hist[-1]
        dt = t1 - t0
        fps, dropped, searches = (r1 - r0) / dt, max(0.0, ((r1 - r0) - (p1 - p0)) / dt), (l1 - l0) / dt
        val = (f"{fps:.0f} frames/s, {dropped:.1f} dropped/s, analysis {tr['fast_ms']:.1f} ms, "
               f"{searches:.0f} full searches/s ({tr['long_ms']:.0f} ms)")
        # received - processed also counts up to 2 frames still queued
        if dropped >= 1.0 and (r1 - r0) - (p1 - p0) > 2:
            return Check("tr.ctracker", label, YELLOW, val,
                         "Tracker falls behind the cameras (CPU busy): controller fixes are skipped")
        return Check("tr.ctracker", label, GREEN, val)

    @staticmethod
    def passthrough_check(pt):
        """Details only: passthrough is a comfort feature, never worth a dashboard colour."""
        label = "Passthrough"
        if pt is None:
            return Check("tr.passthrough", label, GREY, "not in this Monado build (patches 0010/0011)")
        age = pt.get("frame_age_ms", -1)
        if not pt.get("streaming"):
            if pt.get("forced"):
                return Check("tr.passthrough", label, GREY, "on, waiting for the compositor")
            return Check("tr.passthrough", label, GREY, "off (hold the right Oculus button)")
        how = "on (Oculus button)" if pt.get("forced") else "WayVR background"
        if age < 0 or age > 200:
            return Check("tr.passthrough", label, YELLOW, f"{how}, no recent camera images",
                         "Cameras stalled? Restart Monado (Restart all)")
        return Check("tr.passthrough", label, GREEN, f"{how}, image {age:.0f} ms old")

    @staticmethod
    def card(state, title, lines, battery, extra=None):
        return {"state": state, "title": title, "lines": lines, "battery": battery, "extra": extra or {}}

    def eval_head(self, status, t):
        head = status.get("head")
        if head is None:
            return self.card(GREY, "No head pose", [], None, {"kind": "nodata"})
        p = head["position"]
        hist = self.head_hist
        if not hist or hist[-1][0] != t:
            hist.append((t, p))
        while hist and hist[0][0] < t - 1.5:
            hist.popleft()
        speed = 0.0
        if len(hist) >= 2 and hist[-1][0] - hist[0][0] > 0.4:
            (t0, p0), (t1, p1) = hist[0], hist[-1]
            speed = math.dist(p0, p1) / (t1 - t0)
        dist = math.sqrt(sum(v * v for v in p))
        worn = bool(status.get("proximity"))
        lines = [f"Position {p[0]:+.2f} {p[1]:+.2f} {p[2]:+.2f} m ({dist:.2f} m from the centre), "
                 f"moving {speed:.2f} m/s."]
        extra = {"worn": worn}
        if speed > 3.0 or dist > 5.0:
            return self.card(RED, "Head tracking diverged", lines +
                             ["Head pose runs away: restart Monado (Restart all)"], None, dict(extra, kind="diverged"))
        if not worn and speed > 0.5:
            return self.card(YELLOW, "Moving while not worn", lines +
                             ["Headset off the head but its pose keeps moving: SLAM starting to run away?"],
                             None, dict(extra, kind="drifting"))
        if not head.get("position_tracked"):
            return self.card(YELLOW, "3DoF only", lines + ["SLAM not providing positions"], None,
                             dict(extra, kind="3dof"))
        return self.card(GREEN, "Tracked (SLAM)", lines, None, dict(extra, kind="tracked"))

    def eval_controller(self, side, c, t):
        if c is None:
            return self.card(GREY, "No device", ["Controller device not created"], None, {"kind": "nodata"})
        rep = c["report_age_ms"]
        bat = c["battery"] if 0 < c["battery"] <= 100 else None
        if bat is not None:
            self.ctrl_battery[side] = bat
        bat_known = self.ctrl_battery[side]
        hist = self.ctrl_hist[side]
        if not c["paired"] or rep < 0:
            hist.clear()
            return self.card(YELLOW, "Offline", ["Never seen on the radio.", "Press a button to wake it."],
                             None, {"kind": "offline"})
        if rep > 1500:
            hist.clear()
            return self.card(YELLOW, "Connection lost",
                             [f"No radio reports for {fmt_age(rep)}: asleep, switched off or battery dead."],
                             bat_known, {"kind": "lost_conn"})

        if not hist or hist[-1][0] != t:
            hist.append((t, c["imu_samples"], c["fixes_accepted"], c["fixes_rejected"], c["resets"]))
        while len(hist) > 2 and hist[0][0] < t - 5.0:
            hist.popleft()
        imu_hz = fixes = accept = None
        resets = 0
        if len(hist) >= 2 and hist[-1][0] - hist[0][0] >= 0.9:
            (t0, i0, a0, r0, s0), (t1, i1, a1, r1, s1) = hist[0], hist[-1]
            dt = t1 - t0
            imu_hz = (i1 - i0) / dt
            fixes = (a1 - a0 + r1 - r0) / dt
            accept = (a1 - a0) / max(1, a1 - a0 + r1 - r0)
            resets = s1 - s0

        opt = c["optical_age_ms"]
        innov = c["last_innovation_m"]
        lines = []
        if c["position_tracked"]:
            p = c["position"]
            lines.append(f"Position {p[0]:+.2f} {p[1]:+.2f} {p[2]:+.2f} m.")
        stats = f"Last camera fix {fmt_age(opt)} ago" if opt >= 0 else "No camera fix yet"
        if fixes:
            stats += f", {fixes:.0f} fixes/s, {accept * 100:.0f} % accepted"
        lines.append(stats + ".")
        lines.append((f"Motion sensor {imu_hz:.0f} Hz" if imu_hz is not None else "Motion sensor -") +
                     f", last correction {innov * 1000:.0f} mm, {c['resets']} filter resets.")

        if imu_hz is not None and imu_hz < 400:
            state, title, kind = YELLOW, "IMU gaps", "imu_gaps"
            lines.append(f"Motion data at {imu_hz:.0f} Hz instead of ~500 Hz: weak battery?")
        elif not c["fusion_initialised"] and opt < 0:
            state, title, kind = YELLOW, "Searching", "searching"
        elif not c["position_tracked"]:
            state, title, kind = YELLOW, "Lost (rotation only)", "lost"
            lines.append("No accepted camera fix: position frozen, rotation still works.")
        elif resets >= 3 or (fixes and fixes >= 10 and accept < 0.4):
            state, title, kind = YELLOW, "Unstable", "unstable"
            lines.append(f"Last 5 s: {resets} filter resets, "
                         f"{(1 - accept) * 100 if accept is not None else 0:.0f} % camera fixes rejected.")
        elif opt > 250:
            state, title, kind = GREEN, "Coasting on IMU", "coasting"
        else:
            state, title, kind = GREEN, "Tracked", "tracked"
        if bat_known is not None and bat_known <= 10:
            state = RED
        elif bat_known is not None and bat_known <= 20:
            state = max(state, YELLOW)
        return self.card(state, title, lines, bat_known, {"kind": kind})


# ----------------------------------------------------------------------------------------------
# Actions (run in background threads, report through log())


class Actions:
    def __init__(self, log, collector):
        self.log = log
        self.collector = collector
        self.busy = threading.Lock()

    def sh(self, cmd, timeout=30, quiet=False):
        self.log("$ " + " ".join(str(c) for c in cmd))
        rc, out, err = run(cmd, timeout=timeout)
        text = (out + err).strip()
        if text and not quiet:
            self.log("\n".join(text.splitlines()[-25:]))
        if rc != 0:
            self.log(f"  (exit {rc})")
        return rc, out

    @staticmethod
    def service_active(unit="monado.service"):
        return run(["systemctl", "--user", "is-active", unit])[1].strip() == "active"

    def stop_monado(self):
        """Stop Monado (wayvr.service is bound to it and stops too)."""
        self.collector.link.close()
        rc, _ = self.sh(["systemctl", "--user", "stop", "monado.service"], timeout=15, quiet=True)
        if rc != 0 or self.service_active():
            self.log("Monado did not stop, sending SIGKILL")
            self.sh(["systemctl", "--user", "kill", "-s", "KILL", "monado.service"])
            time.sleep(1)
            self.sh(["systemctl", "--user", "stop", "monado.service"], timeout=15)
        self.sh(["systemctl", "--user", "reset-failed", "monado.service", "wayvr.service"], quiet=True)

    def start_monado(self):
        """Start Monado; wayvr.service follows if it is enabled (WantedBy=monado.service)."""
        self.sh(["systemctl", "--user", "restart", "monado.socket"])
        self.sh(["systemctl", "--user", "start", "monado.service"], timeout=30)

    def restart_monado(self):
        self.stop_monado()
        self.start_monado()

    def kill_manual_wayvr(self):
        pids = wayvr_manual_pids()
        if not pids:
            return
        self.log(f"Stopping hand-started WayVR (pids {' '.join(map(str, pids))})")
        for sig in (15, 9):
            for pid in pids:
                try:
                    os.kill(pid, sig)
                except OSError:
                    pass
            for _ in range(30):
                if not any(pid_alive(p) for p in pids):
                    return
                time.sleep(0.1)

    def start_wayvr(self):
        self.kill_manual_wayvr()
        self.sh(["systemctl", "--user", "reset-failed", "wayvr.service"], quiet=True)
        self.sh(["systemctl", "--user", "start", "wayvr.service"], timeout=30)

    def stop_wayvr(self):
        self.kill_manual_wayvr()
        self.sh(["systemctl", "--user", "stop", "wayvr.service"], timeout=20)

    def restart_wayvr(self):
        self.kill_manual_wayvr()
        self.sh(["systemctl", "--user", "reset-failed", "wayvr.service"], quiet=True)
        self.sh(["systemctl", "--user", "restart", "wayvr.service"], timeout=30)

    def wayvr_enable(self, on=True):
        self.sh(["systemctl", "--user", "enable" if on else "disable", "wayvr.service"])
        if on and self.service_active():
            self.start_wayvr()

    def wayvr_desktop(self, on):
        if on:
            WAYVR_MODE_CONF.parent.mkdir(parents=True, exist_ok=True)
            WAYVR_MODE_CONF.write_text("# Written by vrstatus: WayVR with desktop capture\n"
                                       "[Service]\nEnvironment=WAYVR_MODE=\n")
        elif WAYVR_MODE_CONF.exists():
            WAYVR_MODE_CONF.unlink()
        self.log(f"WayVR mode: {'desktop capture' if on else 'headless'}")
        self.sh(["systemctl", "--user", "daemon-reload"])
        if self.service_active("wayvr.service"):
            self.restart_wayvr()

    def restart_all(self):
        self.kill_manual_wayvr()
        self.stop_monado()
        self.start_monado()
        if not run(["systemctl", "--user", "is-enabled", "wayvr.service"])[1].strip() == "enabled":
            self.start_wayvr()
        self.log("Restart all done. Restart the game if one was running.")

    def recenter(self):
        if not self.service_active():
            self.log("Monado is not running")
            return
        if self.collector.link.recenter():
            self.log("Recentered")
        else:
            self.sh([MONADO_CTL, "-c"])

    def set_performance(self):
        self.sh(["busctl", "--system", "set-property", "org.freedesktop.UPower.PowerProfiles",
                 "/org/freedesktop/UPower/PowerProfiles", "org.freedesktop.UPower.PowerProfiles",
                 "ActiveProfile", "s", "performance"])
        self.collector.slow_at = 0  # re-read the profile on the next poll

    def apply_conf(self, changes, restart=True):
        set_conf(changes)
        self.log("rift-s.conf: " + ", ".join(f"{k}={'(removed)' if v is None else v}" for k, v in changes.items()))
        self.sh(["systemctl", "--user", "daemon-reload"])
        if restart:
            if self.service_active():
                self.restart_monado()
                self.log("Monado restarted: restart the game")
            else:
                self.sh(["systemctl", "--user", "restart", "monado.socket"])

    def set_lens(self, value):
        if value == "cv1":
            self.apply_conf({"RIFT_S_DISTORTION_C1": None, "RIFT_S_DISTORTION": "cv1"})
        else:
            self.apply_conf({"RIFT_S_DISTORTION": None, "RIFT_S_DISTORTION_C1": value})

    def toggle_recording(self):
        env = read_conf()
        if "RIFT_S_CTRL_RECORD" in env:
            self.log(f"Recording stopped: {env['RIFT_S_CTRL_RECORD']}")
            self.apply_conf({"RIFT_S_CTRL_RECORD": None})
        else:
            d = RECORDINGS / time.strftime("ctrlrec-%Y%m%d-%H%M%S")
            d.mkdir(parents=True, exist_ok=True)
            self.apply_conf({"RIFT_S_CTRL_RECORD": str(d)})
            self.log(f"Recording controller IMU + optical data to {d}")

    def head_test(self):
        if not self.service_active():
            self.log("Head stillness test needs Monado running")
            return
        self.log("Head stillness test: hold your head still for 10 s")
        say("Hold your head still for ten seconds. Starting now.")
        rc, out = self.sh([BIN / "headrec", "10"], timeout=40, quiet=True)
        pos, rot = [], []
        for line in out.splitlines()[1:]:
            f = line.split(",")
            if len(f) == 10 and f[2] == "0":
                rot.append(tuple(map(float, f[3:7])))
                pos.append(tuple(map(float, f[7:10])))
        if len(pos) < 20:
            self.log("Head test: too few samples")
            self.collector.head_test = Check("tr.headtest", "Head stillness test", YELLOW, "no data")
            return
        disp = max(math.dist(p, pos[0]) for p in pos)
        q0 = rot[0]
        ang = max(math.degrees(2 * math.acos(min(1.0, abs(sum(a * b for a, b in zip(q, q0)))))) for q in rot)
        step = max(math.dist(a, b) for a, b in zip(pos, pos[1:]))
        ok = disp < 0.03 and ang < 1.0 and step < 0.005
        val = f"max {disp * 100:.1f} cm, {ang:.2f}°, step {step * 1000:.1f} mm ({time.strftime('%H:%M')})"
        self.log(f"Head test {'passed' if ok else 'FAILED'}: {val}")
        self.collector.head_test = Check("tr.headtest", "Head stillness test", GREEN if ok else RED, val,
                                         "" if ok else "Head tracking unhealthy (healthy: <3 cm, <1°, <5 mm): Restart all")
        say("Head test passed." if ok else "Head test failed. Restart all.")

    def guided_test(self):
        if not self.service_active():
            self.log("Guided controller test needs Monado running")
            return
        RECORDINGS.mkdir(parents=True, exist_ok=True)
        out = RECORDINGS / time.strftime("guided-%Y%m%d-%H%M%S.csv")
        self.log(f"Guided controller test (45 s, follow the spoken cues) -> {out}")
        self.sh(["env", f"RIFTS_BIN={BIN}", TOOLS / "guided-run.sh", out], timeout=180, quiet=True)
        self.sh([sys.executable, TOOLS / "analyze.py", out], timeout=60)

    def vibration_test(self):
        if not self.service_active():
            self.log("Vibration test needs Monado running")
            return
        self.log("Vibration test (~16 s): left then right controller, 3 strengths, short pulses, continuous buzz")
        rc, out = self.sh([BIN / "hapticstest", "both"], timeout=60, quiet=True)
        if rc != 0:
            self.log("Vibration test could not run: it needs the OpenXR focus, close the game first "
                     f"(details: run {BIN / 'hapticstest'} in a terminal)")
            return
        st = self.collector.read_status_file() or {}
        for side, c in (st.get("controllers") or {}).items():
            h = (c or {}).get("haptic")
            if h:
                self.log(f"Vibration {side}: {h['writes']} motor writes, {h['failed']} failed, "
                         f"radio round trip {h['rtt_ms']:.1f} ms")

    def reach_test(self):
        if not self.service_active():
            self.log("Reach test needs Monado running")
            return
        self.log("Reach test (~1 min, follow the spoken cues): controller positions relative to the head")
        self.sh([sys.executable, TOOLS / "reach-test.py"], timeout=180)

    def ctrl_protocol(self):
        if not self.service_active():
            self.log("Tracking protocol needs Monado running")
            return
        if "RIFT_S_CTRL_RECORD" not in read_conf():
            self.log("Tracking protocol needs a controller recording: press 'Start controller recording' first "
                     "(restarts Monado, so before launching the game)")
            return
        self.log("Tracking protocol starts in 15 s (put the headset on), ~4 min, follow the spoken cues")
        rc, out = self.sh([sys.executable, TOOLS / "dev/ctrl-protocol.py", "--delay", "15"], timeout=440, quiet=True)
        if rc != 0:
            self.log("\n".join(out.strip().splitlines()[-5:]) or "ctrl-protocol.py failed, run it in a terminal")
        else:
            self.sh([sys.executable, TOOLS / "dev/ctrl-analyze.py", read_conf()["RIFT_S_CTRL_RECORD"],
                     "--segments-only"], timeout=180)

    def dump_firmware(self):
        if self.service_active():
            self.log("Stop Monado first (firmware dumps need exclusive hidraw access)")
            return
        dev = None
        for h in sorted(Path("/sys/class/hidraw").glob("hidraw*")):
            if "HID_NAME=Oculus VR Rift S" in read_text(h / "device/uevent"):
                dev = Path("/dev") / h.name   # lowest interface = HMD
                break
        if dev is None:
            self.log("No Rift S hidraw device")
            return
        out = FIRMWARE / time.strftime("dump-%Y%m%d-%H%M%S")
        out.mkdir(parents=True, exist_ok=True)
        self.sh([sys.executable, TOOLS / "dev/dump_fw_blocks.py", dev, out], timeout=120)
        self.log(f"Dumped to {out} (contains serial numbers, keep it out of public repos)")


def open_terminal(cmd):
    """Open a terminal window running cmd (list)."""
    for term in (["konsole", "--hold", "-e"], ["gnome-terminal", "--"], ["xterm", "-hold", "-e"]):
        if shutil.which(term[0]):
            subprocess.Popen(term + [str(c) for c in cmd], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    return False


# ----------------------------------------------------------------------------------------------
# Text report (--once)


def text_report():
    if not setup_steps.setup_complete():
        print("== Setup not finished (rift-s-linux setup install)")
        setup_steps.print_status()
        print()
    col = Collector()
    col.collect()
    time.sleep(1.1)  # second sample for rates
    snap = col.collect()
    col.link.close()
    print("== Dashboard")
    for r in snap["dashboard"]:
        print(f"[{STATE_NAMES[r.state]:>4}] {r.category:16} {r.status}" + (f"  ({r.detail})" if r.detail else ""))
        if r.state >= YELLOW:
            for ln in r.info:
                print(f"         {ln}")
            if r.todo:
                print(f"         -> {r.todo}")
    for sec, checks in snap["sections"].items():
        print(f"\n== {sec}")
        for ch in checks:
            print(f"[{STATE_NAMES[ch.state]:>4}] {ch.label}: {ch.value}" + (f"\n         -> {ch.hint}" if ch.hint else ""))
    print(f"\n(collect {snap['collect_ms']:.0f} ms)")


# ----------------------------------------------------------------------------------------------
# GUI


def main_gui():
    from PySide6 import QtCore, QtGui, QtWidgets
    from PySide6.QtCore import Qt

    class Bridge(QtCore.QObject):
        snapshot = QtCore.Signal(object)
        log = QtCore.Signal(str)
        ask = QtCore.Signal(object)          # {"title", "text", "default"} -> sets "answer"
        setup_state = QtCore.Signal(object)  # [(step, State)]
        action_done = QtCore.Signal()

    def dot_style(state, size=12):
        return (f"background:{COLORS[state]}; border-radius:{size // 2}px; "
                f"min-width:{size}px; max-width:{size}px; min-height:{size}px; max-height:{size}px;")

    def make_icon(state):
        pm = QtGui.QPixmap(64, 64)
        pm.fill(Qt.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.setBrush(QtGui.QColor(COLORS[state]))
        p.setPen(QtGui.QPen(QtGui.QColor("#1f2328"), 3))
        p.drawRoundedRect(4, 14, 56, 36, 14, 14)
        p.setBrush(QtGui.QColor("#1f2328"))
        p.drawEllipse(14, 24, 14, 14)
        p.drawEllipse(36, 24, 14, 14)
        p.end()
        return QtGui.QIcon(pm)

    def draw_symbol(kind, color, size=44):
        """Line-art symbols for the dashboard rows."""
        pm = QtGui.QPixmap(size * 2, size * 2)  # 2x for HiDPI
        pm.setDevicePixelRatio(2)
        pm.fill(Qt.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        pen = QtGui.QPen(QtGui.QColor(color), size / 16)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        s = size
        if kind == "headset":
            p.drawRoundedRect(QtCore.QRectF(s * .08, s * .30, s * .84, s * .40), s * .14, s * .14)
            p.drawLine(QtCore.QPointF(s * .02, s * .45), QtCore.QPointF(s * .08, s * .45))
            p.drawLine(QtCore.QPointF(s * .92, s * .45), QtCore.QPointF(s * .98, s * .45))
            for x in (.36, .50, .64):
                p.drawEllipse(QtCore.QPointF(s * x, s * .54), s * .03, s * .03)
        elif kind.startswith("controller"):
            if kind.endswith("left"):
                p.translate(s, 0)
                p.scale(-1, 1)
            p.drawEllipse(QtCore.QPointF(s * .48, s * .32), s * .22, s * .22)
            p.drawEllipse(QtCore.QPointF(s * .50, s * .30), s * .05, s * .05)
            path = QtGui.QPainterPath(QtCore.QPointF(s * .36, s * .50))
            path.cubicTo(s * .34, s * .70, s * .38, s * .86, s * .48, s * .90)
            path.cubicTo(s * .58, s * .86, s * .58, s * .70, s * .58, s * .52)
            p.drawPath(path)
        elif kind == "monado":
            c = QtCore.QPointF(s * .5, s * .5)
            for i in range(8):
                a = math.radians(i * 45)
                p.drawLine(QtCore.QPointF(c.x() + math.cos(a) * s * .30, c.y() + math.sin(a) * s * .30),
                           QtCore.QPointF(c.x() + math.cos(a) * s * .42, c.y() + math.sin(a) * s * .42))
            p.drawEllipse(c, s * .30, s * .30)
            f = p.font()
            f.setPixelSize(int(s * .30))
            f.setBold(True)
            p.setFont(f)
            p.drawText(QtCore.QRectF(0, 0, s, s), Qt.AlignCenter, "M")
        elif kind == "wayvr":
            p.drawRoundedRect(QtCore.QRectF(s * .10, s * .18, s * .56, s * .42), s * .05, s * .05)
            p.drawRoundedRect(QtCore.QRectF(s * .34, s * .40, s * .56, s * .42), s * .05, s * .05)
            p.drawLine(QtCore.QPointF(s * .34, s * .52), QtCore.QPointF(s * .90, s * .52))
        elif kind == "laptop":
            p.drawRoundedRect(QtCore.QRectF(s * .18, s * .22, s * .64, s * .42), s * .04, s * .04)
            path = QtGui.QPainterPath(QtCore.QPointF(s * .06, s * .76))
            path.lineTo(s * .94, s * .76)
            path.lineTo(s * .86, s * .66)
            path.lineTo(s * .14, s * .66)
            path.closeSubpath()
            p.drawPath(path)
        p.end()
        return pm

    class Sparkline(QtWidgets.QWidget):
        def __init__(self, title, unit, fmt="{:.1f}", lo=None, hi=None):
            super().__init__()
            self.title, self.unit, self.fmt, self.lo, self.hi = title, unit, fmt, lo, hi
            self.values = collections.deque(maxlen=180)
            self.setMinimumHeight(56)
            self.setMinimumWidth(140)

        def push(self, v):
            self.values.append(v)
            self.update()

        def paintEvent(self, ev):
            p = QtGui.QPainter(self)
            p.setRenderHint(QtGui.QPainter.Antialiasing)
            pal = self.palette()
            r = self.rect().adjusted(1, 1, -1, -1)
            p.setPen(QtGui.QPen(pal.color(QtGui.QPalette.Mid)))
            p.setBrush(pal.color(QtGui.QPalette.Base))
            p.drawRoundedRect(r, 6, 6)
            vals = [v for v in self.values if v is not None]
            cur = self.values[-1] if self.values else None
            p.setPen(pal.color(QtGui.QPalette.Text))
            f = p.font()
            f.setPointSizeF(f.pointSizeF() * 0.85)
            p.setFont(f)
            txt = f"{self.title}: " + (self.fmt.format(cur) + " " + self.unit if cur is not None else "-")
            p.drawText(r.adjusted(8, 4, -8, -4), Qt.AlignLeft | Qt.AlignTop, txt)
            if len(vals) < 2:
                return
            lo = self.lo if self.lo is not None else min(vals)
            hi = max(self.hi if self.hi is not None else max(vals), max(vals))
            if hi - lo < 1e-6:
                hi = lo + 1
            plot = r.adjusted(8, 22, -8, -6)
            n = self.values.maxlen
            path = QtGui.QPainterPath()
            first = True
            start = n - len(self.values)
            for i, v in enumerate(self.values):
                if v is None:
                    first = True
                    continue
                x = plot.left() + plot.width() * (start + i) / (n - 1)
                y = plot.bottom() - plot.height() * (v - lo) / (hi - lo)
                if first:
                    path.moveTo(x, y)
                    first = False
                else:
                    path.lineTo(x, y)
            p.setPen(QtGui.QPen(QtGui.QColor("#4493f8"), 1.6))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)

    class Card(QtWidgets.QFrame):
        """Detailed status card for head / controllers (Details tab)."""

        def __init__(self, title):
            super().__init__()
            self.setObjectName("card")
            lay = QtWidgets.QVBoxLayout(self)
            top = QtWidgets.QHBoxLayout()
            self.dot = QtWidgets.QLabel()
            self.dot.setStyleSheet(dot_style(GREY, 14))
            name = QtWidgets.QLabel(f"<b>{title}</b>")
            self.state = QtWidgets.QLabel("-")
            top.addWidget(self.dot)
            top.addWidget(name)
            top.addStretch()
            top.addWidget(self.state)
            lay.addLayout(top)
            self.text = QtWidgets.QLabel()
            self.text.setWordWrap(True)
            self.text.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.text.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            lay.addWidget(self.text, 1)

        def set(self, card):
            st = card["state"]
            self.dot.setStyleSheet(dot_style(st, 14))
            bat = f" · battery ~{card['battery']} %" if card["battery"] is not None else ""
            self.state.setText(card["title"] + bat)
            self.state.setStyleSheet(f"color:{COLORS[st]}; font-weight:bold;" if st > GREEN else "")
            self.setStyleSheet(f"#card {{ border: 1px solid {COLORS[st] if st > GREEN else 'palette(mid)'};"
                               f" border-radius: 8px; }}")
            self.text.setText("\n".join(card["lines"]))

    class Section(QtWidgets.QGroupBox):
        def __init__(self, title):
            super().__init__(title)
            self.grid = QtWidgets.QGridLayout(self)
            self.grid.setColumnStretch(2, 1)
            self.grid.setVerticalSpacing(3)
            self.rows = {}

        def update_checks(self, checks):
            seen = set()
            for ch in checks:
                seen.add(ch.key)
                row = self.rows.get(ch.key)
                if row is None:
                    r = len(self.rows) * 2
                    dot = QtWidgets.QLabel()
                    label = QtWidgets.QLabel()
                    value = QtWidgets.QLabel()
                    value.setWordWrap(True)
                    value.setTextInteractionFlags(Qt.TextSelectableByMouse)
                    hint = QtWidgets.QLabel()
                    hint.setWordWrap(True)
                    hint.setTextInteractionFlags(Qt.TextSelectableByMouse)
                    self.grid.addWidget(dot, r, 0, Qt.AlignTop | Qt.AlignHCenter)
                    self.grid.addWidget(label, r, 1, Qt.AlignTop)
                    self.grid.addWidget(value, r, 2, Qt.AlignTop)
                    self.grid.addWidget(hint, r + 1, 1, 1, 2)
                    row = self.rows[ch.key] = (dot, label, value, hint)
                dot, label, value, hint = row
                for w in row:
                    w.setVisible(True)
                dot.setStyleSheet(dot_style(ch.state) + "margin-top:3px;")
                label.setText(f"<b>{ch.label}</b>")
                value.setText(ch.value)
                hint.setText("→ " + ch.hint if ch.hint else "")
                hint.setStyleSheet(f"color:{COLORS[ch.state] if ch.state > GREEN else 'palette(mid)'};"
                                   "margin-left:4px; margin-bottom:3px;")
                hint.setVisible(bool(ch.hint))
            for key, row in self.rows.items():
                if key not in seen:
                    for w in row:
                        w.setVisible(False)

    class DashRow(QtWidgets.QFrame):
        """One dashboard line; click to open the explanation + fix buttons."""
        clicked = QtCore.Signal(str)

        def __init__(self, key, on_action):
            super().__init__()
            self.key = key
            self.on_action = on_action
            self.expanded = False
            self.icon_kind = None
            self.icon_color = None
            self.setObjectName("dashrow")
            self.setCursor(Qt.PointingHandCursor)
            outer = QtWidgets.QVBoxLayout(self)
            outer.setContentsMargins(18, 12, 18, 12)
            top = QtWidgets.QHBoxLayout()
            top.setSpacing(16)
            self.icon = QtWidgets.QLabel()
            self.icon.setFixedSize(48, 48)
            self.icon.setAlignment(Qt.AlignCenter)
            names = QtWidgets.QVBoxLayout()
            names.setSpacing(0)
            self.category = QtWidgets.QLabel()
            f = self.category.font()
            f.setPointSizeF(f.pointSizeF() * 0.85)
            f.setCapitalization(QtGui.QFont.AllUppercase)
            f.setLetterSpacing(QtGui.QFont.PercentageSpacing, 106)
            self.category.setFont(f)
            self.name = QtWidgets.QLabel()
            f = self.name.font()
            f.setPointSizeF(f.pointSizeF() * 1.25)
            self.name.setFont(f)
            names.addWidget(self.category)
            names.addWidget(self.name)
            self.dot = QtWidgets.QLabel()
            stat = QtWidgets.QVBoxLayout()
            stat.setSpacing(0)
            self.status = QtWidgets.QLabel()
            f = self.status.font()
            f.setPointSizeF(f.pointSizeF() * 1.15)
            f.setBold(True)
            f.setCapitalization(QtGui.QFont.AllUppercase)
            self.status.setFont(f)
            self.detail = QtWidgets.QLabel()
            stat.addWidget(self.status)
            stat.addWidget(self.detail)
            self.chevron = QtWidgets.QLabel("›")
            f = self.chevron.font()
            f.setPointSizeF(f.pointSizeF() * 1.6)
            self.chevron.setFont(f)
            self.chevron.setFixedWidth(18)
            top.addWidget(self.icon)
            top.addLayout(names, 5)
            top.addWidget(self.dot, 0, Qt.AlignVCenter)
            top.addLayout(stat, 5)
            top.addWidget(self.chevron)
            outer.addLayout(top)

            self.body = QtWidgets.QWidget()
            body = QtWidgets.QVBoxLayout(self.body)
            body.setContentsMargins(64, 8, 0, 2)
            self.info = QtWidgets.QLabel()
            self.info.setWordWrap(True)
            self.info.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.todo = QtWidgets.QLabel()
            self.todo.setWordWrap(True)
            self.todo.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.buttons = QtWidgets.QHBoxLayout()
            self.buttons.addStretch()
            body.addWidget(self.info)
            body.addWidget(self.todo)
            body.addLayout(self.buttons)
            self.body.setVisible(False)
            outer.addWidget(self.body)
            self.action_sig = None

        def mousePressEvent(self, ev):
            if ev.button() == Qt.LeftButton:
                self.set_expanded(not self.expanded)

        def set_expanded(self, on):
            self.expanded = on
            self.body.setVisible(on)
            self.chevron.setText("⌄" if on else "›")

        def set(self, row):
            muted = self.palette().color(QtGui.QPalette.PlaceholderText).name()
            text = self.palette().color(QtGui.QPalette.WindowText).name()
            if (row.icon, text) != (self.icon_kind, self.icon_color):
                self.icon_kind, self.icon_color = row.icon, text
                self.icon.setPixmap(draw_symbol(row.icon, text))
            self.category.setText(row.category)
            self.category.setStyleSheet(f"color:{muted};")
            self.name.setText(row.name)
            self.dot.setStyleSheet(dot_style(row.state, 14))
            self.status.setText(row.status)
            self.status.setStyleSheet(f"color:{COLORS[row.state]};" if row.state > GREEN else "")
            self.detail.setText(row.detail)
            self.detail.setStyleSheet(f"color:{muted};")
            self.detail.setVisible(bool(row.detail))
            border = COLORS[row.state] if row.state > GREEN else "palette(mid)"
            self.setStyleSheet(f"#dashrow {{ background: palette(base); border: 1px solid {border};"
                               f" border-radius: 10px; }}")
            self.info.setText("<br>".join(row.info) if row.info else "Nothing to report.")
            self.todo.setText(f"<b>What to do:</b> {row.todo}" if row.todo else "")
            self.todo.setVisible(bool(row.todo))
            sig = tuple(row.actions)
            if sig != self.action_sig:
                self.action_sig = sig
                while self.buttons.count() > 1:
                    w = self.buttons.takeAt(0).widget()
                    if w:
                        w.deleteLater()
                for label, action in row.actions:
                    b = QtWidgets.QPushButton(label)
                    b.setMinimumHeight(32)
                    b.clicked.connect(lambda _=False, a=action: self.on_action(a))
                    self.buttons.insertWidget(self.buttons.count() - 1, b)

    class SetupRow(QtWidgets.QFrame):
        """One setup step: state, explanation, details, button."""
        SETUP_COLORS = {setup_steps.OK: GREEN, setup_steps.MISSING: RED, setup_steps.OUTDATED: YELLOW,
                        setup_steps.WARN: YELLOW, setup_steps.INFO: GREY}

        def __init__(self, step, on_run):
            super().__init__()
            self.step = step
            self.setObjectName("setuprow")
            lay = QtWidgets.QHBoxLayout(self)
            lay.setContentsMargins(14, 10, 14, 10)
            lay.setSpacing(14)
            self.dot = QtWidgets.QLabel()
            self.dot.setStyleSheet(dot_style(GREY, 14))
            lay.addWidget(self.dot, 0, Qt.AlignTop | Qt.AlignHCenter)
            text = QtWidgets.QVBoxLayout()
            text.setSpacing(2)
            title = QtWidgets.QLabel(f"<b>{step.title}</b>" + ("  <small>(asks for your password)</small>"
                                                               if step.root else ""))
            if step.root:
                title.setToolTip("Asks for your password (pkexec)")
            about = QtWidgets.QLabel(step.about)
            about.setWordWrap(True)
            about.setStyleSheet("color: palette(placeholder-text);")
            self.details = QtWidgets.QLabel()
            self.details.setWordWrap(True)
            self.details.setTextInteractionFlags(Qt.TextSelectableByMouse)
            text.addWidget(title)
            text.addWidget(about)
            text.addWidget(self.details)
            lay.addLayout(text, 1)
            self.status = QtWidgets.QLabel("…")
            self.status.setMinimumWidth(170)
            self.status.setAlignment(Qt.AlignRight | Qt.AlignTop)
            lay.addWidget(self.status, 0, Qt.AlignTop)
            self.btn = QtWidgets.QPushButton()
            self.btn.setMinimumWidth(110)
            if step.duration:
                self.btn.setToolTip(f"Takes about {step.duration}")
            self.btn.clicked.connect(lambda: on_run(step.key))
            self.btn.setVisible(False)
            lay.addWidget(self.btn, 0, Qt.AlignTop)

        def set(self, state):
            st = self.SETUP_COLORS[state.kind]
            self.dot.setStyleSheet(dot_style(st, 14) + "margin-top:3px;")
            self.status.setText(state.text)
            self.status.setStyleSheet(f"color:{COLORS[st]}; font-weight:bold;" if st > GREEN else "")
            show_details = state.kind != setup_steps.OK or self.step.check_only
            self.details.setText("<br>".join(state.details) if show_details else "")
            self.details.setVisible(bool(state.details) and show_details)
            label = self.step.button(state)
            self.btn.setVisible(label is not None)
            if label:
                self.btn.setText(label)
            border = COLORS[st] if st > GREEN else "palette(mid)"
            self.setStyleSheet(f"#setuprow {{ background: palette(base); border: 1px solid {border};"
                               f" border-radius: 10px; }}")

    class Smoother:
        """Problems must persist HOLD seconds before a row turns yellow/red; recovery is immediate."""
        HOLD = 3.0

        def __init__(self):
            self.shown = {}
            self.pending = {}

        def apply(self, rows):
            now = time.monotonic()
            out = []
            for r in rows:
                prev = self.shown.get(r.key)
                if prev is None or r.state <= prev.state or r.state <= GREEN:
                    self.pending.pop(r.key, None)
                    self.shown[r.key] = r
                else:
                    since = self.pending.setdefault(r.key, now)
                    if now - since >= self.HOLD:
                        self.pending.pop(r.key, None)
                        self.shown[r.key] = r
                out.append(self.shown[r.key])
            return out

    class Window(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("Rift S")
            self.setWindowIcon(make_icon(GREY))
            self.resize(1000, 860)
            self.settings = QtCore.QSettings("rift-s-linux", "vrstatus")
            self.bridge = Bridge()
            self.collector = Collector()
            self.actions = Actions(self.bridge.log.emit, self.collector)
            self.smoother = Smoother()
            self.prev_red = set()
            self.inhibitor = None
            self.quitting = False

            central = QtWidgets.QWidget()
            self.setCentralWidget(central)
            root = QtWidgets.QVBoxLayout(central)

            # Header
            head = QtWidgets.QHBoxLayout()
            titles = QtWidgets.QVBoxLayout()
            titles.setSpacing(0)
            self.title = QtWidgets.QLabel("Rift S")
            f = self.title.font()
            f.setPointSizeF(f.pointSizeF() * 1.6)
            f.setBold(True)
            self.title.setFont(f)
            self.summary = QtWidgets.QLabel("Checking…")
            titles.addWidget(self.title)
            titles.addWidget(self.summary)
            head.addLayout(titles, 1)
            self.btn_recenter = self.button("Recenter", self.actions.recenter, "Reset the forward direction")
            self.btn_restart_all = self.button("Restart all", self.actions.restart_all,
                                               "Restart Monado and WayVR (fixes most problems). "
                                               "Restart the game afterwards.")
            for b in (self.btn_recenter, self.btn_restart_all):
                b.setMinimumHeight(36)
                head.addWidget(b)
            root.addLayout(head)

            self.tabs = QtWidgets.QTabWidget()
            root.addWidget(self.tabs, 1)
            self.tabs.addTab(self.build_dashboard(), "Dashboard")
            self.tabs.addTab(self.build_details_tab(), "Details")
            self.tabs.addTab(self.build_tools_tab(), "Tools && config")
            self.setup_tab = self.build_setup_tab()
            self.tabs.addTab(self.setup_tab, "Setup")
            self.logview = QtWidgets.QPlainTextEdit()
            self.logview.setReadOnly(True)
            self.logview.setMaximumBlockCount(3000)
            self.logview.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
            self.tabs.addTab(self.logview, "Activity log")
            self.statusBar().showMessage("Collecting…")

            # Tray: icon colour = dashboard status, no desktop notifications
            self.tray = None
            if QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
                self.tray = QtWidgets.QSystemTrayIcon(make_icon(GREY), self)
                menu = QtWidgets.QMenu()
                menu.addAction("Show / hide", self.toggle_visible)
                menu.addSeparator()
                menu.addAction("Recenter", lambda: self.do(self.actions.recenter))
                menu.addAction("Restart all", lambda: self.do(self.actions.restart_all))
                menu.addAction("Stop Monado", lambda: self.do(self.actions.stop_monado))
                menu.addSeparator()
                menu.addAction("Quit", self.quit)
                self.tray.setContextMenu(menu)
                self.tray.activated.connect(
                    lambda r: self.toggle_visible() if r == QtWidgets.QSystemTrayIcon.Trigger else None)
                self.tray.show()

            self.bridge.snapshot.connect(self.on_snapshot)
            self.bridge.log.connect(self.on_log)
            self.bridge.ask.connect(self.on_ask, Qt.BlockingQueuedConnection)
            self.bridge.setup_state.connect(self.on_setup_state)
            self.bridge.action_done.connect(self.refresh_setup)
            self.setup_first = "--screenshot" not in sys.argv
            self.uninstalled = False
            self.refresh_setup()
            self.wake = threading.Event()
            self.stop = threading.Event()
            self.worker = threading.Thread(target=self.poll_loop, daemon=True)
            self.worker.start()

        # -- building ----------------------------------------------------------------

        def button(self, text, fn, tip=""):
            b = QtWidgets.QPushButton(text)
            if tip:
                b.setToolTip(tip)
            b.clicked.connect(lambda: self.do(fn))
            return b

        def build_dashboard(self):
            w = QtWidgets.QWidget()
            outer = QtWidgets.QHBoxLayout(w)
            col = QtWidgets.QVBoxLayout()
            col.setSpacing(10)
            self.setup_banner = QtWidgets.QPushButton()
            self.setup_banner.setMinimumHeight(44)
            self.setup_banner.setStyleSheet(f"text-align:left; padding:8px 16px; font-weight:bold;"
                                            f" border:1px solid {COLORS[YELLOW]}; border-radius:10px;")
            self.setup_banner.clicked.connect(lambda: self.tabs.setCurrentWidget(self.setup_tab))
            self.setup_banner.setVisible(False)
            col.addWidget(self.setup_banner)
            self.dash_rows = {}
            for key in ("headset", "left", "right", "monado", "wayvr", "laptop"):
                row = DashRow(key, self.dash_action)
                self.dash_rows[key] = row
                col.addWidget(row)
            hint = QtWidgets.QLabel("Click a line for details and what to do.")
            hint.setStyleSheet("color: palette(placeholder-text);")
            hint.setAlignment(Qt.AlignCenter)
            col.addWidget(hint)
            col.addStretch()
            box = QtWidgets.QWidget()
            box.setLayout(col)
            box.setMaximumWidth(920)
            outer.addStretch()
            outer.addWidget(box, 20)
            outer.addStretch()
            sa = QtWidgets.QScrollArea()
            sa.setWidgetResizable(True)
            sa.setFrameShape(QtWidgets.QFrame.NoFrame)
            sa.setWidget(w)
            return sa

        def build_details_tab(self):
            w = QtWidgets.QWidget()
            lay = QtWidgets.QVBoxLayout(w)
            cards = QtWidgets.QHBoxLayout()
            self.cards = {"left": Card("Left controller"), "head": Card("Head"), "right": Card("Right controller")}
            for k in ("left", "head", "right"):
                cards.addWidget(self.cards[k], 1)
            lay.addLayout(cards)
            graphs = QtWidgets.QHBoxLayout()
            self.graphs = {
                "missed_per_s": Sparkline("Missed frames", "/s", lo=0, hi=5),
                "monado_cpu": Sparkline("Monado CPU", "cores", lo=0, hi=3),
                "tctl": Sparkline("Tctl", "°C", "{:.0f}", lo=40, hi=95),
                "gpu_mhz": Sparkline("GPU clock", "MHz", "{:.0f}", lo=0, hi=2900),
            }
            for g in self.graphs.values():
                graphs.addWidget(g)
            lay.addLayout(graphs)
            grid = QtWidgets.QGridLayout()
            self.sections = {}
            layout = [("Tracking", 0, 0), ("Monado", 1, 0), ("Clients", 2, 0),
                      ("Hardware", 0, 1), ("Power / thermals", 1, 1), ("Steam / xrizer", 2, 1)]
            for name, r, c in layout:
                s = Section(name)
                self.sections[name] = s
                grid.addWidget(s, r, c, Qt.AlignTop)
            grid.setColumnStretch(0, 1)
            grid.setColumnStretch(1, 1)
            lay.addLayout(grid)
            lay.addStretch()
            sa = QtWidgets.QScrollArea()
            sa.setWidgetResizable(True)
            sa.setWidget(w)
            return sa

        def build_tools_tab(self):
            w = QtWidgets.QWidget()
            grid = QtWidgets.QGridLayout(w)
            env = read_conf()

            box = QtWidgets.QGroupBox("Monado")
            v = QtWidgets.QVBoxLayout(box)
            v.addWidget(self.button("Start Monado", self.actions.start_monado))
            v.addWidget(self.button("Stop Monado", self.actions.stop_monado, "Saves ~3 CPU cores when not playing"))
            v.addWidget(self.button("Restart Monado", self.actions.restart_monado))
            v.addWidget(self.button("Monado log", lambda: open_terminal(
                ["journalctl", "--user", "-u", "monado.service", "-f", "-n", "300"])))
            v.addWidget(self.button("xrizer log", lambda: open_terminal(["less", "+F", XRIZER_LOG])))
            v.addStretch()
            grid.addWidget(box, 0, 0)

            box = QtWidgets.QGroupBox("WayVR (wayvr.service)")
            v = QtWidgets.QVBoxLayout(box)
            v.addWidget(self.button("Start", self.actions.start_wayvr, "Also starts Monado if needed"))
            v.addWidget(self.button("Stop", self.actions.stop_wayvr))
            v.addWidget(self.button("Restart", self.actions.restart_wayvr))
            v.addWidget(self.button("WayVR log", lambda: open_terminal(
                ["journalctl", "--user", "-u", "wayvr.service", "-f", "-n", "300"])))
            self.wv_enabled = QtWidgets.QCheckBox("Start together with Monado")
            self.wv_enabled.setChecked(run(["systemctl", "--user", "is-enabled", "wayvr.service"])[1].strip()
                                       == "enabled")
            self.wv_enabled.toggled.connect(lambda on: self.do(lambda: self.actions.wayvr_enable(on)))
            v.addWidget(self.wv_enabled)
            self.wv_desktop = QtWidgets.QCheckBox("Desktop capture (costs GPU)")
            self.wv_desktop.setChecked(WAYVR_MODE_CONF.exists())
            self.wv_desktop.toggled.connect(lambda on: self.do(lambda: self.actions.wayvr_desktop(on)))
            v.addWidget(self.wv_desktop)
            v.addStretch()
            grid.addWidget(box, 0, 1)

            box = QtWidgets.QGroupBox("Tests (open an OpenXR session)")
            v = QtWidgets.QVBoxLayout(box)
            v.addWidget(self.button("Head stillness test (10 s)", self.actions.head_test,
                                    "headrec 10 with spoken cues. Healthy: <3 cm, <1°, <5 mm steps"))
            v.addWidget(self.button("Guided controller test (45 s)", self.actions.guided_test,
                                    f"tools/guided-run.sh + analyze.py, saved in {RECORDINGS}"))
            v.addWidget(self.button("Vibration test (16 s)", self.actions.vibration_test,
                                    "hapticstest: left then right controller, 30/60/100 %, short pulses, "
                                    "then per-frame pulses that should feel like one continuous buzz"))
            v.addWidget(self.button("Reach test (1 min)", self.actions.reach_test,
                                    "tools/reach-test.py: controllers touching the headset / each other / arms "
                                    "stretched, in head coordinates. Front touch should read ~10-15 cm forward"))
            v.addStretch()
            grid.addWidget(box, 0, 2)

            box = QtWidgets.QGroupBox("Developer (tracking work, see tools/dev/README.md)")
            v = QtWidgets.QVBoxLayout(box)
            self.btn_rec = self.button("Start controller recording", self.actions.toggle_recording,
                                       f"Restarts Monado with RIFT_S_CTRL_RECORD={RECORDINGS}/ctrlrec-<time>")
            v.addWidget(self.btn_rec)
            v.addWidget(self.button("Tracking protocol (4 min)", self.actions.ctrl_protocol,
                                    "tools/dev/ctrl-protocol.py + ctrl-analyze.py: fixed movement sequence for "
                                    "comparing tracking runs. Start a controller recording first"))
            v.addWidget(self.button("Dump HMD firmware blocks", self.actions.dump_firmware,
                                    f"tools/dev/dump_fw_blocks.py into {FIRMWARE}, Monado must be stopped. "
                                    "Contains serial numbers: keep the dumps private"))
            v.addStretch()
            grid.addWidget(box, 2, 2)

            box = QtWidgets.QGroupBox("Config (rift-s.conf, applying restarts Monado)")
            form = QtWidgets.QFormLayout(box)
            self.cfg_fusion = QtWidgets.QCheckBox("IMU + optical fusion filter")
            self.cfg_fusion.setChecked(env.get("RIFT_S_CTRL_FUSION", "1") != "0")
            form.addRow("Controllers", self.cfg_fusion)
            self.cfg_cams = QtWidgets.QComboBox()
            self.cfg_cams.addItems(["5", "3", "2"])
            self.cfg_cams.setCurrentText(env.get("RIFT_S_SLAM_CAMS", "5"))
            self.cfg_cams.setToolTip("5 = smooth; 2 saves ~0.8 cores but causes head-turn stutter")
            form.addRow("SLAM cameras", self.cfg_cams)
            self.cfg_scale = QtWidgets.QSpinBox()
            self.cfg_scale.setRange(50, 200)
            self.cfg_scale.setSingleStep(10)
            self.cfg_scale.setSuffix(" %")
            self.cfg_scale.setValue(int(env.get("XRT_COMPOSITOR_SCALE_PERCENTAGE", "140")))
            self.cfg_scale.setToolTip("100 = 1280x1440 per eye (known good), Monado default 140")
            form.addRow("Render scale", self.cfg_scale)
            self.cfg_budget = QtWidgets.QSpinBox()
            self.cfg_budget.setRange(2, 12)
            self.cfg_budget.setSuffix(" ms")
            self.cfg_budget.setValue(int(env.get("U_PACING_COMP_MIN_TIME_MS", "3")))
            self.cfg_budget.setToolTip("Compositor frame budget: 3 (default) misses ~half the vblanks, 8 = 0 misses")
            form.addRow("Compositor budget", self.cfg_budget)
            apply = QtWidgets.QPushButton("Apply config + restart Monado")
            apply.clicked.connect(self.apply_config)
            form.addRow(apply)
            lens = QtWidgets.QHBoxLayout()
            self.cfg_c1 = QtWidgets.QDoubleSpinBox()
            self.cfg_c1.setRange(0.30, 0.65)
            self.cfg_c1.setSingleStep(0.01)
            self.cfg_c1.setDecimals(2)
            self.cfg_c1.setValue(float(env.get("RIFT_S_DISTORTION_C1", "0.47")))
            lens.addWidget(self.cfg_c1)
            b = QtWidgets.QPushButton("Apply c1")
            b.clicked.connect(lambda: self.do(lambda: self.actions.set_lens(f"{self.cfg_c1.value():.2f}")))
            lens.addWidget(b)
            b = QtWidgets.QPushButton("CV1 model")
            b.clicked.connect(lambda: self.do(lambda: self.actions.set_lens("cv1")))
            lens.addWidget(b)
            form.addRow("Lens c1", lens)
            grid.addWidget(box, 1, 0, 1, 2)

            box = QtWidgets.QGroupBox("Options")
            v = QtWidgets.QVBoxLayout(box)
            self.opt = {}
            for key, text, default, tip in (
                    ("speak", "Spoken alerts in the headset", True,
                     "Only for: head tracking lost, controller battery empty, Monado crashed"),
                    ("inhibit", "Block lid switch / sleep while Monado runs", False,
                     "systemd-inhibit --what=handle-lid-switch:sleep:idle (the lid sensor suspended the laptop mid-game once)"),
                    ("close_to_tray", "Close button hides to tray", True, "")):
                cb = QtWidgets.QCheckBox(text)
                cb.setToolTip(tip)
                cb.setChecked(self.settings.value(key, default, type=bool))
                cb.toggled.connect(lambda val, k=key: self.settings.setValue(k, val))
                v.addWidget(cb)
                self.opt[key] = cb
            v.addStretch()
            grid.addWidget(box, 1, 2)
            grid.setRowStretch(3, 1)
            sa = QtWidgets.QScrollArea()
            sa.setWidgetResizable(True)
            sa.setWidget(w)
            return sa

        def build_setup_tab(self):
            w = QtWidgets.QWidget()
            outer = QtWidgets.QHBoxLayout(w)
            col = QtWidgets.QVBoxLayout()
            col.setSpacing(8)
            intro = QtWidgets.QLabel(
                "Everything is built and installed into your home folder (<tt>~/.local/opt</tt>, "
                "<tt>~/.local/share/rift-s-linux</tt>); config files that get replaced are backed up first. "
                "Before Fedora packages are installed you see what dnf would install and upgrade.")
            intro.setWordWrap(True)
            col.addWidget(intro)
            self.setup_rows = {}
            for st in setup_steps.STEPS:
                row = SetupRow(st, self.run_setup_step)
                self.setup_rows[st.key] = row
                col.addWidget(row)
            btns = QtWidgets.QHBoxLayout()
            self.btn_setup_all = QtWidgets.QPushButton("Install everything missing")
            self.btn_setup_all.setMinimumHeight(38)
            f = self.btn_setup_all.font()
            f.setBold(True)
            self.btn_setup_all.setFont(f)
            self.btn_setup_all.clicked.connect(lambda: self.run_setup_step(None))
            btns.addWidget(self.btn_setup_all)
            refresh = QtWidgets.QPushButton("Check again")
            refresh.clicked.connect(self.refresh_setup)
            btns.addWidget(refresh)
            btns.addStretch()
            uninstall = QtWidgets.QPushButton("Uninstall…")
            uninstall.setToolTip("Removes what this setup installed and restores replaced config files")
            uninstall.clicked.connect(self.run_uninstall)
            btns.addWidget(uninstall)
            col.addLayout(btns)
            col.addStretch()
            box = QtWidgets.QWidget()
            box.setLayout(col)
            box.setMaximumWidth(920)
            outer.addStretch()
            outer.addWidget(box, 20)
            outer.addStretch()
            sa = QtWidgets.QScrollArea()
            sa.setWidgetResizable(True)
            sa.setFrameShape(QtWidgets.QFrame.NoFrame)
            sa.setWidget(w)
            return sa

        def setup_ctx(self):
            def confirm(title, text, default_yes):
                holder = {"title": title, "text": text, "default": default_yes, "answer": False}
                self.bridge.ask.emit(holder)
                return holder["answer"]
            return setup_steps.Ctx(self.bridge.log.emit, confirm)

        def on_ask(self, h):
            box = QtWidgets.QMessageBox(self)
            box.setWindowTitle(h["title"])
            box.setIcon(QtWidgets.QMessageBox.Question if h["default"] else QtWidgets.QMessageBox.Warning)
            box.setText(h["text"])
            box.setStandardButtons(QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
            box.setDefaultButton(QtWidgets.QMessageBox.Yes if h["default"] else QtWidgets.QMessageBox.No)
            h["answer"] = box.exec() == QtWidgets.QMessageBox.Yes

        def run_setup_step(self, key):
            self.do(lambda: setup_steps.install(self.setup_ctx(), [key] if key else None))

        def run_uninstall(self):
            def job():
                try:
                    setup_steps.uninstall(self.setup_ctx())
                    self.uninstalled = True
                except setup_steps.Cancelled:
                    self.bridge.log.emit("Uninstall cancelled")
            self.do(job)

        def refresh_setup(self):
            def job():
                try:
                    self.bridge.setup_state.emit([(st, st.check()) for st in setup_steps.STEPS])
                except Exception as e:
                    self.bridge.log.emit(f"Setup check failed: {e!r}")
            threading.Thread(target=job, daemon=True).start()

        def on_setup_state(self, states):
            if self.uninstalled:
                QtWidgets.QMessageBox.information(self, "Uninstalled",
                                                  "Everything the setup installed is removed. This window closes now.")
                self.quit()
                return
            for st, state in states:
                self.setup_rows[st.key].set(state)
            todo = [st for st, state in states if state.needs_work]
            self.btn_setup_all.setEnabled(bool(todo))
            self.setup_banner.setVisible(bool(todo))
            if todo:
                self.setup_banner.setText(f"Setup not finished: {len(todo)} of {len(states) - 1} steps left"
                                          "  ·  Open setup ›")
            if self.setup_first:
                self.setup_first = False
                if todo:
                    self.tabs.setCurrentWidget(self.setup_tab)

        # -- actions -----------------------------------------------------------------

        def dash_action(self, name):
            fn = {
                "restart_all": self.actions.restart_all,
                "restart_monado": self.actions.restart_monado,
                "start_monado": self.actions.start_monado,
                "stop_monado": self.actions.stop_monado,
                "start_wayvr": self.actions.start_wayvr,
                "restart_wayvr": self.actions.restart_wayvr,
                "wayvr_enable": lambda: self.actions.wayvr_enable(True),
                "set_performance": self.actions.set_performance,
            }.get(name)
            if fn:
                self.do(fn, show_log=False)

        def do(self, fn, show_log=True):
            """Run an action in a background thread (one at a time), then refresh."""
            if not self.actions.busy.acquire(blocking=False):
                self.on_log("Another action is still running")
                return

            def runner():
                try:
                    fn()
                except Exception as e:  # keep the GUI alive whatever happens
                    self.bridge.log.emit(f"Action failed: {e!r}")
                finally:
                    self.actions.busy.release()
                    self.wake.set()
                    self.bridge.action_done.emit()
            if show_log:
                self.tabs.setCurrentWidget(self.logview)
            self.statusBar().showMessage("Working…")
            threading.Thread(target=runner, daemon=True).start()

        def apply_config(self):
            changes = {
                "RIFT_S_CTRL_FUSION": None if self.cfg_fusion.isChecked() else "0",
                "RIFT_S_SLAM_CAMS": self.cfg_cams.currentText(),
                "XRT_COMPOSITOR_SCALE_PERCENTAGE": str(self.cfg_scale.value()),
                "U_PACING_COMP_MIN_TIME_MS": str(self.cfg_budget.value()),
            }
            self.do(lambda: self.actions.apply_conf(changes))

        def toggle_visible(self):
            if self.isVisible() and not self.isMinimized():
                self.hide()
            else:
                self.showNormal()
                self.raise_()
                self.activateWindow()

        def closeEvent(self, ev):
            if self.tray is not None and self.opt["close_to_tray"].isChecked() and not self.quitting:
                ev.ignore()
                self.hide()
                return
            self.quit()

        def quit(self):
            self.quitting = True
            self.stop.set()
            self.wake.set()
            self.set_inhibit(False)
            self.collector.link.close()
            QtWidgets.QApplication.quit()

        # -- polling -----------------------------------------------------------------

        def poll_loop(self):
            while not self.stop.is_set():
                t0 = time.monotonic()
                try:
                    self.bridge.snapshot.emit(self.collector.collect())
                except Exception as e:
                    self.bridge.log.emit(f"Collector error: {e!r}")
                self.wake.wait(max(0.2, 1.0 - (time.monotonic() - t0)))
                self.wake.clear()

        def on_log(self, text):
            stamp = time.strftime("%H:%M:%S")
            for line in text.splitlines() or [""]:
                self.logview.appendPlainText(f"{stamp}  {line}")

        def set_inhibit(self, on):
            running = self.inhibitor is not None and self.inhibitor.poll() is None
            if on and not running:
                self.inhibitor = subprocess.Popen(
                    ["systemd-inhibit", "--what=handle-lid-switch:sleep:idle", "--who=VR status",
                     "--why=Monado is running", "--mode=block", "sleep", "infinity"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.on_log("Lid switch / sleep blocked while Monado runs")
            elif not on and running:
                self.inhibitor.terminate()
                self.inhibitor = None
                self.on_log("Lid switch / sleep no longer blocked")

        def on_snapshot(self, snap):
            svc = snap["service"]
            rows = self.smoother.apply(snap["dashboard"])
            for r in rows:
                self.dash_rows[r.key].set(r)
            for name, checks in snap["sections"].items():
                self.sections[name].update_checks(checks)
            for k, card in snap["cards"].items():
                self.cards[k].set(card)
            for k, g in self.graphs.items():
                g.push(snap["metrics"].get(k))

            worst = max((r.state for r in rows), default=GREY)
            bad = [r for r in sorted(rows, key=lambda r: -r.state) if r.state >= YELLOW]
            if bad:
                self.summary.setText(" · ".join(f"{r.category}: {r.status}" for r in bad))
                self.summary.setStyleSheet(f"color:{COLORS[worst]}; font-weight:bold;")
            else:
                self.summary.setText("Everything OK" + ("" if svc["active"] else
                                                        " · Monado idle, starts with the game"))
                self.summary.setStyleSheet("")
            self.btn_recenter.setEnabled(svc["active"])
            self.btn_rec.setText("Stop controller recording" if "RIFT_S_CTRL_RECORD" in read_conf()
                                 else "Start controller recording")
            icon_state = worst if worst >= YELLOW else (GREEN if svc["active"] else GREY)
            icon = make_icon(icon_state)
            self.setWindowIcon(icon)
            if self.tray is not None:
                self.tray.setIcon(icon)
                self.tray.setToolTip("VR status: " + ("\n".join(f"{r.category}: {r.status}" for r in bad)
                                                      if bad else "everything OK"))
            self.statusBar().showMessage(f"Updated {time.strftime('%H:%M:%S')}  ·  collect "
                                         f"{snap['collect_ms']:.0f} ms")
            self.set_inhibit(self.opt["inhibit"].isChecked() and svc["active"])
            self.speak_alerts(rows, svc)

        def speak_alerts(self, rows, svc):
            red = {r.key for r in rows if r.state == RED and r.speak}
            new = red - self.prev_red
            self.prev_red = red
            if not new or not svc["active"] or not self.opt["speak"].isChecked():
                return
            for r in rows:
                if r.key in new:
                    self.on_log(f"Alert: {r.category}: {r.status}")
                    threading.Thread(target=say, args=(r.speak,), daemon=True).start()

    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("vrstatus")
    app.setDesktopFileName("vrstatus")
    app.setQuitOnLastWindowClosed(False)
    win = Window()
    if "--hidden" not in sys.argv or win.tray is None:
        win.show()
    if "--screenshot" in sys.argv:  # vrstatus.py --screenshot out.png [tab] [row to open]: grab after 5 s
        args = sys.argv[sys.argv.index("--screenshot") + 1:]
        if os.environ.get("RIFTS_SCREENSHOT_SIZE"):
            win.resize(*map(int, os.environ["RIFTS_SCREENSHOT_SIZE"].split("x")))
        win.tabs.setCurrentIndex(int(args[1]) if len(args) > 1 else 0)
        if len(args) > 2:
            win.dash_rows[args[2]].set_expanded(True)
        QtCore.QTimer.singleShot(5000, lambda: (win.grab().save(args[0]), win.quit()))
    sys.exit(app.exec())


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        setup_steps.main(sys.argv[2:])
    elif "--once" in sys.argv:
        text_report()
    else:
        try:
            import PySide6  # noqa: F401
        except ImportError:
            sys.exit("PySide6 missing: start with ./rift-s-linux (own venv with pip PySide6; do NOT dnf install "
                     "python3-pyside6 for it, that can upgrade Qt ahead of Plasma and break the desktop)")
        main_gui()
