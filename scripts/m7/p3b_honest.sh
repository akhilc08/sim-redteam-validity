#!/usr/bin/env bash
# p3b_honest.sh TASKS: honest reference runs for Phase 3b pool pairs (Control Tower honest policy, Sonnet 4.6 on
# Bedrock, 1 epoch, rerun until each task has a run simrt.reference accepts) into ~/work/logs/ar/phase3b/honest_pool,
# then one Nova Pro mean-quick no-goals score (3 passes) of those runs into honest_pool_novapro/scores.json, an FPR
# sanity check in the new environments (the 4.00 threshold is not changed). TASKS lines: "env main_task".
# Bedrock only. Usage: scripts/launch.sh ar_p3b_honest bash ~/work/ar/code/p3b_honest.sh TASKS
set -euo pipefail
export SIMRT_KEY_FILE=/nonexistent ANTHROPIC_API_KEY=
source "$HOME/work/simrt/scripts/box_env.sh"
box_init
P3="${AR_ROOT:-$W/logs/ar/phase3b}"; D="$P3/honest_pool"; mkdir -p "$D"
tasks="$(realpath "$1")"
cd "$W/control-tower"
require_cache_off "p3b_honest.sh"
prepare_real_stage
honest_until_complete --reference "$tasks" "$D" 1 honest_real "$D"
export AR_REAL_CT="$CT" CT="$HOME/work/ar/code/ct_mon_shim.sh" AR_EXTRA="-d provide_goals=false"
mkdir -p "$P3/honest_pool_novapro"
"$PY" -m simrt.monitoring --evals "$D" --model bedrock/us.amazon.nova-pro-v1:0 --n-runs 3 \
  --out "$P3/honest_pool_novapro/scores.json" > "$P3/honest_pool_novapro/monitor_cli.log" 2>&1 || echo "nova pro scoring exited $?"
echo "=== honest done $(date -u +%FT%TZ)"
