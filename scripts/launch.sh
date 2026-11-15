#!/usr/bin/env bash
# Launch a long-running job on the box, detached from SSH, at most one instance per job name.
# Usage: launch.sh JOB_NAME command args...
# Output goes to ~/work/logs/jobs/JOB_NAME.out; exit status to JOB_NAME.status when it finishes.
set -euo pipefail
name="$1"; shift
dir="$HOME/work/logs/jobs"; mkdir -p "$dir"
lock="$dir/$name.lock"
if ! ( set -o noclobber; echo $$ > "$lock" ) 2>/dev/null; then
  pid=$(cat "$lock" 2>/dev/null || true)
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    echo "job $name already running (pid $pid)"; exit 1
  fi
  echo $$ > "$lock"  # stale lock
fi
rm -f "$dir/$name.status"
# shellcheck disable=SC1091
source "$HOME/work/box_env.sh"
setsid bash -c '
  "$@" > "'"$dir/$name.out"'" 2>&1 < /dev/null
  echo $? > "'"$dir/$name.status"'"
  rm -f "'"$lock"'"
' _ "$@" < /dev/null > /dev/null 2>&1 &
echo $! > "$lock"
echo "started $name (pid $!)"
