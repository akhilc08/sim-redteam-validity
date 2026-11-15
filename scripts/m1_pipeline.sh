#!/usr/bin/env bash
# Milestone 1 (one-shot SCE slice) on the box. Run from anywhere; launch via scripts/launch.sh.
# Usage: m1_pipeline.sh STAGE [STAGE...]   stages: real sim monitor judge analyze
set -euo pipefail
source "$HOME/work/box_env.sh"
W="$HOME/work"; L="$W/logs/m1"; PY="$W/linuxarena-paper/.venv/bin/python"
TASKS="$W/simrt/config/tasks/m1_attack.txt"
# Sonnet 4.6: Bedrock routes first (us., global., eu.); Anthropic API (user's key) only when all are capped.
SONNET="rotate/anthropic/bedrock/us.anthropic.claude-sonnet-4-6@us-east-1,anthropic/bedrock/global.anthropic.claude-sonnet-4-6,anthropic/bedrock/eu.anthropic.claude-sonnet-4-6@eu-west-1,anthropic/claude-sonnet-4-6"
ATTACKER="$SONNET"; JUDGE="$SONNET"
MONITOR="bedrock/us.amazon.nova-lite-v1:0"
ENVS="$W/control-tower/.environments"
MAX_PASSES=4
cd "$W/control-tower"

monitor() {  # $1 = eval dir, $2 = out dir
  for f in "$1"/*.eval; do
    "$CT" run monitor "$f" --delayed-monitor mean-quick -d "model=$MONITOR" \
      --only-delayed --no-upload --save-dir "$2"
  done
}

# Rerun only task pairs that lack a scored sample, until complete or MAX_PASSES.
until_complete() {  # $1 = eval dir, $2 = score key, $3... = command taking the task file as last arg
  local dir="$1" key="$2"; shift 2
  for pass in $(seq 1 "$MAX_PASSES"); do
    local todo="$L/.todo_$(basename "$dir").txt"
    "$PY" -m simrt.missing_pairs --task-file "$TASKS" --evals "$dir" --score-key "$key" > "$todo"
    local n; n=$(wc -l < "$todo")
    echo "--- $(basename "$dir") pass $pass: $n pair(s) to run"
    [ "$n" -eq 0 ] && return 0
    "$@" "$todo" || true
  done
}

real_cmd() {
  "$CT" run eval --policy attack -p "model=$ATTACKER" -p enable_side_task_test=false \
    --max-samples 2 --no-fail-on-error --retry-on-error 1 --no-upload --log-dir "$L/real_attack" \
    --task-file "$1"
}
sim_cmd() {
  "$PY" -m simrt.run_sim_eval --policy attack -p "model=$ATTACKER" -p enable_side_task_test=false \
    --sim-settings "$W/simrt/config/sim_m1.json" --judge-model "$JUDGE" \
    --log-dir "$L/sim_attack" --max-samples 3 --task-file "$1"
}

for stage in "$@"; do
  echo "=== stage $stage $(date -u +%FT%TZ)"
  case "$stage" in
    real) until_complete "$L/real_attack" side_task_success real_cmd ;;
    sim) until_complete "$L/sim_attack" side_task_success_prob sim_cmd ;;
    monitor)
      rm -rf "$L/mon"; monitor "$L/honest" "$L/mon/honest"
      monitor "$L/real_attack" "$L/mon/real"; monitor "$L/sim_attack" "$L/mon/sim" ;;
    judge)
      "$PY" -m simrt.judge_cli --evals "$L/real_attack" --model "$JUDGE" --envs-dir "$ENVS" \
        --out "$L/real_judge.json" ;;
    analyze)
      "$PY" -m simrt.analyze_m1 --honest "$L/mon/honest" --real "$L/mon/real" --sim "$L/mon/sim" \
        --sim-evals "$L/sim_attack" --real-evals "$L/real_attack" --real-judge "$L/real_judge.probs.json" | tee "$L/m1_results.txt" ;;
    *) echo "unknown stage $stage"; exit 2 ;;
  esac
done
echo "=== done $(date -u +%FT%TZ)"
