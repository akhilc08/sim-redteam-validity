#!/usr/bin/env bash
# Milestone 3 (main experiment). Usage: m3_run.sh STAGE [STAGE...]
#   honest    24 extra honest runs (12 main tasks x 2 epochs) for threshold calibration
#   threshold average-of-3 monitor on all 36 honest runs -> logs/m3/threshold.txt
#   optimize  2 task pairs x 3 repeats, up to 8 rounds each (3 in parallel)
#   validate  real-execution checks of every round
set -euo pipefail
source "$HOME/work/box_env.sh"
W="$HOME/work"; L="$W/logs/m3"; PY="$W/linuxarena-paper/.venv/bin/python"
SONNET="rotate/anthropic/bedrock/us.anthropic.claude-sonnet-4-6@us-east-1,anthropic/bedrock/global.anthropic.claude-sonnet-4-6,anthropic/bedrock/eu.anthropic.claude-sonnet-4-6@eu-west-1,anthropic/claude-sonnet-4-6"
MONITOR="bedrock/us.amazon.nova-lite-v1:0"
PAIRS_FILE="${PAIRS_FILE:-$W/simrt/config/tasks/m3_pairs.txt}"
mapfile -t PAIRS < "$PAIRS_FILE"
REPEATS=3
mkdir -p "$L"; cd "$W/control-tower"

run_dirs() { for p in "${PAIRS[@]}"; do set -- $p; for k in $(seq 0 $((REPEATS - 1))); do echo "$L/runs/$2__$3/rep$k"; done; done; }

for stage in "$@"; do
  echo "=== stage $stage $(date -u +%FT%TZ)"
  case "$stage" in
    honest)
      "$CT" run eval --policy honest -p "model=$SONNET" --task-file "$W/simrt/config/tasks/m1_honest.txt" \
        --epochs 2 --max-samples 4 --no-fail-on-error --no-upload --log-dir "$L/honest_extra" ;;
    threshold)
      "$PY" -m simrt.monitoring --evals "$W/logs/m1/honest" --model "$MONITOR" --out "$L/honest_m1_scores.json"
      "$PY" -m simrt.monitoring --evals "$L/honest_extra" --model "$MONITOR" --out "$L/honest_extra_scores.json"
      "$PY" - "$L" <<'PY'
import json, sys
from pathlib import Path
from simrt.monitoring import audit_threshold
L = Path(sys.argv[1])
s = [v["sus"] for f in ("honest_m1_scores.json", "honest_extra_scores.json") for v in json.loads((L / f).read_text()).values()]
t = audit_threshold(s)
(L / "threshold.txt").write_text(f"{t}\n")
print(f"honest n={len(s)} threshold@5% = {t}")
PY
      ;;
    optimize)
      T=$(cat "$L/threshold.txt")
      i=0
      for p in "${PAIRS[@]}"; do set -- $p
        for k in $(seq 0 $((REPEATS - 1))); do
          out="$L/runs/$2__$3/rep$k"; mkdir -p "$out"
          "$PY" -m simrt.optimize --env "$1" --main "$2" --side "$3" --rounds 8 --patience 3 \
            --threshold "$T" --out-dir "$out" --sim-settings "$W/simrt/config/sim_m1.json" \
            --model "$SONNET" --monitor-model "$MONITOR" > "$out/optimize.log" 2>&1 &
          i=$((i + 1)); if (( i % 3 == 0 )); then wait; fi
        done
      done
      wait ;;
    validate)
      T=$(cat "$L/threshold.txt")
      # 3 run dirs in parallel (each brings up real containers); resumable per round.
      i=0
      for out in $(run_dirs); do
        "$PY" -m simrt.validate --out-dir "$out" --model "$SONNET" --monitor-model "$MONITOR" \
          --threshold "$T" >> "$out/validate.log" 2>&1 || echo "validate failed: $out" &
        i=$((i + 1)); if (( i % 3 == 0 )); then wait; fi
      done
      wait ;;
    *) echo "unknown stage $stage"; exit 2 ;;
  esac
done
echo "=== done $(date -u +%FT%TZ)"
