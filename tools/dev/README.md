# Developer tools

For working on controller tracking, vibration and frame pacing. You don't need them for playing. `$M` is the Monado source tree (`~/.local/share/rift-s-linux/src/monado`, build in `../monado-build`).

## Recording and analysing controller tracking

- **Recording:** the status window's Tools tab, "Start controller recording", restarts Monado with `RIFT_S_CTRL_RECORD=~/.local/share/rift-s-linux/recordings/ctrlrec-<time>` (controller IMU + optical fixes as CSV). Start it before the game, because it restarts Monado.
- **`ctrl-protocol.py [--label NAME]`**: guided tracking protocol (~4 min, spoken cues in the headset). It covers holding still, smooth circles, fast moves, the WayVR watch gesture, hands close together, and reaching to the sides and behind the back. Segment times go to `segments.csv` in the active recording. Also a button on the Tools tab, which runs the analysis afterwards.
- **`ctrl-analyze.py <recording> [--compare <recording>] [--segments-only] [--replay-args "k=v ..."] [--compare-args "k=v ..."]`**: per controller and protocol segment it reports:
  - fix rate, accepted %, latency
  - dropouts (> 0.2 s without an accepted fix), wrong matches (> 20° from the filter), implausible fixes
  - excursions, tracked % and jitter of the replayed 80 Hz filter output, controller separation
  - tracker dropped frames and search times

  Filter metrics come from replaying the recording through the current filter, so `--compare-args` A/Bs filter parameters on one recording. Needs `replay` (below).
- **`replay.cpp`**: offline replay of a recording through the fusion filter, with `key=value` parameter overrides, `skip_s=`, `imu_offset_ms=`, `accel_scale=`, `lever=x,y,z`, `--out out.csv` and `--trace out.csv`. Build:

  ```bash
  g++ -O2 -std=c++17 -I$M/src/xrt/drivers/rift_s -I$M/src/xrt/include -I$M/src/xrt/auxiliary -I$M/../monado-build/src/xrt/include -I/usr/include/eigen3 replay.cpp $M/src/xrt/drivers/rift_s/rift_s_controller_fusion.cpp $M/../monado-build/src/xrt/auxiliary/math/libaux_math.a -o replay
  ```

  The fusion filter has unit tests too: reconfigure the build with `-DBUILD_TESTING=ON` (the setup builds without), then `ctest -R rift_s` in the build directory.

## Vibration

- **`hapticsplay.c`**: plays a timed vibration script (`<t_ms> <L|R> <amplitude> <duration_ms>` per line). `gcc -O1 -Wall hapticsplay.c -o hapticsplay -lopenxr_loader`
- **`haptics-ab.py [haptics.csv]`**: A/B of two driver logics. It takes the most intense 15 s of a `RIFT_S_HAPTICS_LOG` recording and plays them on both controllers at once (left: 0015 logic, right: 0016), simulated as the exact motor timeline. `--dry-run` only prints the comparison.

## Other

- **`run-monado.sh <fusion 0|1> <record dir or ""> <log>`**: run Monado by hand (stop the systemd units first).
- **`measure-pacing.sh <ms> ...`**: missed frames per compositor budget, with `hello_xr`.
- **`measure-slam-cpu.sh`**: Monado CPU per SLAM camera count.
- **`dump_fw_blocks.py <hidraw> <dir>`**: read the headset's firmware blocks (lens and IMU calibration). Monado must not run. **The dumps contain the headset's serial numbers, so don't publish them.**

Spoken cues go to the Rift S speakers with espeak-ng + paplay (`pactl list short sinks | grep Rift_S`).
