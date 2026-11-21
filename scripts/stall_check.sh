#!/usr/bin/env bash
# Watch jobs started by launch.sh, checking once a minute. Exits with
#   0  every job finished with status 0          1  every job finished, some with another status
#   2  a job was never launched, or died without writing a status
#   3  a running job wrote nothing under its watch directory for STALL_MIN minutes (e.g. throttling)
# Usage: stall_check.sh STALL_MIN JOB[=DIR] [JOB[=DIR]...]
#   DIR is where that job writes (default ~/work/logs). Give each job its own directory, e.g.
#   m3=$HOME/work/logs/m3, or one job's writes can hide another's stall. The job's .out file counts too.
# A job counts as running while its lock is held or any process of its process group (launch.sh
# records it in JOB.pid) is alive: children that closed the lock's descriptor can outlive the job.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=box_env.sh
source "$here/box_env.sh"
[ $# -ge 2 ] || { echo "usage: stall_check.sh STALL_MIN JOB[=DIR] [JOB[=DIR]...]" >&2; exit 2; }
command -v flock >/dev/null 2>&1 || die "flock not found (util-linux)"
stall_min="$1"; shift
jobs_dir="$W/logs/jobs"

group_alive() {  # $1 = job file prefix: a process of the job's process group is alive
  local _pid group
  read -r _pid group 2>/dev/null < "$1.pid" && [ -n "${group:-}" ] && kill -0 -- "-$group" 2>/dev/null
}

while :; do
  running=0
  for spec in "$@"; do
    j="${spec%%=*}"; watch="$W/logs"
    if [ "$spec" != "$j" ]; then watch="${spec#*=}"; fi
    f="$jobs_dir/$j"
    if [ -f "$f.status" ] && ! group_alive "$f"; then continue; fi  # ended, and nothing it started is left
    [ -f "$f.lock" ] || { echo "UNKNOWN: $j was never launched (no $f.lock)"; exit 2; }
    if ( flock -n 9 ) 9>>"$f.lock" && ! group_alive "$f"; then  # nothing left running, yet no status
      echo "DIED: $j ended without writing a status (killed?); see $f.out"; exit 2
    fi
    running=$((running + 1))
    recent=$(find "$watch" "$f.out" -type f -mmin -"$stall_min" 2>/dev/null | head -1) || true
    if [ -z "$recent" ]; then
      echo "STALL: $j wrote nothing under $watch for ${stall_min}m"; exit 3
    fi
  done
  if [ "$running" -eq 0 ]; then
    rc=0
    for spec in "$@"; do
      j="${spec%%=*}"; s=$(cat "$jobs_dir/$j.status")
      echo "DONE $j status=$s"
      if [ "$s" != 0 ]; then rc=1; fi
    done
    exit "$rc"
  fi
  sleep 60
done
