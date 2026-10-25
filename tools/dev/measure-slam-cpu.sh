#!/bin/bash
# Measure monado-service CPU (cores) for different RIFT_S_SLAM_CAMS values. Headset must be connected.
for n in "$@"; do
	env RIFT_S_SLAM_CAMS=$n VIT_SYSTEM_LIBRARY_PATH=/usr/lib64/libbasalt.so.2 RIFT_S_HANDTRACKING=0 \
		"$HOME/.local/opt/monado-6dof/bin/monado-service" < <(sleep 40) > /dev/null 2>&1 &
	sleep 15
	P=$(pgrep -n -x monado-service)
	t1=$(awk '{print $14+$15}' /proc/$P/stat)
	sleep 10
	t2=$(awk '{print $14+$15}' /proc/$P/stat)
	echo "RIFT_S_SLAM_CAMS=$n: monado-service $(echo "scale=2; ($t2-$t1)/1000" | bc) cores"
	kill $P
	while pgrep -x monado-service > /dev/null; do sleep 0.5; done
done
