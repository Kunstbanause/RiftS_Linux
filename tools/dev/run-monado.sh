#!/bin/bash
# Run the 6DoF Monado build by hand for A/B tests (stop the systemd units first).
# Usage: run-monado.sh <fusion 0|1> <record dir or ""> <log file>
exec env VIT_SYSTEM_LIBRARY_PATH=/usr/lib64/libbasalt.so.2 RIFT_S_HANDTRACKING=0 \
	RIFT_S_CTRL_FUSION="$1" RIFT_S_CTRL_RECORD="$2" \
	"$HOME/.local/opt/monado-6dof/bin/monado-service" < <(sleep infinity) > "$3" 2>&1
