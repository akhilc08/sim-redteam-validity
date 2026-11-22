#!/usr/bin/env bash
# run_cfg.sh TAG MODEL NRUNS KEYMODE SET... : score ~/work/logs/m6/rescore/evals/<SET> with one monitor config
# into ~/work/logs/ar/phase1/<TAG>/<SET>/scores.json. KEYMODE: bedrock (no key anywhere) | api (monitor key
# via with_key). Env: AR_MONITOR (ct delayed monitor, default mean-quick), AR_EXTRA (extra ct args),
# MON_PARALLEL (sets at once).
set -euo pipefail
tag=$1; MODEL=$2; nruns=$3; keymode=$4; shift 4
if [ "$keymode" = api ]; then export SIMRT_KEY_FILE="$HOME/work/.env.monitor"; else export SIMRT_KEY_FILE=/nonexistent ANTHROPIC_API_KEY=; fi
source "$HOME/work/simrt/scripts/box_env.sh"
box_init
PARALLEL="${MON_PARALLEL:-3}"
export AR_REAL_CT="$CT" CT="$HOME/work/ar/code/ct_shim.sh" AR_MONITOR="${AR_MONITOR:-}" AR_EXTRA="${AR_EXTRA:-}"
cd "$W/control-tower"
R="$W/logs/m6/rescore"; O="$W/logs/ar/phase1/$tag"; mkdir -p "$O"
echo "{\"tag\":\"$tag\",\"model\":\"$MODEL\",\"monitor\":\"${AR_MONITOR:-mean-quick}\",\"extra\":\"$AR_EXTRA\",\"n_runs\":$nruns}" > "$O/config.json"
for set in "$@"; do
  out="$O/$set"; mkdir -p "$out"
  cmd=("$PY" -m simrt.monitoring --evals "$R/evals/$set" --model "$MODEL" --n-runs "$nruns" --out "$out/scores.json")
  echo "$(date -u +%FT%TZ) start $set"
  if [ "$keymode" = api ]; then batch_run "$set" "$out/monitor_cli.log" with_key "${cmd[@]}"
  else batch_run "$set" "$out/monitor_cli.log" env SIMRT_KEY_FILE=/nonexistent ANTHROPIC_API_KEY= "${cmd[@]}"; fi
done
batch_done
echo "=== done $tag $(date -u +%FT%TZ)"
