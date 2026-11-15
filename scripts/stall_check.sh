#!/usr/bin/env bash
# Print one line per check; exits when the named jobs finish, or prints STALL if no file under
# ~/work/logs changed for STALL_MIN minutes while a job is running (e.g. Bedrock throttling).
# Usage: stall_check.sh STALL_MIN JOB [JOB...]
set -uo pipefail
stall_min="$1"; shift
jobs_dir="$HOME/work/logs/jobs"
while true; do
  running=()
  for j in "$@"; do [ -f "$jobs_dir/$j.status" ] || running+=("$j"); done
  if [ ${#running[@]} -eq 0 ]; then
    for j in "$@"; do echo "DONE $j status=$(cat "$jobs_dir/$j.status")"; done; exit 0
  fi
  recent=$(find "$HOME/work/logs" -type f -mmin -"$stall_min" | head -1)
  if [ -z "$recent" ]; then echo "STALL: nothing written for ${stall_min}m; running: ${running[*]}"; exit 3; fi
  sleep 60
done
