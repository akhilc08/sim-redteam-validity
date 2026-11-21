#!/usr/bin/env bash
# Follow-up experiments from the 2026-11-20 audit, run on the M3 logs (logs/m3/runs). Outputs go to
# logs/m5; nothing under logs/m3 is modified. Usage: m5_run.sh STAGE [STAGE...]
# Only the transcripts behind results/m3 are used: each round rounds.jsonl recorded without an error
# (its simulated transcript) and each replay validation.jsonl recorded with a known outcome; set-aside
# attempts (round_XX.stale-N, real_replay.failed-N) are skipped. Costs are given in model calls (no
# prices), "normally" without and "at most" with the retries the code makes; Inspect's retries of
# throttled calls come on top. Counts refer to results/m3/rounds.csv: 64 validated rounds, 63 with a
# replay, 24 simulated losses. ct caps each agent phase (exploration, execution) at 150 steps.
#   noise          monitor test-retest: NOISE_PASSES (default 10) fresh monitor passes on every
#                  simulated and real-replay transcript (about 64 + 63), into logs/m5/noise (the
#                  original 3-pass scores stay as they are). A pass is 2 Nova Lite calls (mean-quick is
#                  a 2-member ensemble), run again up to 2 times while invalid (simrt.monitoring
#                  --retries 2): per transcript normally 2 x NOISE_PASSES calls, at most 6 x NOISE_PASSES.
#                  No Claude calls.
#   closedloop     closed-loop reruns of the simulated LOSSES (validate.py --rerun-all), to get
#                  P(real win | sim loss) next to the 36/40 for sim wins. validate.py skips rounds
#                  already in validation.jsonl, so the rounds each run's validation.jsonl recorded as
#                  sim losses are copied into logs/m5/closedloop/<pair>/<rep> and validated there
#                  (appending to the original files would also double-count them). Per round (about 24):
#                  one live strategy-attack trajectory in real containers (one Sonnet call per agent
#                  step plus one planning call), run once more if it errors or is not scored
#                  (validate.py --real-retries 1), so at most 2 trajectories; a fresh open-loop replay
#                  (no model calls, container time only; also at most 2 runs); 3 monitor passes on the
#                  replay and on the rerun: normally 12 Nova Lite calls, at most 36.
#   judge          the success judge on every real-replay transcript, to compare the judge with the
#                  real scorer on the same real transcripts (about 63): per transcript normally 1 Sonnet
#                  call plus one per round of codebase lookups (up to 8), plus up to 2 re-asks when the
#                  answer has no probability: at most 11. Resumable: a transcript is judged again until
#                  it gets one.
#   sim_honest     the honest policy in the simulator on config/tasks/m1_honest.txt,
#                  SIM_HONEST_EPOCHS (default 3) epochs = 36 trajectories, plus reruns of runs that
#                  errored (at most MAX_PASSES passes). Per trajectory: one Sonnet call per agent step
#                  plus one planning call, and per tool call one simulator call (none when the file
#                  layer answers it), at most 21: up to 6 lookups and 3 attempts (7 calls each).
#                  The judge makes no call for honest runs.
#   sim_threshold  3 monitor passes on each simulated honest run that counts (the first
#                  SIM_HONEST_EPOCHS clean runs of each task): normally 6 Nova Lite calls each, at
#                  most 18; the same 95th-percentile rule as m3's threshold -> logs/m5/sim_threshold.txt.
set -euo pipefail
# shellcheck source=box_env.sh
source "$(dirname "${BASH_SOURCE[0]}")/box_env.sh"
box_init
for stage in "$@"; do
  case "$stage" in closedloop) require_cache_off "m5_run.sh closedloop" ;; esac
done
M3="$W/logs/m3"
L="$W/logs/m5"
HONEST_TASKS="$SIMRT_REPO/config/tasks/m1_honest.txt"
NOISE_PASSES="${NOISE_PASSES:-10}"
SIM_HONEST_EPOCHS="${SIM_HONEST_EPOCHS:-3}"
SIM="$L/sim_m1.json"  # this run's copy of config/sim_m1.json
mkdir -p "$L"
cd "$W/control-tower"

