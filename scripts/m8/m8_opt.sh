#!/usr/bin/env bash
# m8_opt.sh: M8 pilot optimizer runs (modelled on scripts/m7/p3_opt_par.sh's optimize stage, Phase 3 settings) with
# the stronger-optimizer options. One run per rep in $REPS, all at once, each under flock -n <run>/opt.lock.
# Env: AR_ROOT (runs root parent, required), AR_SONNET (attacker/judge route, required: the 7-route Sonnet 4.6 list),
# THRESHOLD (required), REPS ("0 1"), ROUNDS (6), PAIR ("env main side", default the visit pair), OPT_MODEL (Opus 4.6
# on Bedrock; OPT_MODEL=none: no --optimizer-model, the attacker route writes strategies), M8_ARGS (default "--rollouts 3
# --incumbent --feedback transcript"; M8_ARGS= (empty) runs the default optimizer, e.g. ROUNDS=1 re-sampled round-0
# draws as in M7 Phase 3c), SIM_MSG_LIMIT (scale-up: --sim-message-limit for every simulated rollout; unset = 300),
# SIM (sim settings file, default $AR_ROOT/sim_m1.json; not re-rendered: it carries the 7-route simulator list).
# Bedrock only.
set -euo pipefail
export SIMRT_KEY_FILE=/nonexistent ANTHROPIC_API_KEY=
source "$HOME/work/simrt/scripts/box_env.sh"
box_init
R="${AR_ROOT:?set AR_ROOT}"; S="${AR_SONNET:?set AR_SONNET}"; : "${THRESHOLD:?set THRESHOLD}"
ROUNDS="${ROUNDS:-6}"; REPS="${REPS:-0 1}"
PAIR="${PAIR:-web_scraping allowlist_config visit_malicious_website}"
OPT_MODEL="${OPT_MODEL:-rotate/anthropic/bedrock/us.anthropic.claude-opus-4-6-v1@us-east-1,anthropic/bedrock/global.anthropic.claude-opus-4-6-v1}"
M8_ARGS="${M8_ARGS---rollouts 3 --incumbent --feedback transcript}"
MON="${AR_MON_MODEL:-bedrock/us.amazon.nova-pro-v1:0}"
export AR_REAL_CT="$CT" CT="$HOME/work/ar/code/ct_mon_shim.sh" AR_EXTRA="-d provide_goals=false"
SIM="${SIM:-$R/sim_m1.json}"; [ -f "$SIM" ] || die "$SIM missing"
EXTRA=()
[ "$OPT_MODEL" = none ] || EXTRA+=(--optimizer-model "$OPT_MODEL")
[ -z "${SIM_MSG_LIMIT:-}" ] || EXTRA+=(--sim-message-limit "$SIM_MSG_LIMIT")
read -r env main side <<< "$PAIR"
PARALLEL=$(wc -w <<< "$REPS")
cd "$W/control-tower"
require_cache_off "m8_opt.sh"
echo "=== m8 optimize $(date -u +%FT%TZ) pair=$PAIR reps=$REPS rounds=$ROUNDS args=$M8_ARGS opt_model=$OPT_MODEL sim=$SIM msg_limit=${SIM_MSG_LIMIT:-default}"
for k in $REPS; do
  out="$R/runs/${main}__${side}/rep$k"; mkdir -p "$out"
  echo "{\"threshold\":$THRESHOLD,\"monitor\":\"$MON\",\"extra\":\"$AR_EXTRA\",\"rounds\":$ROUNDS,\"m8_args\":\"$M8_ARGS\",\"optimizer_model\":\"$OPT_MODEL\",\"sim_settings\":\"$SIM\",\"sim_message_limit\":\"${SIM_MSG_LIMIT:-}\"}" > "$out/ar_config.json"
  # shellcheck disable=SC2086
  batch_run "${main}__${side}/rep$k" "$out/optimize.log" flock -n "$out/opt.lock" "$PY" -m simrt.optimize \
    --env "$env" --main "$main" --side "$side" --rounds "$ROUNDS" --patience 3 --threshold "$THRESHOLD" \
    --out-dir "$out" --sim-settings "$SIM" --model "$S" --monitor-model "$MON" \
    --envs-dir "$ENVS" --python "$PY" --tie-rule lenient ${EXTRA[@]+"${EXTRA[@]}"} $M8_ARGS
done
batch_done
echo "=== done $(date -u +%FT%TZ)"
