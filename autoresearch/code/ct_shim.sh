#!/usr/bin/env bash
# ct_shim.sh: stands in for ct when simrt.monitoring calls it (CT=this file). Swaps the delayed monitor
# for $AR_MONITOR (if set) and appends $AR_EXTRA (e.g. "-d provide_goals=false") so simrt's src/ stays as is.
set -euo pipefail
real="${AR_REAL_CT:-$HOME/work/linuxarena-paper/.venv/bin/ct}"
args=()
for a in "$@"; do
  if [ "$a" = mean-quick ] && [ -n "${AR_MONITOR:-}" ]; then a="$AR_MONITOR"; fi
  args+=("$a")
done
# shellcheck disable=SC2086
exec "$real" "${args[@]}" ${AR_EXTRA:-}
