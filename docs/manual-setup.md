# Manual setup

What the Setup tab does, as commands, for doing it by hand or on another distribution. The pinned versions are in [`config/versions.json`](../config/versions.json); `$R` is this repo.

## 1. Packages (root)

Fedora:

```bash
sudo dnf install git cmake ninja-build gcc-c++ glslc cargo 'pkgconfig(glslang)' 'pkgconfig(eigen3)' 'pkgconfig(vulkan)' 'pkgconfig(hidapi-libusb)' 'pkgconfig(libusb-1.0)' 'pkgconfig(libudev)' 'pkgconfig(libsystemd)' 'pkgconfig(dbus-1)' 'pkgconfig(libbsd)' 'pkgconfig(libcjson)' 'pkgconfig(libdrm)' 'pkgconfig(libjpeg)' 'pkgconfig(opencv)' 'pkgconfig(openxr)' 'pkgconfig(sdl2)' 'pkgconfig(wayland-client)' 'pkgconfig(wayland-protocols)' 'pkgconfig(wayland-scanner)' 'pkgconfig(xrandr)' 'pkgconfig(egl)' 'pkgconfig(gl)' 'pkgconfig(dri)' 'pkgconfig(zlib)' 'pkgconfig(tbb)' 'pkgconfig(fmt)' basalt-monado xr-hardware openxr-libs libcap usbutils espeak-ng pulseaudio-utils lm_sensors
```

Check first what that would upgrade (`dnf install --assumeno …`). If it wants to upgrade Qt, KDE or Mesa packages, do a full system update and reboot first.

