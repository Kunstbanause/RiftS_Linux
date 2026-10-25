#!/usr/bin/env python3
"""First-time setup: checks and installs everything the Rift S stack needs.

Usage: rift-s-linux setup                       state of every step
       rift-s-linux setup install [step ...]    install what is missing (or rebuild the named steps)
       rift-s-linux setup uninstall             remove what the setup installed, restore replaced files

The GUI's Setup tab runs the same steps. Rules:
- Everything is built / unpacked into the user's home (~/.local/opt, ~/.local/share/rift-s-linux).
- Only three things need root, each asked for with pkexec: Fedora packages (build dependencies,
  udev rules, Basalt), and setcap on monado-service.
- Before installing packages, dnf is asked what it would do; upgrades of desktop / graphics
  packages (Qt, KDE, Mesa, ...) are shown and need an explicit "install anyway".
- Config files that already exist are backed up before they are replaced; uninstall restores them.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import *  # noqa: E402,F401,F403
import paths  # noqa: E402

VERSIONS = json.loads((CONFIG / "versions.json").read_text())

OK, MISSING, OUTDATED, WARN, INFO = "ok", "missing", "outdated", "warn", "info"

# Fedora package names / capabilities. Build: Monado (as in Fedora's monado.spec, minus drivers for
# other hardware), the test tools and xrizer (cargo, only when building it from source).
BUILD_PACKAGES = [
    "git", "cmake", "ninja-build", "gcc-c++", "glslc", "pkgconfig(glslang)", "pkgconfig(eigen3)",
    "pkgconfig(vulkan)", "pkgconfig(hidapi-libusb)", "pkgconfig(libusb-1.0)", "pkgconfig(libudev)",
    "pkgconfig(libsystemd)", "pkgconfig(dbus-1)", "pkgconfig(libbsd)", "pkgconfig(libcjson)",
    "pkgconfig(libdrm)", "pkgconfig(libjpeg)", "pkgconfig(opencv)", "pkgconfig(openxr)",
    "pkgconfig(sdl2)", "pkgconfig(wayland-client)", "pkgconfig(wayland-protocols)",
    "pkgconfig(wayland-scanner)", "pkgconfig(xrandr)", "pkgconfig(egl)", "pkgconfig(gl)",
    "pkgconfig(dri)", "pkgconfig(zlib)",
]
RUNTIME_PACKAGES = [
    "basalt-monado",      # SLAM head tracking (loaded at runtime via VIT_SYSTEM_LIBRARY_PATH)
    "xr-hardware",        # udev rules: lets your user open the headset's USB / hidraw devices
    "openxr-libs",        # OpenXR loader
    "libcap",             # setcap / getcap
    "usbutils",           # lsusb (status checks)
]
RECOMMENDED_PACKAGES = [
    "espeak-ng",          # spoken cues / alerts in the headset
    "pulseaudio-utils",   # paplay / pactl for those
    "lm_sensors",         # temperatures in the status GUI
]
# Upgrading these on their own (without a full system update) can break the desktop until the next
# full update: a Qt upgrade pulled in ahead of Plasma once left KDE without a desktop after reboot.
PROTECTED_RE = re.compile(r"^(qt[56]-|kf[56]-|plasma-|kwin|sddm|mesa-|libdrm|vulkan-|glibc|systemd|kernel|"
                          r"gnome-shell|mutter|gtk[34]|pipewire|wireplumber|xorg-x11-server|xwayland|"
                          r"python3-pyside)")

CI_NAME = ["-c", "user.name=rift-s-linux setup", "-c", "user.email=setup@rift-s-linux.invalid"]


class StepFailed(Exception):
    pass


class Cancelled(Exception):
    pass


@dataclass
class State:
    kind: str
    text: str
    details: list = field(default_factory=list)

    @property
    def needs_work(self):
        return self.kind in (MISSING, OUTDATED)


# ----------------------------------------------------------------------------------------------
# Helpers


def quick(cmd, timeout=20, cwd=None):
    """Run a command, return (rc, stdout + stderr); never raises."""
    try:
        r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, timeout=timeout,
                           errors="replace", cwd=cwd)
        return r.returncode, r.stdout + r.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)


def systemctl(*args):
    """systemctl --user, skipped in test mode (RIFTS_HOME)."""
    if paths.TEST_HOME:
        return 0, "(test mode: systemctl skipped)"
    return quick(["systemctl", "--user", *args])


def os_release():
    try:
        return dict(re.findall(r'^(\w+)="?(.*?)"?$', Path("/etc/os-release").read_text(), re.M))
    except OSError:
        return {}


def is_fedora():
    return os_release().get("ID") == "fedora"


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def files_hash(*paths_):
    h = hashlib.sha256()
    for p in paths_:
        for f in sorted(Path(p).glob("*")) if Path(p).is_dir() else [Path(p)]:
            h.update(f.name.encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:16]


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def rpm_missing(caps):
    """The capabilities / package names in caps that no installed package provides."""
    if not shutil.which("rpm"):
        return list(caps)
    rc, out = quick(["rpm", "-q", "--whatprovides", *caps], timeout=30)
    missing = re.findall(r"^no package provides (.+)$", out, re.M)
    missing += re.findall(r"^package (\S+) is not installed$", out, re.M)
    return [c for c in caps if c in missing]


def parse_dnf_transaction(out):
    """dnf5 --assumeno output -> {section: [package names]} (installing, upgrading, ...)."""
    sections, cur = {}, None
    for ln in out.splitlines():
        s = ln.strip()
        if not s:
            continue
        if not ln.startswith(" ") and s.endswith(":"):
            cur = s[:-1].lower()
            sections.setdefault(cur, [])
            continue
        if cur and cur != "transaction summary" and ln.startswith(" ") and not s.startswith("replacing"):
            sections[cur].append(s.split()[0])
    return sections


def unit_enabled(unit):
    if paths.TEST_HOME:
        return unit in (read_json(MANIFEST, {}) or {}).get("enabled", [])
    rc, out = quick(["systemctl", "--user", "is-enabled", unit])
    return out.strip() == "enabled"


# ----------------------------------------------------------------------------------------------
# Manifest: which files we replaced (with their backup) and which directories we created


class Manifest:
    def __init__(self):
        self.data = read_json(MANIFEST, {}) or {}
        self.data.setdefault("files", {})      # path -> backup path or None (did not exist)
        self.data.setdefault("dirs", [])       # directories the setup created
        self.data.setdefault("packages", [])   # packages the setup installed (incl. dependencies)
        self.data.setdefault("enabled", [])    # units the setup enabled

    def save(self):
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST.write_text(json.dumps(self.data, indent=2) + "\n")

    def touch(self, path):
        """Call before writing path: backs up a file that existed before the setup first touched it."""
        path = Path(path)
        key = str(path)
        if key in self.data["files"]:
            return
        backup = None
        if path.exists() or path.is_symlink():
            dst = BACKUP / path.relative_to(HOME) if path.is_relative_to(HOME) else BACKUP / path.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dst, follow_symlinks=False)
            backup = str(dst)
        self.data["files"][key] = backup
        self.save()

    def created_dir(self, path):
        if str(path) not in self.data["dirs"]:
            self.data["dirs"].append(str(path))
            self.save()

    def add(self, key, items):
        for it in items:
            if it not in self.data[key]:
                self.data[key].append(it)
        self.save()


def write_file(man, path, content, mode=None):
    path = Path(path)
    if path.is_file() and not path.is_symlink() and path.read_text(errors="replace") == content:
        return False
    man.touch(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        path.unlink()
    tmp = path.with_name(path.name + ".rift-s-linux-tmp")
    tmp.write_text(content)
    if mode is not None:
        tmp.chmod(mode)
    os.replace(tmp, path)
    return True


def write_symlink(man, path, target):
    path = Path(path)
    if path.is_symlink() and Path(os.readlink(path)) == Path(target):
        return False
    man.touch(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        path.unlink()
    path.symlink_to(target)
    return True


# ----------------------------------------------------------------------------------------------
# Context: how a step logs, runs commands and asks questions (terminal or GUI)


class Ctx:
    def __init__(self, log, confirm):
        self.log = log            # log(text)
        self.confirm = confirm    # confirm(title, text, default_yes) -> bool
        self.manifest = Manifest()

    def run(self, cmd, cwd=None, env=None, timeout=None):
        """Run cmd, stream its output into the log (build progress thinned out), raise on failure."""
        cmd = [str(c) for c in cmd]
        shown = " ".join(c.replace(str(REPO) + "/", "") for c in cmd)
        self.log("$ " + (shown if len(shown) < 400 else shown[:400] + " …"))
        full_env = dict(os.environ, **(env or {}))
        try:
            p = subprocess.Popen(cmd, cwd=cwd, env=full_env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                                 errors="replace", bufsize=1)
        except OSError as e:
            raise StepFailed(f"cannot run {cmd[0]}: {e}")
        tail = []
        last_pct = -10
        t0 = time.monotonic()
        for line in p.stdout:
            line = line.rstrip()
            tail = (tail + [line])[-40:]
            m = re.match(r"^\[(\d+)/(\d+)\] ", line)          # ninja progress
            if m:
                pct = int(m.group(1)) * 100 // max(1, int(m.group(2)))
                if pct >= last_pct + 5:
                    last_pct = pct
                    self.log(f"  build {pct} % ({m.group(1)}/{m.group(2)}, {time.monotonic() - t0:.0f} s)")
                continue
            if line.startswith(("-- Installing: ", "-- Up-to-date: ", "-- Set non-toolchain portion")):
                continue
            self.log(line)
        rc = p.wait(timeout=timeout)
        if rc != 0:
            self.log("\n".join(tail[-15:]))
            raise StepFailed(f"{Path(cmd[0]).name} failed (exit {rc})")

    def download(self, url, dest, sha256):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.log(f"Downloading {url}")
        tmp = dest.with_name(dest.name + ".part")
        h = hashlib.sha256()
        with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done, last = 0, -25
            while chunk := r.read(1 << 20):
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if total and done * 100 // total >= last + 25:
                    last = done * 100 // total
                    self.log(f"  {last} % of {total / 1e6:.0f} MB")
        if sha256 and h.hexdigest() != sha256:
            tmp.unlink()
            raise StepFailed(f"checksum mismatch for {url} (expected {sha256}, got {h.hexdigest()})")
        os.replace(tmp, dest)


def git_checkout(ctx, src, repo, commit, branch=None):
    """Fresh checkout of commit in src (fetched shallow), local changes discarded."""
    if not (src / ".git").exists():
        src.mkdir(parents=True, exist_ok=True)
        ctx.run(["git", "init", "-q"], cwd=src)
        ctx.run(["git", "remote", "add", "origin", repo], cwd=src)
    rc, _ = quick(["git", "cat-file", "-e", f"{commit}^{{commit}}"], cwd=src)
    if rc != 0:
        try:
            ctx.run(["git", "fetch", "-q", "--depth", "1", "origin", commit], cwd=src, timeout=1800)
        except StepFailed:
            if not branch:
                raise
            ctx.run(["git", "fetch", "-q", "origin", branch], cwd=src, timeout=1800)
    quick(["git", "am", "--abort"], cwd=src)
    ctx.run(["git", "checkout", "-q", "-f", "--detach", commit], cwd=src)
    ctx.run(["git", "clean", "-fdq"], cwd=src)


def need_tools(*names):
    missing = [n for n in names if not shutil.which(n)]
    if missing:
        raise StepFailed(f"{', '.join(missing)} not found: run the Packages step first")


# ----------------------------------------------------------------------------------------------
# Steps


class Step:
    key = ""
    title = ""
    about = ""
    root = False          # asks for the admin password (pkexec)
    duration = ""         # rough time, shown on the button's tooltip
    check_only = False

    def check(self):
        raise NotImplementedError

    def install(self, ctx):
        raise NotImplementedError

    redo = "Reinstall"     # button text when nothing is missing

    def button(self, state):
        if self.check_only:
            return None
        return {MISSING: "Install", OUTDATED: "Update"}.get(state.kind, self.redo)


class SystemStep(Step):
    key = "system"
    title = "This computer"
    about = "Fedora, GPU, headset on USB. Nothing to install here."
    check_only = True

    def check(self):
        osr = os_release()
        details = [f"System: {osr.get('PRETTY_NAME', 'unknown')}, {os.uname().machine}"]
        kind = OK
        if osr.get("ID") != "fedora":
            kind = WARN
            details.append("Not Fedora: the setup only knows Fedora's package names. The steps are "
                           "described in docs/manual-setup.md for doing them by hand.")
        if os.uname().machine != "x86_64":
            kind = WARN
            details.append("Only tested on x86_64.")
        gpus = []
        for d in sorted(Path("/sys/class/drm").glob("card[0-9]/device/driver")):
            gpus.append(Path(os.readlink(d)).name)
        details.append("GPU driver: " + (", ".join(sorted(set(gpus))) or "none found"))
        if "nvidia" in gpus:
            details.append("NVIDIA: untested with this stack (direct mode needs DRM leasing).")
        session = os.environ.get("XDG_SESSION_TYPE", "?")
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "?")
        details.append(f"Session: {desktop} on {session} (tested: KDE Plasma on Wayland)")
        rc, out = quick(["lsusb"])
        if "2833:0051" in out:
            details.append("Rift S found on USB" + (" (USB 3)" if "2833:2051" in out else
                                                     " but only USB 2: use a USB 3 port / replug"))
        else:
            details.append("Rift S not plugged in (not needed for the setup)")
        steamvr = HOME / ".local/share/Steam/steamapps/common/SteamVR"
        if steamvr.exists():
            details.append("SteamVR is installed: don't start it, it takes over the OpenVR "
                           "registration from xrizer (the status window warns when it did).")
        return State(kind, "Fedora" if kind == OK else "Check the notes", details)


class PackagesStep(Step):
    key = "packages"
    redo = "Install"
    title = "Fedora packages"
    about = ("Build tools and libraries, Basalt (head tracking), udev rules for the headset. "
             "Asks for your password; shows first what dnf would upgrade.")
    root = True
    duration = "1-5 min"

    def button(self, state):
        return None if state.kind == OK else "Install"

    def wanted(self):
        pkgs = BUILD_PACKAGES + RUNTIME_PACKAGES + RECOMMENDED_PACKAGES
        if not VERSIONS["xrizer"]["prebuilt"]["url"]:
            pkgs = pkgs + ["cargo"]
        return pkgs

    def check(self):
        if not is_fedora():
            return State(WARN, "Not Fedora", ["Install the equivalent packages by hand (docs/manual-setup.md)."])
        missing = rpm_missing(self.wanted())
        if not missing:
            return State(OK, "All installed")
        req = [m for m in missing if m not in RECOMMENDED_PACKAGES]
        return State(MISSING if req else WARN,
                     f"{len(missing)} missing" if req else f"{len(missing)} optional missing",
                     ["Missing: " + ", ".join(missing)])

    def install(self, ctx):
        if not is_fedora():
            raise StepFailed("not Fedora: install the packages by hand (docs/manual-setup.md)")
        missing = rpm_missing(self.wanted())
        if not missing:
            ctx.log("All packages installed")
            return
        ctx.log("Asking dnf what it would install (nothing is changed yet)…")
        rc, out = quick(["dnf", "install", "--assumeno", *missing], timeout=600)
        tx = parse_dnf_transaction(out)
        if not any(tx.values()):
            ctx.log(out[-3000:])
            raise StepFailed("dnf could not resolve the packages (output above)")
        installs = [p for k, v in tx.items() if k.startswith("installing") for p in v]
        upgrades = tx.get("upgrading", []) + tx.get("downgrading", [])
        removes = tx.get("removing", [])
        text = f"dnf would install {len(installs)} packages ({len(missing)} asked for, the rest dependencies)."
        if upgrades:
            text += f"\n\nIt would also UPGRADE {len(upgrades)} installed packages:\n  " + ", ".join(upgrades)
        if removes:
            text += "\n\nIt would REMOVE:\n  " + ", ".join(removes)
        risky = [p for p in upgrades + removes if PROTECTED_RE.match(p)]
        if risky:
            text += ("\n\nCareful: that touches your desktop / graphics stack (" + ", ".join(risky) + "). "
                     "Upgrading those on their own, without a full system update, can leave the desktop "
                     "broken after the next reboot.\n\nRecommended: cancel, do a full system update "
                     "(Discover, or sudo dnf upgrade --refresh), reboot, and come back. Then nothing "
                     "needs upgrading here.")
            if not ctx.confirm("Packages would upgrade your desktop", text + "\n\nInstall anyway?", False):
                raise Cancelled()
        elif not ctx.confirm("Install Fedora packages", text + "\n\nYou will be asked for your password.", True):
            raise Cancelled()
        ctx.run(["pkexec", "dnf", "install", "-y", *missing], timeout=3600)
        ctx.manifest.add("packages", installs)


class MonadoStep(Step):
    key = "monado"
    redo = "Rebuild"
    title = "Monado (OpenXR runtime)"
    about = ("thaytan's Rift S controller tracking branch plus this repo's patches, built into "
             "~/.local/opt/monado-6dof.")
    duration = "10-30 min"
    marker = MONADO / "rift-s-linux.json"

    def wanted(self):
        v = VERSIONS["monado"]
        return {"commit": v["commit"], "patches": files_hash(PATCHES / "monado")}

    def check(self):
        if not MONADO_BIN.exists():
            return State(MISSING, "Not built")
        have = read_json(self.marker)
        if have is None:
            return State(WARN, "Installed, unknown version",
                         ["Not built by this setup. Rebuild builds the version in this repo."])
        if have != self.wanted():
            return State(OUTDATED, "Update available", ["The repo has newer patches than the installed build."])
        return State(OK, f"thaytan {have['commit'][:7]} + {len(list((PATCHES / 'monado').glob('*.patch')))} patches")

    def install(self, ctx):
        need_tools("git", "cmake", "ninja")
        v = VERSIONS["monado"]
        src, build = SRC / "monado", SRC / "monado-build"
        git_checkout(ctx, src, v["repo"], v["commit"], v.get("branch"))
        patches = sorted((PATCHES / "monado").glob("*.patch"))
        ctx.log(f"Applying {len(patches)} patches")
        try:
            ctx.run(["git", *CI_NAME, "am", "-q", "--committer-date-is-author-date", *patches], cwd=src)
        except StepFailed:
            quick(["git", "am", "--abort"], cwd=src)
            raise
        if not MONADO.exists():
            ctx.manifest.created_dir(MONADO)
        ctx.run(["cmake", "-S", src, "-B", build, "-G", "Ninja", "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
                 f"-DCMAKE_INSTALL_PREFIX={MONADO}", "-DXRT_HAVE_SYSTEMD=ON",
                 "-DXRT_INSTALL_ABSOLUTE_RUNTIME_PATH=ON", "-DCMAKE_INSTALL_RPATH=$ORIGIN",
                 "-DBUILD_TESTING=OFF"])
        ctx.log("Building Monado (takes a while; progress below)")
        ctx.run(["ninja", "-C", build], timeout=7200)
        ctx.run(["ninja", "-C", build, "install"])
        # Steam's container has no libcjson: ship it next to libopenxr_monado.so (RUNPATH $ORIGIN)
        cjson = Path("/usr/lib64/libcjson.so.1")
        if cjson.exists():
            shutil.copy2(cjson.resolve(), MONADO / "lib64/libcjson.so.1")
        self.marker.write_text(json.dumps(self.wanted()) + "\n")
        ctx.log("Monado installed. A rebuild removes the realtime priority: run that step again.")


class SetcapStep(Step):
    key = "setcap"
    redo = "Apply again"
    title = "Realtime GPU priority"
    about = "setcap CAP_SYS_NICE on monado-service. Asks for your password; needed again after every Monado build."
    root = True

    def check(self):
        if not MONADO_BIN.exists():
            return State(MISSING, "Needs Monado first")
        rc, out = quick(["getcap", MONADO_BIN])
        if "cap_sys_nice" in out:
            return State(OK, "Set")
        return State(MISSING, "Not set", ["Without it Monado only gets medium GPU queue priority."])

    def install(self, ctx):
        if not MONADO_BIN.exists():
            raise StepFailed("build Monado first")
        ctx.run(["pkexec", "setcap", "CAP_SYS_NICE=eip", MONADO_BIN], timeout=300)


class XrizerStep(Step):
    key = "xrizer"
    redo = "Rebuild"
    title = "xrizer (SteamVR games)"
    about = "Runs OpenVR (SteamVR) games on OpenXR, with the fixes Half-Life: Alyx needs. Never install SteamVR itself."
    duration = "3-10 min"
    marker = XRIZER / "rift-s-linux.json"

    def wanted(self):
        v = VERSIONS["xrizer"]
        return {"commit": v["commit"], "patches": files_hash(PATCHES / "xrizer"),
                "prebuilt": v["prebuilt"]["sha256"]}

    def check(self):
        if not XRIZER_SO.exists():
            return State(MISSING, "Not installed")
        have = read_json(self.marker)
        if have is None:
            return State(WARN, "Installed, unknown version", ["Not installed by this setup."])
        if have != self.wanted():
            return State(OUTDATED, "Update available")
        return State(OK, f"main {have['commit'][:7]} + PR 429 + GetPosesForFrame")

    def install(self, ctx):
        v = VERSIONS["xrizer"]
        if not XRIZER.exists():
            ctx.manifest.created_dir(XRIZER)
        XRIZER_SO.parent.mkdir(parents=True, exist_ok=True)
        tmp = XRIZER_SO.with_name("vrclient.so.rift-s-linux-tmp")
        if v["prebuilt"]["url"]:
            ctx.download(v["prebuilt"]["url"], tmp, v["prebuilt"]["sha256"])
        else:
            need_tools("git", "cargo")
            src = SRC / "xrizer"
            git_checkout(ctx, src, v["repo"], v["commit"])
            diffs = sorted((PATCHES / "xrizer").glob("*.diff"))
            for d in diffs:
                ctx.run(["git", "apply", d], cwd=src)
            if diffs:
                ctx.run(["git", *CI_NAME, "commit", "-q", "-am", "Apply " + ", ".join(d.name for d in diffs)], cwd=src)
            ctx.run(["git", *CI_NAME, "am", "-q", *sorted((PATCHES / "xrizer").glob("*.patch"))], cwd=src)
            # Static OpenXR loader: Steam's container has no libopenxr_loader
            ctx.run(["cargo", "xbuild", "--release", "--features", "static-openxr"], cwd=src, timeout=7200)
            shutil.copy2(src / "target/release/bin/linux64/vrclient.so", tmp, follow_symlinks=True)
        tmp.chmod(0o755)
        os.replace(tmp, XRIZER_SO)   # atomic: safe while a game has the old one loaded
        self.marker.write_text(json.dumps(self.wanted()) + "\n")


class WayVRStep(Step):
    key = "wayvr"
    title = "WayVR (in-VR menu)"
    about = "Wrist watch, desktop windows and playspace tools in VR. Official release, checksum-verified."
    duration = "1 min"

    def check(self):
        ver = VERSIONS["wayvr"]["version"]
        cur = WAYVR / "current"
        if not (cur / "AppRun").exists():
            return State(MISSING, "Not installed")
        target = os.readlink(cur) if cur.is_symlink() else ""
        if not target.startswith(ver + "/"):
            have = target.split("/")[0] or "unknown"
            return State(OUTDATED, f"{have} installed, {ver} in the repo")
        return State(OK, ver)

    def install(self, ctx):
        w = VERSIONS["wayvr"]
        ver = w["version"]
        if not WAYVR.exists():
            ctx.manifest.created_dir(WAYVR)
        img = WAYVR / f"WayVR-{ver}-x86_64.AppImage"
        if not img.exists() or sha256_file(img) != w["appimage"]["sha256"]:
            ctx.download(w["appimage"]["url"], img, w["appimage"]["sha256"])
        img.chmod(0o755)
        out = WAYVR / ver
        shutil.rmtree(out / "squashfs-root", ignore_errors=True)
        out.mkdir(parents=True, exist_ok=True)
        ctx.log("Unpacking the AppImage (the FUSE-mounted one crashes on every stop)")
        ctx.run([img, "--appimage-extract"], cwd=out)
        cur = WAYVR / "current"
        if cur.is_symlink() or cur.exists():
            cur.unlink()
        cur.symlink_to(f"{ver}/squashfs-root")
        ctx.download(w["wayvrctl"]["url"], WAYVR / "wayvrctl", w["wayvrctl"]["sha256"])
        (WAYVR / "wayvrctl").chmod(0o755)


def conf_env(text):
    return dict(re.findall(r'^\s*Environment="?([A-Za-z0-9_]+)=(.*?)"?\s*$', text, re.M))


REQUIRED_ENV = ("VIT_SYSTEM_LIBRARY_PATH", "RIFT_S_HANDTRACKING")


class ConfigStep(Step):
    key = "config"
    redo = "Apply again"
    title = "Connect everything"
    about = ("systemd units (Monado starts when a game does, WayVR with it), OpenXR / OpenVR runtime "
             "registration, Steam container access, menu entry. Replaced files are backed up.")

    def units(self):
        """(unit file in ~/.config/systemd/user, its content) for monado.service / .socket."""
        out = []
        for name in ("monado.service", "monado.socket"):
            src = MONADO_UNITS / name
            if src.exists():
                out.append((SYSTEMD_USER / name, src.read_text()))
        return out

    def problems(self):
        p = []
        units = self.units()
        if not units:
            p.append("Monado not built yet (its systemd units come from the build)")
        rc, out = systemctl("show", "monado.service", "-p", "ExecStart")
        if str(MONADO_BIN) not in out:   # the units may also come from Fedora's monado + a drop-in
            for path, content in units:
                if not path.exists() or path.read_text(errors="replace") != content:
                    p.append(f"{path.name} not installed")
        env = conf_env(RIFT_S_CONF.read_text()) if RIFT_S_CONF.exists() else {}
        if any(k not in env for k in REQUIRED_ENV):
            p.append("Monado settings (rift-s.conf) missing or incomplete")
        for rel in ("wayvr.service", "wayvr.service.d/50-stop.conf"):
            if not (SYSTEMD_USER / rel).exists():
                p.append(f"{rel} not installed")
        if not ENV_CONF.exists():
            p.append("Steam container access (environment.d) not set up")
        if not (ACTIVE_RUNTIME.exists() and ACTIVE_RUNTIME.resolve() == MONADO_MANIFEST.resolve()):
            p.append("OpenXR runtime does not point to this Monado")
        rt = (read_json(VRPATH, {}) or {}).get("runtime") or []
        if not rt or Path(rt[0]) != XRIZER:
            p.append("OpenVR runtime does not point to xrizer")
        if not DESKTOP_FILE.exists():
            p.append("No menu entry")
        for unit in ("monado.socket", "wayvr.service"):
            if not unit_enabled(unit):
                p.append(f"{unit} not enabled")
        return p

    def check(self):
        p = self.problems()
        if p:
            return State(MISSING, f"{len(p)} thing{'s' if len(p) != 1 else ''} to set up", p)
        if "monado_comp_ipc" not in os.environ.get("PRESSURE_VESSEL_FILESYSTEMS_RW", "") and not paths.TEST_HOME:
            return State(WARN, "Log out and in once",
                         ["Steam games can reach Monado only after a new login (environment.d)."])
        return State(OK, "Done")

    def install(self, ctx):
        man = ctx.manifest
        units = self.units()
        if not units:
            raise StepFailed("build Monado first")
        for path, content in units:
            write_file(man, path, content)
        # rift-s.conf: written once; later runs only add missing required settings and never change
        # values that are there (your tuning from the GUI's Tools tab stays)
        tmpl = (CONFIG / "systemd/monado.service.d/rift-s.conf").read_text()
        if RIFT_S_CONF.exists():
            cur = RIFT_S_CONF.read_text()
            have = conf_env(cur)
            add = [ln for ln in tmpl.splitlines()
                   if (m := re.match(r'\s*Environment="?([A-Za-z0-9_]+)=', ln))
                   and m.group(1) in REQUIRED_ENV and m.group(1) not in have]
            if add:
                write_file(man, RIFT_S_CONF, cur.rstrip("\n") + "\n" + "\n".join(add) + "\n")
                ctx.log("rift-s.conf: added " + ", ".join(conf_env("\n".join(add))))
        else:
            write_file(man, RIFT_S_CONF, tmpl)
        write_file(man, WAYVR_UNIT, (CONFIG / "systemd/wayvr.service").read_text())
        write_file(man, SYSTEMD_USER / "wayvr.service.d/50-stop.conf",
                   (CONFIG / "systemd/wayvr.service.d/50-stop.conf").read_text())
        write_file(man, ENV_CONF, (CONFIG / "environment.d/50-monado-vr.conf").read_text())
        write_symlink(man, ACTIVE_RUNTIME, MONADO_MANIFEST)
        vr = read_json(VRPATH, None) or {
            "version": 1, "jsonid": "vrpathreg",
            "config": [str(HOME / ".local/share/Steam/config")],
            "log": [str(HOME / ".local/share/Steam/logs")], "external_drivers": None}
        vr["runtime"] = [str(XRIZER)] + [r for r in vr.get("runtime") or [] if Path(r) != XRIZER]
        write_file(man, VRPATH, json.dumps(vr, indent=4) + "\n")
        write_file(man, DESKTOP_FILE, "\n".join([
            "[Desktop Entry]", "Type=Application", "Name=Rift S",
            "Comment=Status and setup for the Rift S VR stack (Monado, WayVR, xrizer)",
            f"Exec={REPO / 'rift-s-linux'}", "Icon=input-gaming", "Terminal=false",
            "Categories=Utility;System;", "StartupWMClass=vrstatus", ""]))
        ctx.log("Config files written")
        systemctl("daemon-reload")
        for unit in ("monado.socket", "wayvr.service"):
            if not unit_enabled(unit):
                rc, out = systemctl("enable", unit)
                ctx.log(f"enable {unit}: {out.strip() or 'ok'}")
                man.add("enabled", [unit])
        rc, out = systemctl("is-active", "monado.service")
        if out.strip() != "active":
            systemctl("restart", "monado.socket")
        else:
            ctx.log("Monado is running: restart it (Restart all) to use the new config")
        if "monado_comp_ipc" not in os.environ.get("PRESSURE_VESSEL_FILESYSTEMS_RW", ""):
            ctx.log("Log out and back in once, so Steam games can reach Monado.")


class ToolsStep(Step):
    key = "tools"
    redo = "Rebuild"
    title = "Test tools"
    about = "Small OpenXR programs for the head stillness, controller and vibration tests (Tools tab)."
    duration = "10 s"
    PROGRAMS = {"headrec": ["-lm"], "posrec": ["-lm"], "hapticstest": []}

    def check(self):
        stale = [n for n in self.PROGRAMS
                 if not (BIN / n).exists() or (BIN / n).stat().st_mtime < (TOOLS / f"{n}.c").stat().st_mtime]
        if not stale:
            return State(OK, "Built")
        return State(MISSING if not any((BIN / n).exists() for n in self.PROGRAMS) else OUTDATED,
                     "Not built" if len(stale) == len(self.PROGRAMS) else "Sources changed")

    def install(self, ctx):
        need_tools("gcc")
        BIN.mkdir(parents=True, exist_ok=True)
        for name, libs in self.PROGRAMS.items():
            ctx.run(["gcc", "-O1", "-Wall", TOOLS / f"{name}.c", "-o", BIN / name, "-lopenxr_loader", *libs])


STEPS = [SystemStep(), PackagesStep(), MonadoStep(), SetcapStep(), XrizerStep(), WayVRStep(),
         ConfigStep(), ToolsStep()]


def step(key):
    return next(s for s in STEPS if s.key == key)


def setup_complete():
    """True when nothing is missing (warnings / unknown versions are fine)."""
    return not any(s.check().needs_work for s in STEPS)


def install(ctx, keys=None):
    """Install the named steps (always), or every step that needs work. Returns True on success."""
    todo = [s for s in STEPS if (s.key in keys if keys else s.check().needs_work) and not s.check_only]
    if not todo:
        ctx.log("Nothing to do: everything is set up.")
        return True
    for s in todo:
        ctx.log(f"\n== {s.title}")
        try:
            s.install(ctx)
        except Cancelled:
            ctx.log(f"{s.title}: cancelled")
            return False
        except (StepFailed, OSError, subprocess.SubprocessError) as e:
            ctx.log(f"{s.title} FAILED: {e}")
            return False
        ctx.log(f"{s.title}: {s.check().text}")
    ctx.log("\nSetup finished." + ("" if keys else " Plug in the headset and start a game from Steam."))
    return True


def uninstall_plan():
    m = Manifest().data
    restore = [p for p, b in m["files"].items() if b]
    remove = [p for p, b in m["files"].items() if not b]
    dirs = [d for d in m["dirs"]] + [str(OWN), str(DATA)]
    return restore, remove, dirs, m["packages"], m["enabled"]


def uninstall(ctx, keep_recordings=True):
    restore, remove, dirs, pkgs, enabled = uninstall_plan()
    text = ("Stops Monado and WayVR and removes what the setup installed:\n"
            + "".join(f"\n  delete   {d}" for d in dirs)
            + "".join(f"\n  delete   {p}" for p in remove)
            + "".join(f"\n  restore  {p}" for p in restore)
            + ("\n\nController recordings are kept." if keep_recordings else "")
            + ("\n\nFedora packages stay installed (other programs may need them)." if pkgs else ""))
    if not ctx.confirm("Uninstall", text + "\n\nContinue?", False):
        raise Cancelled()
    systemctl("stop", "wayvr.service", "monado.service")
    for unit in enabled:
        systemctl("disable", unit)
    m = Manifest().data
    for p, backup in m["files"].items():
        p = Path(p)
        if p.exists() or p.is_symlink():
            p.unlink()
        if backup and (Path(backup).exists() or Path(backup).is_symlink()):
            p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, p, follow_symlinks=False)
            ctx.log(f"restored {p}")
        else:
            ctx.log(f"deleted {p}")
    systemctl("daemon-reload")
    keep = RECORDINGS if keep_recordings and RECORDINGS.exists() and any(RECORDINGS.iterdir()) else None
    for d in dirs:
        d = Path(d)
        if d == DATA and keep:
            for child in d.iterdir():
                if child != keep:
                    shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink()
        else:
            shutil.rmtree(d, ignore_errors=True)
        ctx.log(f"deleted {d}" + (" (except recordings)" if d == DATA and keep else ""))
    if pkgs:
        ctx.log("Packages installed by the setup (left in place): " + " ".join(pkgs))
    ctx.log("Uninstalled.")


# ----------------------------------------------------------------------------------------------
# Terminal


def terminal_confirm(title, text, default_yes):
    print(f"\n== {title}\n{text}")
    try:
        ans = input("[Y/n] " if default_yes else "[y/N] ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes") or (default_yes and ans == "")


def print_status():
    marks = {OK: "  ok", MISSING: "TODO", OUTDATED: " UPD", WARN: "WARN", INFO: "    "}
    for s in STEPS:
        st = s.check()
        print(f"[{marks[st.kind]}] {s.title:28} {st.text}")
        if st.kind != OK:
            for d in st.details:
                print(f"         {d}")


def main(argv):
    cmd, args = (argv[0], argv[1:]) if argv else ("status", [])
    ctx = Ctx(lambda t: print(t, flush=True), terminal_confirm)
    if cmd == "status":
        print_status()
    elif cmd == "install":
        bad = [a for a in args if a not in [s.key for s in STEPS]]
        if bad:
            sys.exit(f"unknown step {bad[0]}; steps: {', '.join(s.key for s in STEPS)}")
        sys.exit(0 if install(ctx, args or None) else 1)
    elif cmd == "uninstall":
        try:
            uninstall(ctx)
        except Cancelled:
            print("cancelled")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
