#!/usr/bin/env bash
# p3_heldout.sh: score every Phase 3 transcript (simulated round_NN/eval, real_replay, real_rerun) with the
# held-out monitor (Haiku 5.5, mean-quick, provide_goals=false, 3 passes) into
# ~/work/logs/ar/phase3/heldout/<pair>/<rep>/<round>/<kind>/scores.json. Resumable (simrt.monitoring reuses
# saved passes). KINDS limits the transcript kinds; run dirs whose opt.lock is held are skipped
# unless NOLOCK=1 (safe for KINDS=eval once the optimizer is done); SKIP=<pair>/<rep> skips one run dir. The API key is used only for these monitor calls (with_key). Env: PARALLEL (4), NRUNS (3).
set -euo pipefail
export SIMRT_KEY_FILE="$HOME/work/.env.monitor"
source "$HOME/work/simrt/scripts/box_env.sh"
box_init
PARALLEL="${PARALLEL:-4}"
MODEL="${HELDOUT_MODEL:-anthropic/claude-haiku-5-5}"
export AR_REAL_CT="$CT" CT="$HOME/work/ar/code/ct_mon_shim.sh" AR_EXTRA="-d provide_goals=false"
P3="${AR_ROOT:-$W/logs/ar/phase3}"; H="$P3/heldout"  # AR_ROOT: Phase 3b uses ~/work/logs/ar/phase3b
cd "$W/control-tower"
for rd in "$P3"/runs/*/rep*/round_*; do
  rel="${rd#"$P3/runs/"}"
  if [ -n "${SKIP:-}" ] && [[ "$rel" == $SKIP* ]]; then continue; fi
  [ "${NOLOCK:-0}" = 1 ] || flock -n "$(dirname "$rd")/opt.lock" true || { echo "skip $rel (run dir locked: optimizer/validate live)"; continue; }
  for kind in ${KINDS:-eval real_replay real_rerun}; do
    has_evals "$rd/$kind" || continue
    out="$H/$rel/$kind"; mkdir -p "$out"
    [ -f "$out/scores.json" ] && continue
    batch_run "$rel/$kind" "$out/monitor_cli.log" with_key "$PY" -m simrt.monitoring --evals "$rd/$kind" \
      --model "$MODEL" --n-runs "${NRUNS:-3}" --out "$out/scores.json"
  done
done
batch_done
echo "=== heldout done $(date -u +%FT%TZ)"
