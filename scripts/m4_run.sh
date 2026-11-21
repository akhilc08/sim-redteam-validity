#!/usr/bin/env bash
# Error labelling (RQ2), defenses (RQ3), and 2 more task pairs for M3 (RQ1 power).
# Chain A (labels + defenses on the existing rounds) and chain B (m3_run.sh optimize validate on
# config/tasks/m3b_pairs.txt) run in parallel; the final reports run only if both chains succeed.
# Each labelling run has its own file (label_errors rewrites its --out): chain A's labels go to
# error_labels_chainA.jsonl; the final labels of every round, which supersede them, to error_labels.jsonl.
# TIE_RULE=lenient|strict (default lenient, the committed results' rule) goes to label_errors, defenses and
# analyze_m3, and to chain B's optimize and validate (through m3_run.sh).
set -euo pipefail
# shellcheck source=box_env.sh
source "$(dirname "${BASH_SOURCE[0]}")/box_env.sh"
box_init
require_cache_off "m4_run.sh (chain B runs m3_run.sh optimize validate)"
L="$W/logs/m3"
T=$(read_threshold "$L/threshold.txt")
TIE_RULE="${TIE_RULE:-lenient}"
case "$TIE_RULE" in lenient|strict) ;; *) die "TIE_RULE must be lenient or strict, not '$TIE_RULE'" ;; esac
SIM2="$L/sim_haiku.json"  # this run's copy of config/sim_haiku.json (second simulator)
cd "$W/control-tower"

defenses() {  # --threshold, --tie-rule: spend only on rounds `defenses report` (same rule) can count as sim wins
  with_key "$PY" -m simrt.defenses dual --root "$L/runs" --sim2-settings "$SIM2" \
    --judge-model "$SONNET" --monitor-model "$MONITOR" --threshold "$T" --tie-rule "$TIE_RULE"
  with_key "$PY" -m simrt.defenses strict --root "$L/runs" --judge-model "$SONNET" --threshold "$T" \
    --tie-rule "$TIE_RULE" --envs-dir "$ENVS"
}
labels() {  # labels OUT: label every validated round into OUT
  with_key "$PY" -m simrt.label_errors --root "$L/runs" --threshold "$T" --tie-rule "$TIE_RULE" \
    --model "$SONNET" --envs-dir "$ENVS" --out "$1"
}

echo "=== start $(date -u +%FT%TZ) threshold=$T"
render_settings "$SIMRT_REPO/config/sim_haiku.json" "$SIM2"
manifest "$L" --config "$SIM2" --config "$SIMRT_REPO/config/sim_haiku.json" --param stage=m4 --param "threshold=$T" \
  --param "tie_rule=$TIE_RULE"
# chain A: existing rounds
( labels "$L/error_labels_chainA.jsonl"; defenses ) > "$L/chainA.log" 2>&1 &
A=$!
# chain B: more data (optimizer now sees the corrected threshold). box_init above blanked the key in
# this shell, so m3_run.sh gets it through with_key; its own box_init takes it from there and blanks
# it again for its real runs (a key given only in the environment would otherwise not reach chain B).
( export PAIRS_FILE="$SIMRT_REPO/config/tasks/m3b_pairs.txt" TIE_RULE
  with_key "$SIMRT_REPO/scripts/m3_run.sh" optimize validate ) > "$L/chainB.log" 2>&1 &
B=$!
failed=""
wait "$A" || failed="$failed A ($L/chainA.log)"
echo "=== chain A done $(date -u +%FT%TZ)"
wait "$B" || failed="$failed B ($L/chainB.log)"
echo "=== chain B done $(date -u +%FT%TZ)"
[ -z "$failed" ] || die "chain(s)$failed failed; final reports not written"
# defenses on the new rounds too (resumable: skips rounds already done), then final reports
defenses >> "$L/chainA.log" 2>&1
labels "$L/error_labels.jsonl" > "$L/error_labels.txt" 2>&1
"$PY" -m simrt.analyze_m3 --root "$L/runs" --threshold "$T" --tie-rule "$TIE_RULE" --out "$L/analysis" \
  > "$L/analysis.txt" 2>&1
"$PY" -m simrt.defenses report --root "$L/runs" --threshold "$T" --tie-rule "$TIE_RULE" > "$L/defenses.txt" 2>&1
echo "=== done $(date -u +%FT%TZ)"