On other distributions you need the same libraries, the [xr-hardware](https://gitlab.freedesktop.org/monado/utilities/xr-hardware) udev rules, and [Monado's Basalt fork](https://gitlab.freedesktop.org/mateosss/basalt), built as in step 2.

## 2. Basalt (head tracking)

Monado's Basalt fork at the commit Fedora packages, with `patches/basalt` (from lbgos/rift-s-linux). Basalt's files are memory-hungry to compile: keep it at 3 parallel jobs or fewer on a laptop (7 jobs ran a 30 GB machine out of memory).

```bash
mkdir -p ~/.local/share/rift-s-linux/src && cd ~/.local/share/rift-s-linux/src
git clone https://gitlab.freedesktop.org/mateosss/basalt.git basalt
cd basalt && git checkout --detach df6e970c8da7636eb401a09e3317fbeaaf829b9a
git submodule update --init --recursive
git apply $R/patches/basalt/*.patch
cmake -S . -B ../basalt-build -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo -DCMAKE_INSTALL_PREFIX=$HOME/.local/opt/basalt-rifts -DCMAKE_INSTALL_LIBDIR=lib64 -DBASALT_BUILTIN_EIGEN=OFF -DEIGEN_ROOT=/usr/include/eigen3 -DBASALT_BUILTIN_CEREAL=ON -DSKIP_PERFORMANCE_COMPARISON=ON -DBUILD_SANDBOX=OFF -DBUILD_DOC=OFF -DBASALT_BUILD_SHARED_LIBRARY_ONLY=ON -DBASALT_BUILD_TESTS=OFF -DBUILD_TESTS=OFF -DBUILD_TESTING=OFF -DBASALT_BUILD_VISUALIZATION=OFF -DBASALT_ENABLE_ROSBAG2=OFF -DCMAKE_POLICY_VERSION_MINIMUM=3.5
ninja -C ../basalt-build -j3 install
```

The bundled cereal is needed because Fedora's `cereal-devel` lacks the CMake target this Basalt wants. Monado loads it through `VIT_SYSTEM_LIBRARY_PATH` (step 6); Fedora's `/usr/lib64/libbasalt.so.2` works as well, just with the stock behaviour.

## 3. Monado

```bash
cd ~/.local/share/rift-s-linux/src
git clone https://gitlab.freedesktop.org/thaytan/monado.git -b dev-constellation-controller-tracking monado
cd monado && git checkout --detach b1c850a015d2d3776414b31168e8f135990c0d8f
git am $R/patches/monado/*.patch
cmake -S . -B ../monado-build -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo -DCMAKE_INSTALL_PREFIX=$HOME/.local/opt/monado-6dof -DXRT_HAVE_SYSTEMD=ON -DXRT_INSTALL_ABSOLUTE_RUNTIME_PATH=ON '-DCMAKE_INSTALL_RPATH=$ORIGIN' -DBUILD_TESTING=OFF
ninja -C ../monado-build install
cp -L /usr/lib64/libcjson.so.1 ~/.local/opt/monado-6dof/lib64/   # Steam's container has no libcjson
```

`XRT_HAVE_SYSTEMD=ON` is required for socket activation. Then (root, again after every rebuild):

```bash
sudo setcap CAP_SYS_NICE=eip ~/.local/opt/monado-6dof/bin/monado-service
```

## 4. xrizer

```bash
cd ~/.local/share/rift-s-linux/src
git clone https://github.com/Supreeeme/xrizer.git && cd xrizer
git checkout --detach 0519ded7421e608805d710be01fdc51303eda16a
git apply $R/patches/xrizer/0001-*.diff && git commit -qam "PR 429"
git am $R/patches/xrizer/0002-*.patch
cargo xbuild --release --features static-openxr
mkdir -p ~/.local/opt/xrizer/bin/linux64
cp -L target/release/bin/linux64/vrclient.so ~/.local/opt/xrizer/bin/linux64/vrclient.so.new
mv ~/.local/opt/xrizer/bin/linux64/vrclient.so.new ~/.local/opt/xrizer/bin/linux64/vrclient.so
```

Copy the real file, not the build's symlink. Copying to a temporary name and then using `mv` is safe while a game has the old file loaded.

## 5. WayVR

Download `WayVR-v26.8.0-x86_64.AppImage` and `wayvrctl` from the [v26.8.0 release](https://github.com/wayvr-org/wayvr/releases/tag/v26.8.0) into `~/.local/opt/wayvr/`, then:

```bash
cd ~/.local/opt/wayvr && chmod +x WayVR-*.AppImage wayvrctl
mkdir -p v26.8.0 && (cd v26.8.0 && ../WayVR-v26.8.0-x86_64.AppImage --appimage-extract)
ln -sfn v26.8.0/squashfs-root current
```

Use the extracted AppImage, because the FUSE-mounted one crashes on every stop.

## 6. Connect everything

```bash
cd ~/.config/systemd/user
cp ~/.local/opt/monado-6dof/lib/systemd/user/monado.{service,socket} .
mkdir -p monado.service.d wayvr.service.d
cp $R/config/systemd/monado.service.d/rift-s.conf monado.service.d/
cp $R/config/systemd/wayvr.service .
cp $R/config/systemd/wayvr.service.d/50-stop.conf wayvr.service.d/
mkdir -p ~/.config/environment.d && cp $R/config/environment.d/50-monado-vr.conf ~/.config/environment.d/
mkdir -p ~/.config/openxr/1 && ln -sf ~/.local/opt/monado-6dof/share/openxr/1/openxr_monado.json ~/.config/openxr/1/active_runtime.json
systemctl --user daemon-reload
systemctl --user enable --now monado.socket
systemctl --user enable wayvr.service
```

`rift-s.conf` points `VIT_SYSTEM_LIBRARY_PATH` at the Basalt from step 2. Without that build, set it to Fedora's `/usr/lib64/libbasalt.so.2`.

In `~/.config/openvr/openvrpaths.vrpath`, put `~/.local/opt/xrizer` (as an absolute path) first in `"runtime"`. Log out and in once so Steam picks up `environment.d`.

## 7. Test tools

```bash
mkdir -p ~/.local/opt/rift-s-linux/bin
for t in headrec posrec hapticstest; do gcc -O1 -Wall $R/tools/$t.c -o ~/.local/opt/rift-s-linux/bin/$t -lopenxr_loader -lm; done
```
