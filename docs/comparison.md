# Comparison: this project vs. lbgos/rift-s-linux

There are two independent efforts to make the Rift S fully usable on Linux, and both are worth a look. Pick the one that fits your setup:

- **Ours (this repo):** a standalone stack of Monado + xrizer + WayVR, without SteamVR. Developed on Fedora with an AMD integrated GPU.
- **Theirs:** [lbgos/rift-s-linux](https://github.com/lbgos/rift-s-linux), Monado's Rift S driver loaded into SteamVR. Developed on Arch with an NVIDIA RTX 3090.

Both start from the same commit of thaytan's controller-tracking branch (`b1c850a`).

*Code comparison from 2026-10-25, based on reading the source, not on using theirs in the headset. Both projects move fast, so details may be outdated.*

**Rough guide:** if you want SteamVR (its dashboard, chaperone, native OpenVR) and the most features, try theirs. If you want a light stack without SteamVR, e.g. on a laptop iGPU, try this one.

### Legend

| Lead | Meaning | | Cell | Meaning |
|---|---|---|---|---|
| 🟢 | **We lead** | | ✅ | Has it |
| 🟠 | **They lead** | | 🟡 | Partly / basic |
| ⚖️ | **Even**: different approaches, both good | | ❌ | Missing |
| 🟰 | **Same**: identical or equivalent | | | |

---

## 🗺️ At a glance

| | Rows | Main areas |
|---|:---:|---|
| 🟢 We lead | **7** of 37 | Haptics, frame pacing, light stack for the iGPU, validation on real data, patches you can review |
| 🟠 They lead | **19** of 37 | SLAM recovery, Basalt fixes, room setup, occlusion, lens, IPD, grip, reconnect, pairing, Settings, packaging, unit tests, hardening |
| ⚖️ Even | **7** of 37 | Stack choice, controller filter, LED-match rejection, constellation tracker, IMU bias, passthrough, status UI |
| 🟰 Same | **4** of 37 | Basalt base, geometry fix, battery, FOV |

> **In short:** theirs has more features and more polish for end users. Ours has a measured core (filter, haptics, pacing) and is lighter on integrated GPUs. Their biggest advantage is the SLAM guard, which recovers from head-tracking runaway automatically.

---

## 🧱 Foundation

| Area | Lead | Ours | Theirs |
|---|:---:|---|---|
| Runtime stack | ⚖️ | ✅ Own Monado compositor, xrizer for OpenVR games, no SteamVR | ✅ SteamVR compositor, dashboard and chaperone; OpenVR games native, no xrizer |
| Weight on the iGPU | 🟢 | ✅ Light: one compositor, tuned for the APU | 🟡 SteamVR compositor on top; Basalt with 4 threads on all 5 cameras |
| Frame pacing | 🟢 | ✅ Pacer tuned and measured (8 ms budget, 0 missed vblanks) | 🟡 SteamVR's async reprojection, untuned for AMD |
| Long-term maintenance | 🟢 | ✅ Upstream Monado APIs only | 🟡 A Vulkan layer hooks SteamVR's compositor, pinned to known SteamVR builds |

## 👓 Head tracking (SLAM)

| Area | Lead | Ours | Theirs |
|---|:---:|---|---|
| SLAM base (Basalt) | 🟰 | ✅ Basalt, 5 cameras (`RIFT_S_SLAM_CAMS` knob) | ✅ Basalt, 5 cameras |
| Detect divergence | 🟠 | 🟡 Shown by the status window (head runs away / drifts) | ✅ Distance, jump, speed and floor-height checks |
| Recover from divergence | 🟠 | ❌ "Restart all" by hand | ✅ Holds a 3DoF pose, restarts Basalt, realigns without a jump |
| Cameras covered / face down on desk | 🟠 | ❌ (a suspected trigger for runaway) | ✅ Holds the pose until features come back |
| Basalt bug fixes | 🟠 | ❌ Stock Fedora package | ✅ 5 patches: stats memory leak, 1-frame input queue (latency), safe restart |
| Room Setup, boundary, saved rooms | 🟠 | 🟡 WayVR playspace reset / recenter only | ✅ Room Setup in the headset, saved boundary, room recognised on later starts |

## 🎮 Controllers

| Area | Lead | Ours | Theirs |
|---|:---:|---|---|
| Filter design | ⚖️ | ✅ One joint filter (ESKF): orientation, position, velocity and biases together | ✅ Separate: 3DoF orientation plus optical nudges, position filter per axis |
| Rejecting wrong LED matches | ⚖️ | ✅ Resets only within 20° of the gyro; rejections reported back to the tracker (0014) | ✅ Needs 3 agreeing fixes; ambiguity and goodness-of-fit tests |
| Constellation tracker | ⚖️ | ✅ Thaytan's solver plus prior refinement (0013) | ✅ New gravity-based solver (pose from 2 LEDs) |
| Occlusion (near face, two hands) | 🟠 | 🟡 Coasts on the IMU, then holds | ✅ Motion models: near head, hand over hand, behind back, out of view, at rest |
| Geometry fix ("T-rex arms") | 🟰 | ✅ Patch 0009 | ✅ Same two bugs found and fixed |
| Grip / aim offset | 🟠 | 🟡 WMR placeholder | ✅ The offset the Windows runtime uses |
| IMU bias | ⚖️ | ✅ Estimated live in the filter | ✅ Estimated, and saved to disk per controller |
| Haptics | 🟢 | ✅ Requests merged per frame (strongest wins), 40 ms minimum, tuned from an Alyx log and confirmed by feel | 🟡 Last request wins, 5 ms minimum; better retry and reconnect handling |
| Battery level | 🟰 | ✅ | ✅ |
| Controller pairing | 🟠 | ❌ | ✅ In Settings |

## 🔍 Display and optics

| Area | Lead | Ours | Theirs |
|---|:---:|---|---|
| Lens distortion | 🟠 | 🟡 One radial term (c1 = 0.47), fitted to the firmware eye-cup edges | ✅ Polynomial per RGB channel, fitted to Oculus's own distortion mesh (< 0.45 px error) |
| Colour fringing correction | 🟠 | 🟡 One scale per colour | ✅ A full curve per colour |
| FOV | 🟰 | ✅ Official Oculus values (hmdgdb) | ✅ Same source |
| IPD | 🟠 | ❌ Fixed 63 mm | ✅ Software IPD; the lens centre shifts with it like on Windows |
| Passthrough | ⚖️ | ✅ Hold the Oculus button, shown over the game, WayVR stays on top | ✅ Exposed to SteamVR as its camera (works with SteamVR's own room view) |

## 🖥️ Apps, UI and system

| Area | Lead | Ours | Theirs |
|---|:---:|---|---|
| Desktop inside VR | 🟠 | 🟡 WayVR (third-party) | ✅ Their own overlay in the SteamVR dashboard (KWin, mouse, keyboard) |
| Settings app | 🟠 | ❌ | ✅ Device state, IPD, recenter, Room Setup, 80 fps headset preview |
| Status / diagnostics UI | ⚖️ | ✅ Status window: health lights, fix buttons, test tools, spoken alerts | 🟡 Logs plus a triage command that starts a coding agent |
| USB / DisplayPort reconnect | 🟠 | ❌ Restart Monado | ✅ Reconnects without a restart |
| Laptop suspend | 🟠 | ❌ Restart Monado | 🟡 Display recovery (not tested on suspend) |
| Proximity sensor → standby | 🟠 | 🟡 Reported in the status file only | ✅ Tells SteamVR to go into standby |
| Install and update | 🟠 | ✅ Setup tab builds and installs everything per user, with uninstall; Fedora only, builds from source | ✅ Arch package, universal tarball, `rifts-setup update` |

## 🧪 Code quality

| Area | Lead | Ours | Theirs |
|---|:---:|---|---|
| Can the changes be reviewed? | 🟢 | ✅ 16 focused patches, each with its rationale | 🟡 The core is one squashed ~30k-line commit |
| Validation on real data | 🟢 | ✅ Recordings, offline replay, guided protocol, A/B in the headset | 🟡 Mostly synthetic; comments say constants are "pending measurement" |
| Unit tests | 🟠 | 🟡 11 filter tests | ✅ ~315 test cases (~18k lines) |
| Hardening of thaytan's code | 🟠 | ❌ Bugs still present | ✅ Fixed: blobwatch race, report buffer overflow, an assert that never fires, v4l2 use-after-free on reconnect |
| File size / readability | 🟢 | ✅ Small additions | 🟡 Several files of 1.5k–2.5k lines |

---

## 🎯 Ideas this project could adopt from theirs

| # | What | Effort | Would help with |
|:---:|---|:---:|---|
| 1 | 🐛 Hardening fixes in thaytan's code | 🟢 Small | Crashes, tracker corruption |
| 2 | 🧠 Basalt patches (stats leak, 1-frame input queue) | 🟢 Small | Memory growth, latency when the CPU is loaded |
| 3 | 🔍 Lens coefficients + IPD table | 🟢 Small | Sharper edges, less colour fringing |
| 4 | ✋ Grip offset | 🟢 Small | Hand angle and position in games |
| 5 | 🛟 SLAM guard | 🟠 Medium | Head-tracking runaway |
| 6 | 🙌 Occlusion motion models | 🔴 Large | Near-face and two-hand dropouts |

*Both projects build on BSL-1.0 code, so ideas and code can flow both ways.*
