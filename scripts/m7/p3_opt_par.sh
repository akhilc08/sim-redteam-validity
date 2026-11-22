#!/usr/bin/env bash
# p3_opt_par.sh: copy of p3_run.sh used for the parallel optimize launch (REPS="1 2" chooses reps; each
# simrt.optimize call holds flock -n on <run>/opt.lock so two jobs never write one run dir).
# p3_run.sh STAGE...  Phase 3 main experiment (modelled on scripts/m3_run.sh), outputs in ~/work/logs/ar/phase3.
#   optimize  PAIRS (file, "env main side" per line) x REPEATS, up to ROUNDS rounds, patience 3, 3 in parallel,
#             target monitor $AR_MON_MODEL with $AR_EXTRA (provide_goals=false) via ct_mon_shim.sh. Bedrock only.
#   validate  simrt.validate on every run dir (replay all rounds; live rerun per $RERUN_ALL=1 -> --rerun-all).
# Env: THRESHOLD (required), PAIRS_FILE, REPEATS (3), ROUNDS (8), PARALLEL (3), RERUN_ALL (0|1), AR_ROOT,
# TRACE_DIR (simulator traces; default from config/sim_m1.json). A pairs line may carry a 4th field: a rendered
# sim settings file to use for that pair instead of $P3/sim_m1.json (e.g. one whose reference_dir has new honest runs).
set -euo pipefail
export SIMRT_KEY_FILE=/nonexistent ANTHROPIC_API_KEY=
source "$HOME/work/simrt/scripts/box_env.sh"
box_init
SONNET_BEDROCK="${AR_SONNET:-$SONNET_BEDROCK}"  # AR_SONNET: longer rotate route list (Phase 3b: more regions, same model)
P3="${AR_ROOT:-$W/logs/ar/phase3}"; mkdir -p "$P3"  # AR_ROOT: runs root parent (Phase 3b: ~/work/logs/ar/phase3b)
PAIRS_FILE="${PAIRS_FILE:-$P3/pairs.txt}"
REPEATS="${REPEATS:-3}"; ROUNDS="${ROUNDS:-8}"; PARALLEL="${PARALLEL:-3}"
: "${THRESHOLD:?set THRESHOLD}"
MON="${AR_MON_MODEL:-bedrock/us.amazon.nova-pro-v1:0}"
export AR_REAL_CT="$CT" CT="$HOME/work/ar/code/ct_mon_shim.sh" AR_EXTRA="${AR_EXTRA:--d provide_goals=false}"
SIM="$P3/sim_m1.json"
PAIRS=()
while read -r line || [ -n "$line" ]; do case "$line" in "" | "#"*) ;; *) PAIRS+=("$line") ;; esac; done < "$PAIRS_FILE"
cd "$W/control-tower"
run_dirs() {
  local env main side k
  for p in "${PAIRS[@]}"; do read -r env main side _ <<< "$p"
    for k in $(seq 0 $((REPEATS - 1))); do echo "$P3/runs/${main}__${side}/rep$k"; done; done
}
for stage in "$@"; do
  echo "=== stage $stage $(date -u +%FT%TZ)"
  case "$stage" in
    optimize)
      require_cache_off "p3_run.sh optimize"
      [ "${SKIP_RENDER:-0}" = 1 ] || render_settings "$SIMRT_REPO/config/sim_m1.json" "$SIM" ${TRACE_DIR:+--trace-dir "$TRACE_DIR"}  # SKIP_RENDER=1 keeps an edited $SIM
      for k in ${REPS:-$(seq 0 $((REPEATS - 1)))}; do  # rep-major: each wave covers every pair
        for p in "${PAIRS[@]}"; do read -r env main side psim <<< "$p"
          out="$P3/runs/${main}__${side}/rep$k"; mkdir -p "$out"
          echo "{\"threshold\":$THRESHOLD,\"monitor\":\"$MON\",\"extra\":\"$AR_EXTRA\",\"rounds\":$ROUNDS}" > "$out/ar_config.json"
          batch_run "${main}__${side}/rep$k" "$out/optimize.log" flock -n "$out/opt.lock" "$PY" -m simrt.optimize \
            --env "$env" --main "$main" --side "$side" --rounds "$ROUNDS" --patience 3 --threshold "$THRESHOLD" \
            --out-dir "$out" --sim-settings "${psim:-$SIM}" --model "$SONNET_BEDROCK" --monitor-model "$MON" \
            --envs-dir "$ENVS" --python "$PY" --tie-rule lenient
        done
      done
      batch_done ;;
    validate)
      require_cache_off "p3_run.sh validate"
      prepare_real_stage
      extra=(); [ "${RERUN_ALL:-0}" = 1 ] && extra=(--rerun-all)
      for out in $(run_dirs); do
        [ -f "$out/rounds.jsonl" ] || { echo "skip $out (no rounds.jsonl)"; continue; }
        batch_run "$out" "$out/validate.log" "$PY" -m simrt.validate --out-dir "$out" \
          --model "$SONNET_BEDROCK" --monitor-model "$MON" --threshold "$THRESHOLD" --tie-rule lenient ${extra[@]+"${extra[@]}"}
      done
      batch_done ;;
    *) die "unknown stage $stage" ;;
  esac
done
echo "=== done $(date -u +%FT%TZ)"
