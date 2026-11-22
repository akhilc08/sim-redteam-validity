#!/usr/bin/env bash
# ct_mon_shim.sh: CT=this file for simrt.optimize / simrt.validate. Only `ct run monitor` calls get
# $AR_EXTRA appended (e.g. "-d provide_goals=false") and mean-quick swapped for $AR_MONITOR (if set);
# every other ct call (run eval for replays and live reruns) goes to the real ct unchanged.
set -euo pipefail
real="${AR_REAL_CT:-$HOME/work/linuxarena-paper/.venv/bin/ct}"
if [ "${1:-}" = run ] && [ "${2:-}" = monitor ]; then
  args=()
  for a in "$@"; do
    if [ "$a" = mean-quick ] && [ -n "${AR_MONITOR:-}" ]; then a="$AR_MONITOR"; fi
    args+=("$a")
  done
  # shellcheck disable=SC2086
  exec "$real" "${args[@]}" ${AR_EXTRA:-}
fi
exec "$real" "$@"