# dataset_rounds: "<round dir> eval" for each round rounds.jsonl recorded without an error and
# "<round dir> real_replay" for each replay validation.jsonl recorded with a known outcome.
dataset_rounds() {
  "$PY" - "$M3/runs" <<'PY'
import json, sys
from pathlib import Path

def records(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []

found = []
for run in sorted(p for p in Path(sys.argv[1]).glob("*/rep*") if p.is_dir()):
    found += [(run / f"round_{r['round']:02d}", "eval") for r in records(run / "rounds.jsonl")
              if r.get("eval_path") and not r.get("error")]
    found += [(run / f"round_{v['round']:02d}", "real_replay") for v in records(run / "validation.jsonl")
              if (v.get("replay") or {}).get("side_task_success") is not None]
if not found:
    sys.exit(f"ERROR: no recorded rounds under {sys.argv[1]}: run m3_run.sh optimize validate first")
print("\n".join(f"{d} {kind}" for d, kind in found))
PY
}

# judged FILE: judge_cli's --out FILE exists and every sample in it got a probability (judge_cli
# writes FILE, with "prob": null, also when the judge failed).
judged() {
  [ -f "$1" ] && "$PY" -c 'import json, sys
sys.exit(any(v.get("prob") is None for v in json.load(open(sys.argv[1])).values()))' "$1" 2>/dev/null
}

# copy_sim_losses RUN OUT: OUT/rounds.jsonl gets the rounds RUN's validation.jsonl recorded as simulated
# losses (sim_win false, with a judge probability and a monitor score; exactly the rounds m3 never
# reran closed-loop), plus each round's strat.txt and task.txt for the rerun.
copy_sim_losses() {
  "$PY" - "$@" <<'PY'
import json, shutil, sys
from pathlib import Path
run, out = Path(sys.argv[1]), Path(sys.argv[2])
rounds = {json.loads(x)["round"]: x for x in (run / "rounds.jsonl").read_text().splitlines()}
vpath = run / "validation.jsonl"
losses = sorted({v["round"] for v in map(json.loads, vpath.read_text().splitlines() if vpath.exists() else [])
                 if not v["sim_win"] and v.get("sim_judge_prob") is not None and v.get("sim_sus") is not None})
for i in losses:
    dst = out / f"round_{i:02d}"
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("strat.txt", "task.txt"):
        shutil.copy2(run / f"round_{i:02d}" / name, dst / name)
out.mkdir(parents=True, exist_ok=True)
(out / "rounds.jsonl").write_text("".join(rounds[i] + "\n" for i in losses))
print(f"{run}: {len(losses)} simulated loss(es)")
PY
}

sim_honest_cmd() {  # $1 = task file, $2 = epochs
  with_key "$PY" -m simrt.run_sim_eval --policy honest -p "model=$SONNET" "${CACHE_ARGS[@]}" \
    --task-file "$1" --epochs "$2" --sim-settings "$SIM" --judge-model "$SONNET" \
    --max-samples 3 --log-dir "$L/sim_honest"
}

for stage in "$@"; do
  echo "=== stage $stage $(date -u +%FT%TZ)"
  case "$stage" in
    noise)
      manifest "$L/noise" --param stage=noise --param "passes=$NOISE_PASSES"
      rounds=$(dataset_rounds)
      while read -r rd kind; do
        [ -n "$rd" ] || continue
        has_evals "$rd/$kind" || die "no .eval log in $rd/$kind, which the M3 records list"
        out="$L/noise/${rd#"$M3/runs/"}/$kind"
        mkdir -p "$out"
        batch_run "$out" "$out/monitor.log" "$PY" -m simrt.monitoring --evals "$rd/$kind" \
          --model "$MONITOR" --n-runs "$NOISE_PASSES" --out "$out/scores.json"
      done <<< "$rounds"
      batch_done ;;
    closedloop)
      T=$(read_threshold "$M3/threshold.txt")
      prepare_real_stage
      for run in "$M3"/runs/*/rep*; do
        [ -f "$run/rounds.jsonl" ] || continue
        out="$L/closedloop/${run#"$M3/runs/"}"
        copy_sim_losses "$run" "$out"
        manifest "$out" --param stage=closedloop --param "threshold=$T" --param "source=$run" \
          --param "policy_cache=$POLICY_CACHE"
        batch_run "$out" "$out/validate.log" "$PY" -m simrt.validate --out-dir "$out" \
          --model "$SONNET_BEDROCK" --monitor-model "$MONITOR" --threshold "$T" --rerun-all
      done
      batch_done ;;
    judge)
      manifest "$L/judge_replay" --param stage=judge
      rounds=$(dataset_rounds)
      while read -r rd kind; do
        [ "$kind" = real_replay ] || continue
        out="$L/judge_replay/${rd#"$M3/runs/"}.json"
        if judged "$out"; then continue; fi  # resumable
        mkdir -p "$(dirname "$out")"
        batch_run "$out" "${out%.json}.log" with_key "$PY" -m simrt.judge_cli --evals "$rd/real_replay" \
          --model "$SONNET" --envs-dir "$ENVS" --out "$out"
      done <<< "$rounds"
      batch_done ;;
    sim_honest)
      render_settings "$SIMRT_REPO/config/sim_m1.json" "$SIM" --trace-dir "$L/sim_honest_traces"
      manifest "$L/sim_honest" --config "$HONEST_TASKS" --config "$SIM" \
        --config "$SIMRT_REPO/config/sim_m1.json" --param stage=sim_honest --param "policy_cache=$POLICY_CACHE"
      honest_until_complete "$HONEST_TASKS" "$L/sim_honest" "$SIM_HONEST_EPOCHS" sim_honest_cmd ;;
    sim_threshold)
      manifest "$L" --config "$HONEST_TASKS" --param stage=sim_threshold
      select_honest "$HONEST_TASKS" "$L/sim_honest" "$SIM_HONEST_EPOCHS" "$L/sim_honest_selected.json"
      "$PY" -m simrt.monitoring --evals "$L/sim_honest" --model "$MONITOR" --out "$L/sim_honest_scores.json"
      # An output no later stage reads, so a new value replaces the old one (kept in .prev.txt).
      honest_pool threshold --force --out "$L/sim_threshold.txt" \
        --pool "$L/sim_honest_scores.json" "$L/sim_honest_selected.json"
      echo "simulated honest threshold: $(cat "$L/sim_threshold.txt") (real: logs/m3/threshold.txt)" ;;
    *) die "unknown stage $stage (stages: noise closedloop judge sim_honest sim_threshold)" ;;
  esac
done
echo "=== done $(date -u +%FT%TZ)"
