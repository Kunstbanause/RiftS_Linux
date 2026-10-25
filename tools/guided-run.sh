#!/bin/bash
# Guided controller tracking test: spoken cues + 45 s posrec recording.
# Usage: guided-run.sh <output.csv>
set -u
OUT="$1"
SINKS=$(pactl list short sinks | awk '{print $2}' | grep -E 'Rift_S' | sort -u)

say() {
	local wav
	wav=$(mktemp --suffix=.wav)
	espeak-ng -s 150 -w "$wav" "$1"
	for s in $SINKS; do
		paplay --device="$s" "$wav" &
	done
	wait
	rm -f "$wav"
}

# Speak a cue at a given second offset from the start of the recording
cue_at() {
	local at="$1" text="$2"
	local now
	now=$(date +%s.%N)
	local wait_s
	wait_s=$(echo "$START + $at - $now" | bc)
	if (($(echo "$wait_s > 0" | bc))); then
		sleep "$wait_s"
	fi
	say "$text" &
}

say "Get ready. Starting in three, two, one."
START=$(date +%s.%N)
"${RIFTS_BIN:-$HOME/.local/opt/rift-s-linux/bin}/posrec" 46 > "$OUT" 2>/dev/null &
REC=$!

cue_at 0 "Hold both controllers completely still."
cue_at 5 "Now slow circles with both hands."
cue_at 15 "Now fast moves. Flicks, shakes and punches."
cue_at 25 "Right hand behind your back."
cue_at 27.5 "Bring it back."
cue_at 28.5 "Left hand behind your back."
cue_at 31 "Bring it back."
cue_at 32.5 "Now hands up above your head, and pull down. Repeat."
wait $REC
say "Done. Thank you."
wait
