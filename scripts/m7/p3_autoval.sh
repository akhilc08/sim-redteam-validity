#!/usr/bin/env bash
# p3_autoval.sh: Phase 3 pipeline tail, safe to relaunch. Every 2 min, validate (--rerun-all, threshold 4.00)
# each run dir whose optimizer has ended (its opt.lock is free) and that has fewer validation records than
# rounds, at most MAXV at once, each under flock -n opt.lock. When every run dir is fully validated and no
# optimizer or validate is left, score everything with the held-out monitor (p3_heldout.sh) and run
# p3_analyze.py. Usage: scripts/launch.sh ar_p3_auto bash ~/work/ar/code/p3_autoval.sh
# Env (Phase 3b): AR_ROOT (default ~/work/logs/ar/phase3), HO_KINDS (held-out transcript kinds, default all three),
# POOL_WITH (another runs root: also write a pooled analysis to $AR_ROOT/analysis_pooled), VAL_ONLY_COUNT=1 (MAXV
# counts only live validates under this root, not optimizers). While $AR_ROOT/MORE exists the loop keeps waiting
# (more optimizer waves to come) instead of moving on to held-out scoring. AR_VAL_KEY_FILE (M8 scale-up, after the
# Bedrock daily token cap): validate with the Anthropic API key from this file (set AR_SONNET=anthropic/claude-sonnet-4-6).
set -uo pipefail
export SIMRT_KEY_FILE="${AR_VAL_KEY_FILE:-/nonexistent}" ANTHROPIC_API_KEY=
source "$HOME/work/simrt/scripts/box_env.sh"
box_init
VK=(); [ -n "${AR_VAL_KEY_FILE:-}" ] && VK=(with_key)
# AR_MON_KEY_FILE (M8 scale-up, Bedrock Nova Pro capped): export the keys in this file (OPENROUTER_API_KEY) for the
# target monitor, e.g. AR_MON_MODEL=openrouter/amazon/nova-pro-v1.
if [ -n "${AR_MON_KEY_FILE:-}" ]; then set -a; . "$AR_MON_KEY_FILE"; set +a; fi
SONNET_BEDROCK="${AR_SONNET:-$SONNET_BEDROCK}"  # AR_SONNET: longer rotate route list (Phase 3b: more regions, same model)
P3="${AR_ROOT:-$W/logs/ar/phase3}"; C="$HOME/work/ar/code"; MAXV="${MAXV:-4}"
export AR_REAL_CT="$CT" CT="$C/ct_mon_shim.sh" AR_EXTRA="-d provide_goals=false"
cd "$W/control-tower"
prepare_real_stage
declare -A tries
n() { cat "$1" 2>/dev/null | wc -l; }
while :; do
  pending=0; running=0
  [ "${VAL_ONLY_COUNT:-0}" = 1 ] && running=$(pgrep -fc "^$PY -m simrt.validate --out-dir $P3/runs/" || true)
  for out in "$P3"/runs/*/rep*; do
    [ -f "$out/rounds.jsonl" ] || { pending=1; continue; }
    if ! flock -n "$out/opt.lock" true; then [ "${VAL_ONLY_COUNT:-0}" = 1 ] || running=$((running + 1)); pending=1; continue; fi
    [ "$(n "$out/validation.jsonl")" -ge "$(n "$out/rounds.jsonl")" ] && continue
    [ "${tries[$out]:-0}" -ge 3 ] && { echo "$(date -u +%FT%TZ) giving up on $out after 3 validate attempts"; continue; }
    pending=1
    if [ "$running" -lt "$MAXV" ]; then
      tries[$out]=$(( ${tries[$out]:-0} + 1 ))
      echo "$(date -u +%FT%TZ) validate $out"
      ${VK[@]+"${VK[@]}"} flock -n "$out/opt.lock" "$PY" -m simrt.validate --out-dir "$out" --model "$SONNET_BEDROCK" \
        --monitor-model "${AR_MON_MODEL:-bedrock/us.amazon.nova-pro-v1:0}" --threshold 4.00 --tie-rule lenient \
        --rerun-all >> "$out/validate.log" 2>&1 < /dev/null &
      running=$((running + 1)); sleep 5
    fi
  done
  [ -e "$P3/MORE" ] && pending=1
  [ "$pending" = 0 ] && break
  sleep 120
done
wait
echo "$(date -u +%FT%TZ) all validated; held-out scoring"
AR_ROOT="$P3" KINDS="${HO_KINDS:-eval real_replay real_rerun}" bash "$C/p3_heldout.sh" || echo "heldout exited $?"
"$PY" "$C/p3_analyze.py" --runs-root "$P3/runs" || echo "analyze exited $?"
if [ -n "${POOL_WITH:-}" ]; then
  "$PY" "$C/p3_analyze.py" --runs-root "$POOL_WITH" --runs-root "$P3/runs" --out "$P3/analysis_pooled" || echo "pooled analyze exited $?"
fi
"$PY" "$C/p3_spend.py" "$P3" > "$P3/spend.json" 2>&1
echo "$(date -u +%FT%TZ) pipeline done"
