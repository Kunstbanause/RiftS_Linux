# Troubleshooting

The status window (`rift-s-linux`) catches most of these and shows the fix on the dashboard. **Restart all** (top right) restarts Monado and WayVR and fixes most problems. Restart the game afterwards.

| Symptom | Cause | Fix |
|---|---|---|
| World or menus slowly float away, grey or black void in the game, controllers "fall into the distance" | Head tracking (Basalt SLAM) diverged | Restart all, then restart the game. Start Monado with the headset on your head rather than lying on the desk. The Tools tab has a head stillness test. |
| Half-Life: Alyx stays in the grey loading room (xrizer log ends at "recreating swapchain") | unknown | Restart all + restart Alyx |
| After laptop suspend: black headset, games never become active | Monado lost the headset display | Restart all |
| Laptop suspends while playing ("Lid closed" in the journal) | The lid sensor triggered (the headset's speaker magnets?) | Keep the headset away from the lid, or enable "Block lid switch / sleep while Monado runs" on the Tools tab |
| "Didn't find Rift S camera device", no tracking after a reboot | The headset came up on USB 2 only | Replug the USB plug firmly; it needs a USB 3 port |
| One controller dead, or tracking with gaps | Flat AA battery | Replace the battery (the dashboard shows the level) |
| About 1 fps, huge lag (laptop) | Power profile is not Performance, so the GPU is held at a low clock | Set Performance (button on the dashboard) |
| Judder when turning the head | The compositor misses frames | Raise "Compositor budget" on the Tools tab (default 8 ms). The Details tab shows missed frames per second. |
| Games say no headset / OpenVR games start in desktop mode | Runtime registration lost (often: SteamVR was started and took over) | Setup tab: Connect everything |
| Proton / Steam Linux Runtime games can't connect to Monado | Steam's container can't see the Monado socket | Log out and in once after the setup (environment.d). The Details tab checks it. |
| xrizer error dialog "not yet implemented" | The game calls an OpenVR function xrizer doesn't have yet | See `~/.local/state/xrizer/xrizer.txt`, report it to xrizer |
| GPU queue priority MEDIUM instead of REALTIME (Details tab) | `setcap` lost: every Monado rebuild removes it | Setup tab: Realtime GPU priority |
| No permission on `/dev/hidraw*` | udev rules missing | Setup tab: Fedora packages (`xr-hardware`), then replug the headset |
| Head tracking feels laggier or uses more CPU after the Basalt update | The patched Basalt uses 4 worker threads | Try `BASALT_MAX_THREADS=2` or `3` in `rift-s.conf`, or set `VIT_SYSTEM_LIBRARY_PATH=/usr/lib64/libbasalt.so.2` for Fedora's Basalt (Configuration doc) |
| Out of memory while the setup builds Basalt | Basalt's files need several GB each to compile | The setup uses at most 3 jobs, fewer when memory is low; close big programs (browser, games) during that step |
| A controller jumps away and stays lost for many seconds although it is in view | A wrong LED match was accepted and the search stayed near it (fixed in patch 0046) | Should recover within ~1.5 s now; `CONSTELLATION_WIDE_SEARCH=0` turns the fix off if it causes trouble |

## Logs

- Monado: `journalctl --user -u monado.service -f` (or "Monado log" on the Tools tab)
- WayVR: `journalctl --user -u wayvr.service -f`
- xrizer: `~/.local/state/xrizer/xrizer.txt`
- Status report without the window: `rift-s-linux --once`

## Notes

- `monado-service` is started on demand by `monado.socket` when a game connects. It keeps about 1.5 CPU cores busy until you stop it (about 3 with Fedora's Basalt).
- Never install or start SteamVR alongside this. It rewrites `~/.config/openvr/openvrpaths.vrpath` (disabling xrizer) and brings its own compositor.
- To run Monado by hand (A/B tests), stop the systemd units first. `monado-service` needs a stdin that stays open: see `tools/dev/run-monado.sh`.
