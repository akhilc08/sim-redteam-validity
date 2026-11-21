#!/usr/bin/env bash
# Launch a long-running job on the box, detached from SSH, at most one instance per job name.
# Usage: launch.sh JOB_NAME command args...    (JOB_NAME: letters, digits, '.', '_' or '-')
# Files in ~/work/logs/jobs:
#   JOB_NAME.out     the job's output
#   JOB_NAME.status  its exit status, written when it ends, also when stopped with TERM or HUP
#   JOB_NAME.pid     "<job pid> <process group>". Stop the job and everything it started with
#                    kill -- -<process group> (TERM). INT does not work: a background job of a script
#                    starts with INT ignored, and a shell cannot trap a signal ignored on entry.
#   JOB_NAME.lock    flock-held by the job's processes that inherit it, so a relaunch is refused while
#                    any of them runs. Children that close inherited descriptors (Python's subprocess
#                    does by default: ct and run_sim_eval under simrt.optimize and simrt.validate) do not
#                    hold it, but they stay in the job's process group, so a relaunch is also refused
#                    while any process of the last run's group is alive. A leftover lock file is free.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ $# -ge 2 ] || { echo "usage: launch.sh JOB_NAME command args..." >&2; exit 2; }
name="$1"; shift
case "$name" in
  "" | .* | *[!A-Za-z0-9._-]*)
    echo "invalid job name '$name': use letters, digits, '.', '_' or '-' (not starting with '.')" >&2
    exit 2 ;;
esac
# shellcheck source=box_env.sh
source "$here/box_env.sh"
for tool in flock setsid; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool not found (util-linux): launch.sh is meant for the Linux box"
done
[ -d "$W" ] || die "deployment root $W not found: run scripts/setup_box.sh first"
dir="$W/logs/jobs"
mkdir -p "$dir"
exec 9>>"$dir/$name.lock"
if ! flock -n 9; then
  echo "job $name already running (pid, process group: $(cat "$dir/$name.pid" 2>/dev/null || echo unknown))" >&2
  exit 1
fi
# A lock file holding a pid was written by the old launcher, whose jobs do not hold the flock.
old=$(tr -cd '0-9' < "$dir/$name.lock")
if [ -n "$old" ] && kill -0 "$old" 2>/dev/null; then die "job $name already running (pid $old, older launcher)"; fi
if read -r _ group 2>/dev/null < "$dir/$name.pid" && [ -n "${group:-}" ] && kill -0 -- "-$group" 2>/dev/null; then
  die "processes of job $name's last run are still alive in process group $group: wait for them or stop" \
    "them (kill -- -$group). If that group is not the job's (its number was reused), delete $dir/$name.pid"
fi
: > "$dir/$name.lock"
rm -f "$dir/$name.status" "$dir/$name.pid"

# The wrapper's program text is fixed; every value reaches it as a positional argument.
# shellcheck disable=SC2016
setsid bash -c '
  out="$1" status="$2" pidfile="$3"; shift 3
  "$@" > "$out" 2>&1 < /dev/null &
  job=$!
  stop() { kill -TERM "$job" 2>/dev/null; }
  trap stop TERM HUP
  echo "$job $(ps -o pgid= -p $$ | tr -d " ")" > "$pidfile"
  while :; do
    rc=0; wait "$job" || rc=$?
    kill -0 "$job" 2>/dev/null || break  # still alive: a trapped signal interrupted wait
  done
  echo "$rc" > "$status.tmp" && mv -f "$status.tmp" "$status"
' launch-wrapper "$dir/$name.out" "$dir/$name.status" "$dir/$name.pid" "$@" < /dev/null > /dev/null 2>&1 &

for _ in $(seq 1 50); do [ -s "$dir/$name.pid" ] && break; sleep 0.1; done
if read -r pid group 2>/dev/null < "$dir/$name.pid"; then
  echo "started $name (pid $pid, process group $group; stop with: kill -- -$group)"
else
  echo "started $name (pid not recorded yet: see $dir/$name.pid)"
fi
