#!/usr/bin/env bash
# Rerun chain A of m4_run.sh on its own (error labels + defenses on validated rounds).
set -uo pipefail
source "$HOME/work/box_env.sh"
W="$HOME/work"; L="$W/logs/m3"; PY="$W/linuxarena-paper/.venv/bin/python"
SONNET="rotate/anthropic/bedrock/us.anthropic.claude-sonnet-4-6@us-east-1,anthropic/bedrock/global.anthropic.claude-sonnet-4-6,anthropic/bedrock/eu.anthropic.claude-sonnet-4-6@eu-west-1,anthropic/claude-sonnet-4-6"
MONITOR="bedrock/us.amazon.nova-lite-v1:0"
T=$(cat "$L/threshold.txt")
cd "$W/control-tower"
"$PY" -m simrt.label_errors --root "$L/runs" --threshold "$T" --model "$SONNET" --out "$L/error_labels.jsonl"
"$PY" -m simrt.defenses dual --root "$L/runs" --sim2-settings "$W/simrt/config/sim_haiku.json" \
  --judge-model "$SONNET" --monitor-model "$MONITOR"
"$PY" -m simrt.defenses strict --root "$L/runs" --judge-model "$SONNET"
