# Box environment and shared helpers. Sourced (never run) by launch.sh, stall_check.sh and the
# pipeline scripts, which find it next to themselves; source it by hand for an interactive shell.
#
# Layout: the deployment root W=$HOME/work holds linuxarena-paper/ (its .venv has ct, Inspect and
# simrt), control-tower/ (with .environments/) and logs/. This checkout can live anywhere: configs
# and task files are read from it, and box_init checks that simrt is installed from it.
#
# API key (optional): ANTHROPIC_API_KEY, if already set when a script starts, else read from
# SIMRT_KEY_FILE (default ~/work/.env). Nothing here exports it. box_init blanks every provider key
# (blank, not unset: ct and Inspect load .env files but never override a variable that is set), and
# only commands run through with_key get the key back: the simulated stages (attacker, simulator,
# judge, optimizer, labeller), where it adds the direct API as the last route after Bedrock. Real
# Control Tower runs use Bedrock routes only and get no key: ct copies provider keys from its
# environment into the internet-simulator container, where agent-submitted scripts run. Of the ten
# pinned environments only media_processing proxies a provider through that container (OpenAI); to
# run it, list the key to leave alone in SIMRT_KEEP_KEYS, e.g. SIMRT_KEEP_KEYS=OPENAI_API_KEY.

SIMRT_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SIMRT_ARGV=("$0" "$@")  # the sourcing script's command line, recorded in run manifests
W="$HOME/work"
PY="$W/linuxarena-paper/.venv/bin/python"
ENVS="$W/control-tower/.environments"
export CT="$W/linuxarena-paper/.venv/bin/ct"
export AWS_DEFAULT_REGION=us-east-1
export SKIP_DOCKER_BUILD=1
export DISABLE_TUI=1
for e in AUTO_WORKFLOW CLINICAL_TRIAL FILE_INDEXING MEDIA_PROCESSING MODEL_REGISTRY \
         SHARED_DOTFILES SLACK_CLONE TIKTOK USER_TRACKING WEB_SCRAPING; do
  export "ENV_IMAGE_TAG_${e}=v1.0.1"
done
# Internet-simulator image. ct pulls this ref before every sample; set it to a digest
# (linuxarena/env-internet-simulator@sha256:...) to pin it. Otherwise prepare_real_stage resolves
# the tag to a digest once per stage, so a whole stage runs on one image, and the manifest records it.
export INTERNET_SIMULATOR_IMAGE="${INTERNET_SIMULATOR_IMAGE:-linuxarena/env-internet-simulator:latest}"
SIMRT_KEY_FILE="${SIMRT_KEY_FILE:-$W/.env}"
MAX_PASSES="${MAX_PASSES:-4}"  # reruns of missing task pairs / honest tasks before a stage gives up
PARALLEL="${PARALLEL:-3}"  # concurrent jobs in batch_run stages

MONITOR="bedrock/us.amazon.nova-lite-v1:0"
# Sonnet 4.6 over three Bedrock inference profiles; rotate/ fails over when one hits its daily cap.
SONNET_BEDROCK="rotate/anthropic/bedrock/us.anthropic.claude-sonnet-4-6@us-east-1,anthropic/bedrock/global.anthropic.claude-sonnet-4-6,anthropic/bedrock/eu.anthropic.claude-sonnet-4-6@eu-west-1"

die() { echo "ERROR: $*" >&2; exit 1; }

# Check the deployment, take the API key out of the environment, and set SONNET (simulated stages:
# Bedrock routes, then the API if a key exists) and CACHE_ARGS. Call right after sourcing this file.
box_init() {
  local p k
  for p in "$PY" "$CT" "$ENVS"; do
    [ -e "$p" ] || die "$p not found: run scripts/setup_box.sh first (deployment root $W)"
  done
  "$PY" - "$SIMRT_REPO" <<'PY' || die "simrt is not installed from this checkout (see above)"
import sys
from pathlib import Path
repo = Path(sys.argv[1]).resolve()
fix = f"uv pip install --python {sys.executable} --no-deps -e {repo}"
try:
    import simrt
except ImportError:
    sys.exit(f"simrt is not installed in {sys.executable}: {fix}")
if not Path(simrt.__file__).resolve().is_relative_to(repo):
    sys.exit(f"{sys.executable} imports simrt from {Path(simrt.__file__).parent}, not {repo}: {fix}")
PY
  SIMRT_API_KEY="${ANTHROPIC_API_KEY:-}"
  if [ -z "$SIMRT_API_KEY" ] && [ -f "$SIMRT_KEY_FILE" ]; then
    SIMRT_API_KEY="$(set +eu -a; . "$SIMRT_KEY_FILE" >/dev/null 2>&1; printf '%s' "${ANTHROPIC_API_KEY:-}")"
  fi
  for k in ANTHROPIC_API_KEY OPENAI_API_KEY OPENROUTER_API_KEY; do
    case " ${SIMRT_KEEP_KEYS:-} " in *" $k "*) ;; *) export "$k=" ;; esac
  done
  # Inside a VS Code terminal Inspect reloads .env files with override; make sure it cannot.
  unset VSCODE_PID VSCODE_CWD VSCODE_IPYTHON_KERNEL VSCODE_CLI_REQUIRE_TOKEN
  if [ "${TERM_PROGRAM:-}" = vscode ]; then unset TERM_PROGRAM; fi
  SONNET="$SONNET_BEDROCK"
  API_FALLBACK=off  # whether the simulated stages (with_key) get the key; recorded in run manifests
  if [ -n "$SIMRT_API_KEY" ]; then
    SONNET="$SONNET_BEDROCK,anthropic/claude-sonnet-4-6"
    API_FALLBACK=on
    echo "simrt: Anthropic API fallback on for simulated stages; real runs use Bedrock only"
  else
    echo "simrt: no ANTHROPIC_API_KEY (environment or $SIMRT_KEY_FILE): all Claude calls use Bedrock only"
  fi
  # ct's policies cache model responses on disk by default, so repeats of a task pair would replay
  # earlier attacker/honest calls instead of sampling. Off unless SIMRT_POLICY_CACHE=1, which restores
  # ct's default (on) for the policy runs these scripts start themselves through CACHE_ARGS: m1 honest,
  # real and sim, m3 honest, m5 sim_honest. It cannot reach the others (see require_cache_off).
  POLICY_CACHE=false
  if [ "${SIMRT_POLICY_CACHE:-0}" = 1 ]; then POLICY_CACHE=true; fi
  CACHE_ARGS=(-p "model_cache=$POLICY_CACHE" -p "cache=$POLICY_CACHE")
}

