# Configuration

Monado's settings live in `~/.config/systemd/user/monado.service.d/rift-s.conf` as `Environment="NAME=value"` lines. The Tools tab of the status window edits the common ones and restarts Monado. After editing by hand: `systemctl --user daemon-reload`, then restart Monado. Re-running the setup never changes values you set. It only updates install paths that moved (the Basalt library) and settings still at an old default that has a better one now (`RIFT_S_DISTORTION_C1=0.47` becomes `RIFT_S_DISTORTION=mesh`), and says so in its log.

## Defaults

| Setting | Default | Meaning |
|---|---|---|
| `VIT_SYSTEM_LIBRARY_PATH` | `~/.local/opt/basalt-rifts/lib64/libbasalt.so.2` | Basalt SLAM library (required): the patched build from the setup, or Fedora's `/usr/lib64/libbasalt.so.2` until that exists |
| `RIFT_S_HANDTRACKING` | `0` | Hand tracking needs ONNX models that aren't packaged; on, Monado crashes (required) |
| `XRT_COMPOSITOR_SCALE_PERCENTAGE` | `100` | Render resolution. 100 = 1280x1440 per eye; Monado's default 140 looks sharper but costs about twice the pixels. Fine on a strong GPU. |
| `U_PACING_COMP_MIN_TIME_MS` | `8` | Compositor time budget per frame. Monado's default of 3 ms missed about half the vblanks on an AMD iGPU (judder on head turns). |
| `RIFT_S_SLAM_CAMS` | `5` | Cameras fed to head tracking. 2 (front pair) saves ~0.8 CPU cores but stutters on head turns. |
| `RIFT_S_DISTORTION` | `mesh` | Rift S lens model fitted to the Oculus runtime's distortion mesh (patch 0017). Unset, with `RIFT_S_DISTORTION_C1=0.47`, is the older one-term model; `cv1` is the original CV1 placeholder. Tools tab: "Lens model". |

## More settings (patches in this repo)

| Setting | Default | Meaning |
|---|---|---|
| `RIFT_S_CTRL_FUSION` | `1` | `0` = controller poses straight from the cameras, without the IMU fusion filter |
| `CONSTELLATION_PRIOR_REFINE` | `1` | `0` = original constellation tracker behaviour (A/B tests) |
| `CONSTELLATION_WIDE_SEARCH` | `1` | `0` = a lost controller is only searched near its last accepted pose (before patch 0046) |
| `RIFT_S_POSE_OFFSETS` | Oculus | `wmr` = the old placeholder aim / grip offsets instead of the Oculus ones (patch 0043) |
| `RIFT_S_LENS_IPD_MM` | `63` | Eye separation the mesh lens model picks its lens centres for (the views render with 63 mm) |
| `BASALT_MAX_THREADS` | `4` | Worker threads of the patched Basalt (1-4). Fewer saves CPU, more keeps up better when loaded. |
| `RIFT_S_HAPTICS_SCALE` | `1.0` | Vibration strength, `0` disables |
| `RIFT_S_HAPTICS_MIN_MS` | `40` | Shortest vibration pulse. Games send 1-11 ms pulses every frame; try 30 if taps feel mushy. |
| `RIFT_S_HAPTICS_LOG` | – | Path: log every vibration request as CSV (for tuning) |
| `RIFT_S_PASSTHROUGH_HOLD_MS` | `600` | How long to hold the right Oculus button for passthrough; `0` disables the button |
| `XRT_PASSTHROUGH_DISTANCE_M` | `2` | Distance the camera image is projected at (near things look a bit off) |
| `XRT_PASSTHROUGH_GAIN` | `1.0` | Passthrough brightness |
| `XRT_PASSTHROUGH_FADE_S` | `0.15` | Fade time |
| `XRT_PASSTHROUGH_STATS` | `0` | `1` = log passthrough statistics once a second |
| `RIFT_S_STATUS_FILE` | `$XDG_RUNTIME_DIR/monado-rift-s-status.json` | Live status for the window; empty disables |
| `RIFT_S_CTRL_RECORD` | – | Directory: record controller IMU + camera data (Tools tab: "Start controller recording") |

## WayVR

- Starts and stops with Monado (`wayvr.service`, `BindsTo=monado.service`). Untick "Start together with Monado" on the Tools tab to start it yourself.
- Headless by default. "Desktop capture" on the Tools tab also shows your desktop screens in VR (costs GPU).
- WayVR's own setting "Enable passthrough" makes the room its background when no game runs; the Oculus-button passthrough works either way.
- WayVR's config: `~/.config/wayvr/`.
