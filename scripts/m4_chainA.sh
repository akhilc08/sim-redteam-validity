#!/usr/bin/env bash
# Rerun chain A of m4_run.sh on its own (error labels + defenses on validated rounds). Like chain A it
# writes error_labels_chainA.jsonl, never m4_run.sh's final error_labels.jsonl, and takes TIE_RULE.
set -euo pipefail
# shellcheck source=box_env.sh
source "$(dirname "${BASH_SOURCE[0]}")/box_env.sh"
box_init
L="$W/logs/m3"
T=$(read_threshold "$L/threshold.txt")
TIE_RULE="${TIE_RULE:-lenient}"  # as in m4_run.sh: lenient (the committed results' rule) or strict
case "$TIE_RULE" in lenient|strict) ;; *) die "TIE_RULE must be lenient or strict, not '$TIE_RULE'" ;; esac
SIM2="$L/sim_haiku.json"  # this run's copy of config/sim_haiku.json (second simulator)
cd "$W/control-tower"
render_settings "$SIMRT_REPO/config/sim_haiku.json" "$SIM2"
manifest "$L" --config "$SIM2" --config "$SIMRT_REPO/config/sim_haiku.json" --param stage=m4_chainA \
  --param "threshold=$T" --param "tie_rule=$TIE_RULE"
with_key "$PY" -m simrt.label_errors --root "$L/runs" --threshold "$T" --tie-rule "$TIE_RULE" --model "$SONNET" \
  --envs-dir "$ENVS" --out "$L/error_labels_chainA.jsonl"
with_key "$PY" -m simrt.defenses dual --root "$L/runs" --sim2-settings "$SIM2" \
  --judge-model "$SONNET" --monitor-model "$MONITOR" --threshold "$T" --tie-rule "$TIE_RULE"
with_key "$PY" -m simrt.defenses strict --root "$L/runs" --judge-model "$SONNET" --threshold "$T" \
  --tie-rule "$TIE_RULE" --envs-dir "$ENVS"
