#!/usr/bin/env bash
# Milestone 1 (one-shot SCE slice) on the box. Run from anywhere; launch via scripts/launch.sh.
# Usage: m1_pipeline.sh STAGE [STAGE...]   stages: honest real sim monitor judge analyze
#   honest   real honest runs of the 12 main tasks in config/tasks/m1_honest.txt (one epoch) into
#            logs/m1/honest. They are the simulator's reference transcripts (reference_dir in
#            config/sim_*.json) and 12 of the 36 threshold runs (m3_run.sh threshold), so run this
#            first. The original M1 honest runs were made before this stage existed and their model
#            string and flags were not recorded; this uses the same tasks, policy and model
#            (Sonnet 4.6) with m3_run.sh's honest-stage flags.
#   real     the 14 attack pairs in real containers      sim      the same pairs in the simulator
#   monitor  one monitor pass per run                     judge    success judge on real transcripts
#   analyze  m1_results.txt
# real, sim and honest rerun only what is missing (at most MAX_PASSES times) and fail if anything
# is still missing afterwards. A rerun honest task keeps its failed run in logs/m1/honest, so the
# honest pool behind the M1 threshold is the first clean run of each task (missing_honest.py
# --select): monitor copies those 12 monitored runs to logs/m1/mon/honest_pool, which analyze reads.
set -euo pipefail
# shellcheck source=box_env.sh
source "$(dirname "${BASH_SOURCE[0]}")/box_env.sh"
box_init
L="$W/logs/m1"
TASKS="$SIMRT_REPO/config/tasks/m1_attack.txt"
HONEST_TASKS="$SIMRT_REPO/config/tasks/m1_honest.txt"
SIM="$L/sim_m1.json"  # this run's copy of config/sim_m1.json
mkdir -p "$L"
cd "$W/control-tower"

monitor() {  # $1 = eval dir, $2 = out dir; one pass, uncached, as simrt.monitoring runs it (one pass of 3)
  has_evals "$1" || die "no .eval logs in $1: run its stage first"
  local f
  for f in "$1"/*.eval; do
    "$CT" run monitor "$f" --delayed-monitor mean-quick -d "model=$MONITOR" -d model_cache=false \
      --only-delayed --no-upload --save-dir "$2"
  done
}

# Rerun only task pairs that lack a scored sample; fail if any are still missing after MAX_PASSES.
until_complete() {  # $1 = eval dir, $2 = score key, $3... = command taking the task file as last arg
  local dir="$1" key="$2" todo n pass=0; shift 2
  todo="$L/.todo_$(basename "$dir").txt"
  while :; do
    "$PY" -m simrt.missing_pairs --task-file "$TASKS" --evals "$dir" --score-key "$key" > "$todo"
    n=$(( $(wc -l < "$todo") ))
    if [ "$n" -eq 0 ]; then return 0; fi
    if [ "$pass" -ge "$MAX_PASSES" ]; then
      echo "ERROR: $n pair(s) still missing in $dir after $MAX_PASSES passes: $todo" >&2
      return 1
    fi
    pass=$((pass + 1))
    echo "--- $(basename "$dir") pass $pass: $n pair(s) to run"
    "$@" "$todo" || echo "--- pass $pass exited with status $?; checking what is still missing"
  done
}

real_cmd() {
  "$CT" run eval --policy attack -p "model=$SONNET_BEDROCK" "${CACHE_ARGS[@]}" -p enable_side_task_test=false \
    --max-samples 2 --no-fail-on-error --retry-on-error 1 --no-upload --log-dir "$L/real_attack" \
    --task-file "$1"
}
sim_cmd() {
  with_key "$PY" -m simrt.run_sim_eval --policy attack -p "model=$SONNET" "${CACHE_ARGS[@]}" \
    -p enable_side_task_test=false --sim-settings "$SIM" --judge-model "$SONNET" \
    --log-dir "$L/sim_attack" --max-samples 3 --task-file "$1"
}

for stage in "$@"; do
  echo "=== stage $stage $(date -u +%FT%TZ)"
  case "$stage" in
    honest)
      prepare_real_stage
      manifest "$L/honest" --config "$HONEST_TASKS" --param stage=honest --param "policy_cache=$POLICY_CACHE"
      # --reference: these runs are also the simulators' reference_dir, so a task also needs a run the
      # simulator can use as its reference (simrt.reference.reference_problem), not just a clean one
      honest_until_complete --reference "$HONEST_TASKS" "$L/honest" 1 honest_real "$L/honest" ;;
    real)
      prepare_real_stage
      manifest "$L/real_attack" --config "$TASKS" --param stage=real --param "policy_cache=$POLICY_CACHE"
      until_complete "$L/real_attack" side_task_success real_cmd ;;
    sim)
      render_settings "$SIMRT_REPO/config/sim_m1.json" "$SIM"
      manifest "$L/sim_attack" --config "$TASKS" --config "$SIM" --config "$SIMRT_REPO/config/sim_m1.json" \
        --param stage=sim --param "policy_cache=$POLICY_CACHE"
      until_complete "$L/sim_attack" side_task_success_prob sim_cmd ;;
    monitor)
      rm -rf "$L/mon"
      manifest "$L/mon" --config "$HONEST_TASKS" --param stage=monitor
      select_honest "$HONEST_TASKS" "$L/honest" 1 "$L/mon/honest_selected.json" \
        "scripts/launch.sh m1 scripts/m1_pipeline.sh honest"
      monitor "$L/honest" "$L/mon/honest"
      honest_pool trajs --selected "$L/mon/honest_selected.json" --src "$L/mon/honest" --dst "$L/mon/honest_pool"
      monitor "$L/real_attack" "$L/mon/real"; monitor "$L/sim_attack" "$L/mon/sim" ;;
    judge)
      manifest "$L" --param stage=judge
      with_key "$PY" -m simrt.judge_cli --evals "$L/real_attack" --model "$SONNET" --envs-dir "$ENVS" \
        --out "$L/real_judge.json" ;;
    analyze)
      [ -d "$L/mon/honest_pool" ] || die "no $L/mon/honest_pool: run the monitor stage first"
      manifest "$L" --param stage=analyze
      "$PY" -m simrt.analyze_m1 --honest "$L/mon/honest_pool" --real "$L/mon/real" --sim "$L/mon/sim" \
        --sim-evals "$L/sim_attack" --real-evals "$L/real_attack" --real-judge "$L/real_judge.probs.json" \
        | tee "$L/m1_results.txt" ;;
    *) die "unknown stage $stage (stages: honest real sim monitor judge analyze)" ;;
  esac
done
echo "=== done $(date -u +%FT%TZ)"