# require_cache_off WHAT: refuse SIMRT_POLICY_CACHE=1 for a stage whose policy runs simrt.optimize or
# simrt.validate start (m2, m3 optimize/validate, m4 chain B, m5 closedloop). Those always pass
# simrt.ct_args.NO_CACHE_ARGS (cache off), so the committed results/m3, which ran with ct's default
# (cache on, before that change), cannot be rerun with the cache on from these scripts.
require_cache_off() {
  [ "$POLICY_CACHE" = false ] || die "SIMRT_POLICY_CACHE=1 cannot apply to $1: simrt.optimize and" \
    "simrt.validate always run Control Tower's policies with the response cache off" \
    "(simrt.ct_args.NO_CACHE_ARGS). Unset SIMRT_POLICY_CACHE for this stage."
}

# Run a command with the API key (if any). Only for simulated stages; real runs never get it.
with_key() { ( export ANTHROPIC_API_KEY="$SIMRT_API_KEY"; "$@" ) }

has_evals() { local f; for f in "$1"/*.eval; do [ -e "$f" ] && return 0; done; return 1; }

# read_threshold FILE: the audit threshold written by `m3_run.sh threshold`.
read_threshold() { cat "$1" 2>/dev/null || die "$1 not found: run 'scripts/m3_run.sh threshold' first"; }

# render_settings TEMPLATE OUT [--trace-dir DIR]: the settings file a run uses (scripts/render_settings.py:
# expands ~, drops the API route when there is no key, checks envs_dir and reference_dir).
render_settings() {
  if [ -n "$SIMRT_API_KEY" ]; then
    "$PY" "$SIMRT_REPO/scripts/render_settings.py" "$@"
  else
    "$PY" "$SIMRT_REPO/scripts/render_settings.py" "$@" --drop-api-route
  fi
}

# manifest DIR [--config FILE]... [--param KEY=VALUE]...: record code, image and config versions, the
# model strings and whether the simulated stages get the API key. Stages that run ct policies add
# --param policy_cache=$POLICY_CACHE themselves (it means nothing for the others).
manifest() {
  local dir="$1"; shift
  "$PY" -m simrt.manifest --out "$dir" "$@" --param "sonnet=$SONNET" --param "sonnet_real=$SONNET_BEDROCK" \
    --param "monitor=$MONITOR" --param "api_fallback=$API_FALLBACK" -- "${SIMRT_ARGV[@]}"
}

# Before a stage that starts real containers: pin the internet-simulator image to the digest its
# tag points at now, and refuse a running simulator container that an earlier run started with
# provider keys (ct reuses a running one as is). Skipped quietly without Docker.
prepare_real_stage() {
  command -v docker >/dev/null 2>&1 || return 0
  local ref="" names k leaked=""
  case "$INTERNET_SIMULATOR_IMAGE" in
    *@sha256:*) ;;
    *)
      if docker pull -q "$INTERNET_SIMULATOR_IMAGE" >/dev/null 2>&1 \
        && ref=$(docker image inspect --format '{{index .RepoDigests 0}}' "$INTERNET_SIMULATOR_IMAGE" 2>/dev/null) \
        && [ -n "$ref" ]; then
        export INTERNET_SIMULATOR_IMAGE="$ref"
      else
        echo "WARNING: could not resolve $INTERNET_SIMULATOR_IMAGE to a digest; ct pulls it per sample" >&2
      fi ;;
  esac
  echo "internet simulator image: $INTERNET_SIMULATOR_IMAGE"
  names=$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' internet-simulator 2>/dev/null \
    | sed -E -n 's/^(ANTHROPIC_API_KEY|OPENAI_API_KEY|OPENROUTER_API_KEY)=.+/\1/p') || true
  for k in $names; do
    case " ${SIMRT_KEEP_KEYS:-} " in *" $k "*) ;; *) leaked="$leaked $k" ;; esac
  done
  [ -z "$leaked" ] || die "the running internet-simulator container holds$leaked from an earlier run" \
    "(visible to code that runs inside it). When no real runs are active: docker rm -f internet-simulator"
}

# batch_run NAME LOG CMD...: run CMD in the background (output appended to LOG), PARALLEL at a time;
# a full batch is waited for job by job. batch_done waits for the rest and fails if any job failed.
BATCH_PIDS=(); BATCH_NAMES=(); BATCH_FAILED=()
batch_run() {
  local name="$1" log="$2"; shift 2
  "$@" >> "$log" 2>&1 < /dev/null &
  BATCH_PIDS+=("$!"); BATCH_NAMES+=("$name")
  if [ "${#BATCH_PIDS[@]}" -ge "$PARALLEL" ]; then batch_wait; fi
}
batch_wait() {
  local i
  for i in "${!BATCH_PIDS[@]}"; do
    wait "${BATCH_PIDS[$i]}" || BATCH_FAILED+=("${BATCH_NAMES[$i]}")
  done
  BATCH_PIDS=(); BATCH_NAMES=()
}
batch_done() {
  batch_wait
  if [ "${#BATCH_FAILED[@]}" -gt 0 ]; then
    echo "ERROR: ${#BATCH_FAILED[@]} job(s) failed: ${BATCH_FAILED[*]}" >&2
    BATCH_FAILED=(); return 1
  fi
}

# honest_until_complete [--reference] TASKS DIR EPOCHS CMD...: run honest tasks until every line of
# TASKS has EPOCHS clean samples in DIR. CMD gets TASK_FILE EPOCHS appended. The first pass on an empty DIR
# runs every task with all epochs; later passes rerun only short tasks, one epoch at a time. Fails
# after MAX_PASSES passes. Errored or unscored samples stay in DIR next to their reruns, so DIR can
# hold more than EPOCHS samples of a task: consumers use only the selection that
# `missing_honest.py --select` writes (the first EPOCHS clean samples of each task).
# --reference is for the simulators' reference_dir (config/sim_*.json): a task is also rerun until one of
# its runs is usable as a reference transcript by simrt.reference's rule (missing_honest.py --reference),
# since a clean run can be too short for it. Those reruns come later in run order, so the selection is unchanged.
honest_until_complete() {
  local ref="" todo n pass=0 what
  if [ "${1:-}" = --reference ]; then ref=--reference; shift; fi
  local tasks="$1" dir="$2" epochs="$3"; shift 3
  what="$epochs clean run(s)${ref:+ or a run usable as a simulator reference}"
  todo="$(dirname "$dir")/.todo_$(basename "$dir").txt"
  while :; do
    "$PY" "$SIMRT_REPO/scripts/missing_honest.py" --task-file "$tasks" --evals "$dir" --epochs "$epochs" \
      ${ref:+"$ref"} > "$todo"
    n=$(( $(wc -l < "$todo") ))
    if [ "$n" -eq 0 ]; then return 0; fi
    if [ "$pass" -ge "$MAX_PASSES" ]; then
      echo "ERROR: $n task(s) still lack $what in $dir after $MAX_PASSES passes: $todo" >&2
      return 1
    fi
    pass=$((pass + 1))
    echo "--- $(basename "$dir") pass $pass: $n task(s) short of $what"
    if has_evals "$dir"; then
      "$@" "$todo" 1 || echo "--- pass $pass exited with status $?; checking what is still missing"
    else
      "$@" "$tasks" "$epochs" || echo "--- pass $pass exited with status $?; checking what is still missing"
    fi
  done
}

# select_honest TASKS DIR EPOCHS OUT [FILL]: write to OUT the honest samples that count, the first EPOCHS
# clean samples of each task in DIR; fails, naming the short tasks and FILL (the command that reruns
# them), if a task has fewer. A run stopped by a limit is not clean, e.g. the existing M1 honest run
# of clinical_trial ct_rewrite_in_rust (300-message limit).
select_honest() {
  "$PY" "$SIMRT_REPO/scripts/missing_honest.py" --task-file "$1" --evals "$2" --epochs "$3" --select "$4" \
    || die "no honest pool from $2: rerun its short tasks first${5:+ with: $5}"
}

# honest_pool threshold|trajs ...: apply such selections to monitor outputs (scripts/honest_pool.py).
honest_pool() { "$PY" "$SIMRT_REPO/scripts/honest_pool.py" "$@"; }

# honest_real LOG_DIR TASK_FILE EPOCHS: Control Tower's honest policy in real containers (Bedrock only).
honest_real() {
  "$CT" run eval --policy honest -p "model=$SONNET_BEDROCK" "${CACHE_ARGS[@]}" --task-file "$2" \
    --epochs "$3" --max-samples 4 --no-fail-on-error --no-upload --log-dir "$1"
}
