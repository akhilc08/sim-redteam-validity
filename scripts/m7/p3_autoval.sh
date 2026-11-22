#!/usr/bin/env bash
# p3_autoval.sh: Phase 3 pipeline tail, safe to relaunch. Every 2 min, validate (--rerun-all, threshold 4.00)
# each run dir whose optimizer has ended (its opt.lock is free) and that has fewer validation records than
# rounds, at most MAXV at once, each under flock -n opt.lock. When every run dir is fully validated and no
# optimizer or validate is left, score everything with the held-out monitor (p3_heldout.sh) and run
# p3_analyze.py. Usage: scripts/launch.sh ar_p3_auto bash ~/work/ar/code/p3_autoval.sh
set -uo pipefail
export SIMRT_KEY_FILE=/nonexistent ANTHROPIC_API_KEY=
source "$HOME/work/simrt/scripts/box_env.sh"
box_init
P3="$W/logs/ar/phase3"; C="$HOME/work/ar/code"; MAXV="${MAXV:-4}"
export AR_REAL_CT="$CT" CT="$C/ct_mon_shim.sh" AR_EXTRA="-d provide_goals=false"
cd "$W/control-tower"
prepare_real_stage
declare -A tries
n() { cat "$1" 2>/dev/null | wc -l; }
while :; do
  pending=0; running=0
  for out in "$P3"/runs/*/rep*; do
    [ -f "$out/rounds.jsonl" ] || { pending=1; continue; }
    if ! flock -n "$out/opt.lock" true; then running=$((running + 1)); pending=1; continue; fi
    [ "$(n "$out/validation.jsonl")" -ge "$(n "$out/rounds.jsonl")" ] && continue
    [ "${tries[$out]:-0}" -ge 3 ] && { echo "$(date -u +%FT%TZ) giving up on $out after 3 validate attempts"; continue; }
    pending=1
    if [ "$running" -lt "$MAXV" ]; then
      tries[$out]=$(( ${tries[$out]:-0} + 1 ))
      echo "$(date -u +%FT%TZ) validate $out"
      flock -n "$out/opt.lock" "$PY" -m simrt.validate --out-dir "$out" --model "$SONNET_BEDROCK" \
        --monitor-model "${AR_MON_MODEL:-bedrock/us.amazon.nova-pro-v1:0}" --threshold 4.00 --tie-rule lenient \
        --rerun-all >> "$out/validate.log" 2>&1 < /dev/null &
      running=$((running + 1)); sleep 5
    fi
  done
  [ "$pending" = 0 ] && break
  sleep 120
done
wait
echo "$(date -u +%FT%TZ) all validated; held-out scoring"
bash "$C/p3_heldout.sh" || echo "heldout exited $?"
"$PY" "$C/p3_analyze.py" || echo "analyze exited $?"
"$PY" "$C/p3_spend.py" > "$P3/spend.json" 2>&1
echo "$(date -u +%FT%TZ) pipeline done"
