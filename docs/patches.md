# Patches

## Monado (`patches/monado/`)

On top of thaytan's `dev-constellation-controller-tracking` branch (commit b1c850a). The setup applies them with `git am`. 0001-0016 and 0040-0046 are this project's; 0017, 0018-0039, 0043 and 0044 come from lbgos/rift-s-linux, with their authorship where they are cherry-picks.

1. **0001 Controller linear velocity** from the optical fixes. The driver reported zero velocity, so throwing and flicking (e.g. Alyx's gravity gloves) didn't work. Superseded by the fusion filter, kept as the fallback.
2. **0002 IMU + optical error-state Kalman filter** for the controllers (`rift_s_controller_fusion.{h,cpp}`, unit tests in `tests/tests_rift_s_controller_fusion.cpp`).
3. **0003 Controller poses from the filter**, controller IMU clock sync, `RIFT_S_CTRL_FUSION=0` for the old behaviour, `RIFT_S_CTRL_RECORD=<dir>` CSV recorder, debug UI.
4. **0004 Reset only on consistent rejected fixes**, so the filter doesn't jump onto wrong LED matches. Most rejected fixes are wrong LED correspondences 35-100° off, which the old code passed straight through (jumps of up to ~1 m).
5. **0005 Keep the constellation tracker able to re-acquire**: fallback prior while not tracking, always-on accelerometer tilt correction, tilt check for (re)initialisation. A wrong filter state used as the tracker prior could otherwise lock the tracker out for 10+ s.
6. **0006 `RIFT_S_SLAM_CAMS=N`**: feed only the first N cameras to SLAM (CPU per camera count, headset static: 5 cams 2.6 cores, 3 cams 2.3, 2 cams 1.75).
7. **0007 Rift S lens model** replacing the CV1 placeholder distortion. Lens centres come from the headset firmware (per unit), the per-eye FOV from the official runtime's values (44/46/45/49° left eye, mirrored for the right). The radial curve isn't published: `c1 = 0.47` fits the eye-cup distances. `RIFT_S_DISTORTION_C1/_C2`, `RIFT_S_DISTORTION=cv1`.
8. **0008 Controller battery** via `xrt_device::get_battery_status` (WayVR's watch shows it), and a JSON status file `$XDG_RUNTIME_DIR/monado-rift-s-status.json` written 4x per second: head pose, proximity sensor, per controller radio / IMU / optical fix age, fusion state, battery. The status window reads it, so it never has to connect as an OpenXR client.
9. **0009 Geometry fixes ("T-rex arms")**: the HMD pose used `device_from_imu` instead of its inverse, which put the eyes ~14 cm too far forward and ~4 cm too low relative to the controllers. The controller LED model was moved to the IMU frame with the wrong sign (~4.5 cm off). Replaying recordings with a lever-arm offset confirms the fix. Recordings made before 0009 need `lever=2*ImuPosition` in the replay tool.
10. **0010 Passthrough, driver side**: `xrt_device::get_passthrough_frames` (latest front camera images with capture time, camera pose, Fisheye62 intrinsics). Images are only copied while someone asked for them in the last 500 ms. Holding the right Oculus button toggles passthrough (`RIFT_S_PASSTHROUGH_HOLD_MS`). The HMD also offers the `ALPHA_BLEND` blend mode.
11. **0011 Passthrough, compositor side** (compute path only): a pseudo layer in `layer.comp`. It sits behind all layers for alpha-blend frames, or over the game but under overlays (WayVR) when forced by the button. Each eye ray hits a sphere around the eye (`XRT_PASSTHROUGH_DISTANCE_M`) and is projected into the camera with the head pose *at capture time*, so the room stays world-stable. Costs nothing while off.
12. **0012 Tracker diagnostics** for the recorder: how each pose was found (prior / last seen / labelled / search), matched LEDs, reprojection error, frames received vs processed, search times. Also a `tracker` block in the status file.
13. **0013 Tracking fixes**: the tracker refines *tracked* priors (mutual nearest-neighbour LED/blob pairs + RANSAC-PnP that must agree with the prior), and no longer submits unrefined or stale poses. The fusion only resets onto fixes within 20° of the gyro orientation while that is trusted. In smooth motion, wrong matches dropped from 12-18 % to 4-6 % in the test protocol. `CONSTELLATION_PRIOR_REFINE=0` = old behaviour.
14. **0014 Wrong poses are no longer kept alive**: when the fusion rejects a pose, the tracker drops that pose's blob labels and lets the other strategies try. The fallback prior only comes from accepted fixes.
15. **0015 Controller vibration**: the motor register written over the radio (`97 01 08 00 <amplitude>`, format from [rift-s-linux](https://github.com/ryaneford/rift-s-linux)). The motor latches the amplitude, so the driver writes changes plus an explicit 0 at the end, on shutdown and when a controller appears. `RIFT_S_HAPTICS_SCALE`, `RIFT_S_HAPTICS_MIN_MS`, `RIFT_S_HAPTICS_LOG`.
16. **0016 Vibration tuned from a game log**: Half-Life: Alyx sends several requests per frame (one per effect) and pulses of one frame each. Requests within 3 ms are now merged (strongest wins), and stops can't end a pulse before `RIFT_S_HAPTICS_MIN_MS` (40 ms). Played back from the log, that is about twice the vibration with a third of the motor on/off switches.

17. **0017 Lens model fitted to the Oculus runtime's distortion mesh** (`RIFT_S_DISTORTION=mesh`, the default): an odd polynomial per colour channel (t to t⁹), within 0.45 px of the mesh, and the lens centres the runtime uses for the eye separation (`RIFT_S_LENS_IPD_MM`, 63). The coefficients and centres are from [lbgos/rift-s-linux](https://github.com/lbgos/rift-s-linux) (`rift_s_optics.c`). The c1 model of 0007 undercorrected the far edges: its fit treated the vertical eye-cup distances as FOV edges. In Half-Life: Alyx the edges are straight now, with no morphing. `RIFT_S_DISTORTION_C1` without `RIFT_S_DISTORTION` still selects the c1 model.
18. **0018-0039 Hardening of thaytan's driver and tracker**, cherry-picked from lbgos/rift-s-linux with their authorship (0039 by Ryan Ford):
    - length and bounds checks in the controller report, radio, firmware block and camera calibration parsers (a device-controlled length could overflow a 48-byte stack buffer);
    - a controller index assert that could never fail;
    - the blobwatch observation queue released under its lock (two threads could hand one buffer to two samples);
    - device and LED model limits;
    - a radio queue with a 1.5 s deadline and HID error backoff (a lost reply no longer wedges the queue);
    - a v4l2 use-after-free and thread / mmap leaks on reconnect;
    - a camera clock resync when the IMU clock is a 2³² µs epoch ahead at startup (otherwise no head tracking on some starts).
19. **0040-0042 Follow-ups to those:** camera debug sinks destroyed; the resync frame itself is kept out of SLAM (its time is wrong); the radio queue unit tests built.
20. **0043 Oculus aim and grip offsets** instead of the WMR placeholder, from lbgos/rift-s-linux: the render model's place in the device frame (Oculus' SteamVR driver), the OpenXR grip of the render model, and the Windows aim offset. Aim is 5.5 cm in front of the device origin, grip is 60° up at (0, -3, +4) cm (was 40° at (0, 0, +10) cm). It is the grip xrizer's Touch profile assumes, so hands in OpenVR games sit and angle like on Windows. `RIFT_S_POSE_OFFSETS=wmr` restores the old offsets.
21. **0044-0045 Occlusion motion models** from lbgos/rift-s-linux (`rift_s_motion.cpp`, unchanged, with their tests) and a filter API to evaluate them offline (`tools/dev/motion-replay.cpp`). **Not used by the driver:** replayed on protocol recordings, they did not beat this repo's fusion filter. Synthetic gaps had 2.2 → 2.8 cm mean error, and the near-head model teleported once. They are kept for further work.
22. **0046 Re-acquisition lockout fix:** the prior of a lost controller is its last accepted pose (0014). If that pose was a wrong LED match, the full search, which only accepts poses near the prior, never found the controller again. One protocol recording had 570 failed searches in 19 s with the controller in view. After 0.5 s without an accepted pose, the search now takes results up to 2 m from the prior if at least 5 LEDs match, and the filter re-initialises from them after 1.5 s. In the reach segment of the protocol, the longest gaps went from 4 s / 11 s to 3.3 s / 2.2 s. `CONSTELLATION_WIDE_SEARCH=0` disables it. Counted as `long_wide` in the status file.

Together, 0017-0046 plus the patched Basalt against 0001-0016 with Fedora's Basalt, in the same (dark) room, from the tracking protocol:
- fast moves: dropouts 10-12 → 2-4 per minute, wrong matches 6-7 % → 2-3 %, jitter ~4 → ~2.5 mm;
- smooth circles: wrong matches 4-6 % → 1.6-3 %;
- watch gesture: right controller dropouts 12 → 0 per minute;
- reach to the sides and behind the back: both controllers tracked 38 → 71 % of the time.

## Basalt (`patches/basalt/`)

From [lbgos/rift-s-linux](https://github.com/lbgos/rift-s-linux) (`patches/basalt`), for [Monado's Basalt fork](https://gitlab.freedesktop.org/mateosss/basalt) at df6e970, the commit Fedora's `basalt-monado` packages. The setup builds it into `~/.local/opt/basalt-rifts` and keeps Fedora's package as the fallback.

1. **0001** IMU frequency per tracker instead of a function-static variable (broke recreating the tracker).
2. **0002** A thread budget for driver-provided calibration, which had no limit on this path.
3. **0003** Safe stop / recreation (the shutdown segfault when Monado closed before the stream started), drop-oldest input queues, and 4 worker threads (`BASALT_MAX_THREADS` 1-4).
4. **0004** No ever-growing execution statistics in live trackers (memory in long sessions).
5. **0005** A live camera queue of one frame: stock Basalt queued up to 10 frames (333 ms) when it fell behind.

Monado's whole CPU load while looking around dropped from ~2.6 to ~1.25 cores on a Ryzen AI 300 laptop.

## xrizer (`patches/xrizer/`)

On top of xrizer `main` at 0519ded.

- **0001** [PR #429](https://github.com/Supreeeme/xrizer/pull/429) by nokichan: action binding / origin queries. Fixes the Half-Life: Alyx level-load hang ([xrizer#435](https://github.com/Supreeeme/xrizer/issues/435)).
- **0002** `GetPosesForFrame` instead of `todo!()`: Alyx calls it and xrizer panicked ([xrizer#448](https://github.com/Supreeeme/xrizer/issues/448)).

Built with `cargo xbuild --release --features static-openxr`. The static OpenXR loader is required because Steam's container has no `libopenxr_loader`.

## Upstreaming

Candidates: the fusion work (0002-0005) and the lockout fix (0046) for thaytan's branch, `RIFT_S_SLAM_CAMS` (0006), and `GetPosesForFrame` for xrizer. The lens model, the grip offsets, the hardening and the Basalt patches come from lbgos/rift-s-linux, so they are theirs to upstream.
