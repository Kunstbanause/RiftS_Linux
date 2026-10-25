"""Where everything lives. Shared by the status GUI and the setup.

Everything is installed per user: programs under ~/.local/opt, data under ~/.local/share/rift-s-linux,
config under ~/.config. RIFTS_HOME=<dir> moves all of it into a fake home (for testing the setup
without touching the real one; systemctl calls are skipped then).
"""

import os
from pathlib import Path

APP = Path(__file__).resolve().parent
REPO = APP.parent
TOOLS = REPO / "tools"
CONFIG = REPO / "config"
PATCHES = REPO / "patches"

TEST_HOME = os.environ.get("RIFTS_HOME")
HOME = Path(TEST_HOME) if TEST_HOME else Path.home()

OPT = HOME / ".local/opt"
OWN = OPT / "rift-s-linux"                       # our own programs: GUI venv, test tools
VENV = OWN / "venv"
BIN = OWN / "bin"                                # compiled test tools (headrec, posrec, hapticstest)
DATA = HOME / ".local/share/rift-s-linux"
SRC = DATA / "src"                               # Monado / xrizer source + build trees
BACKUP = DATA / "backup"                         # config files we replaced
MANIFEST = DATA / "manifest.json"                # what the setup wrote / installed
RECORDINGS = DATA / "recordings"
FIRMWARE = DATA / "hmd-firmware"

MONADO = OPT / "monado-6dof"
MONADO_CTL = MONADO / "bin/monado-ctl"
MONADO_BIN = MONADO / "bin/monado-service"
LIBMONADO = MONADO / "lib64/libmonado.so"
MONADO_MANIFEST = MONADO / "share/openxr/1/openxr_monado.json"
MONADO_UNITS = MONADO / "lib/systemd/user"
XRIZER = OPT / "xrizer"
XRIZER_SO = XRIZER / "bin/linux64/vrclient.so"
WAYVR = OPT / "wayvr"

CFG = HOME / ".config"
ACTIVE_RUNTIME = CFG / "openxr/1/active_runtime.json"
SYSTEMD_USER = CFG / "systemd/user"
RIFT_S_CONF = SYSTEMD_USER / "monado.service.d/rift-s.conf"
WAYVR_UNIT = SYSTEMD_USER / "wayvr.service"
WAYVR_MODE_CONF = SYSTEMD_USER / "wayvr.service.d/mode.conf"
ENV_CONF = CFG / "environment.d/50-monado-vr.conf"
VRPATH = CFG / "openvr/openvrpaths.vrpath"
XRIZER_LOG = HOME / ".local/state/xrizer/xrizer.txt"
DESKTOP_FILE = HOME / ".local/share/applications/rift-s-linux.desktop"

RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
STATUS_FILE = RUNTIME_DIR / "monado-rift-s-status.json"
