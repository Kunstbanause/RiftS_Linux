#!/bin/bash
# Count Monado "missed frame" warnings with hello_xr for several compositor time budgets.
# Usage: measure-pacing.sh <min_comp_time_ms> ...  (systemd monado units must be stopped)
for ms in "$@"; do
	env U_PACING_COMP_MIN_TIME_MS=$ms XRT_COMPOSITOR_SCALE_PERCENTAGE=100 RIFT_S_DISTORTION_C1=0.47 \
		VIT_SYSTEM_LIBRARY_PATH=/usr/lib64/libbasalt.so.2 RIFT_S_HANDTRACKING=0 \
		"$HOME/.local/opt/monado-6dof/bin/monado-service" < <(sleep 40) > /tmp/pacing_$ms.log 2>&1 &
	sleep 8
	(sleep 20 | timeout 20 hello_xr -g Vulkan2 > /dev/null 2>&1 &)
	sleep 8
	a=$(grep -c 'missed frame' /tmp/pacing_$ms.log)
	sleep 10
	b=$(grep -c 'missed frame' /tmp/pacing_$ms.log)
	echo "min comp time ${ms} ms: $(( (b - a) )) missed-frame warnings in 10 s"
	pkill -x hello_xr; pkill -x monado-service
	while pgrep -x monado-service > /dev/null; do sleep 0.5; done
	sleep 1
done
