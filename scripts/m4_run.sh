#!/usr/bin/env bash
# Error labelling (RQ2), defenses (RQ3), and 2 more task pairs for M3 (RQ1 power).
set -uo pipefail
source "$HOME/work/box_env.sh"
W="$HOME/work"; L="$W/logs/m3"; PY="$W/linuxarena-paper/.venv/bin/python"
SONNET="rotate/anthropic/bedrock/us.anthropic.claude-sonnet-4-6@us-east-1,anthropic/bedrock/global.anthropic.claude-sonnet-4-6,anthropic/bedrock/eu.anthropic.claude-sonnet-4-6@eu-west-1,anthropic/claude-sonnet-4-6"
MONITOR="bedrock/us.amazon.nova-lite-v1:0"
T=$(cat "$L/threshold.txt")
cd "$W/control-tower"

defenses() {
  "$PY" -m simrt.defenses dual --root "$L/runs" --sim2-settings "$W/simrt/config/sim_haiku.json" \
    --judge-model "$SONNET" --monitor-model "$MONITOR"
  "$PY" -m simrt.defenses strict --root "$L/runs" --judge-model "$SONNET"
}

echo "=== start $(date -u +%FT%TZ) threshold=$T"
# chain A: existing rounds
( "$PY" -m simrt.label_errors --root "$L/runs" --threshold "$T" --model "$SONNET" --out "$L/error_labels.jsonl"
  defenses ) > "$L/chainA.log" 2>&1 &
A=$!
# chain B: more data (optimizer now sees the corrected threshold)
( PAIRS_FILE="$W/simrt/config/tasks/m3b_pairs.txt" "$W/simrt/scripts/m3_run.sh" optimize validate ) > "$L/chainB.log" 2>&1 &
B=$!
wait $A; echo "=== chain A done $(date -u +%FT%TZ)"
wait $B; echo "=== chain B done $(date -u +%FT%TZ)"
# defenses on the new rounds too (resumable: skips rounds already done), then final reports
defenses >> "$L/chainA.log" 2>&1
"$PY" -m simrt.label_errors --root "$L/runs" --threshold "$T" --model "$SONNET" --out "$L/error_labels.jsonl" \
  > "$L/error_labels.txt" 2>&1
"$PY" -m simrt.analyze_m3 --root "$L/runs" --threshold "$T" --out "$L/analysis" > "$L/analysis.txt" 2>&1
"$PY" -m simrt.defenses report --root "$L/runs" --threshold "$T" > "$L/defenses.txt" 2>&1
echo "=== done $(date -u +%FT%TZ)"
