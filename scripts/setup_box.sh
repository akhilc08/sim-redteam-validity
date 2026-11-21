#!/usr/bin/env bash
# Set up LinuxArena (linuxarena-paper + control-tower + 10 public envs) and simrt on the EC2 box.
# Idempotent. Run as the box user (ubuntu), from this checkout (wherever it is cloned); needs git,
# uv and Docker. Installs everything under ~/work, the deployment root the scripts expect.
set -euo pipefail

# Pinned revisions. PAPER_REV is the linuxarena-paper commit of the checkout this repo was developed
# and tested against (its uv.lock pins inspect-ai 0.3.171). The commit used on the box for the
# committed results/m3 was not recorded (setup used to clone the default branch's HEAD); run
# manifests record it from now on.
PAPER_REV=9d1fbc6cf3ff5a133738cd8d035dfa825550ef67
CT_REV=699d5ca5c26ab3126fa29c6fa73dbf898aa0ca53
ENV_TAG=v1.0.1
SIMRT_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$HOME/work"
export PATH="$HOME/.local/bin:$PATH"
command -v uv >/dev/null 2>&1 || {
  echo "uv not found: install it (https://docs.astral.sh/uv/; the standalone installer puts it in ~/.local/bin)" >&2
  exit 1
}
mkdir -p "$ROOT" && cd "$ROOT"

# clone_at DIR URL REV: clone and check out REV if DIR is missing; warn if an existing DIR differs.
clone_at() {
  if [ ! -d "$1" ]; then
    git clone -q --filter=blob:none "$2" "$1"
    git -C "$1" checkout -q "$3"
  elif [ "$(git -C "$1" rev-parse HEAD)" != "$(git -C "$1" rev-parse -q --verify "$3^{commit}" || true)" ]; then
    echo "WARNING: $ROOT/$1 is at $(git -C "$1" rev-parse --short HEAD), not the pinned $3; left as is" >&2
  fi
}
clone_at linuxarena-paper https://github.com/redwoodresearch/linuxarena-paper.git "$PAPER_REV"
clone_at control-tower https://github.com/linuxarena/control-tower.git "$CT_REV"

# control-tower's private `src/research` submodule breaks `uv sync`; install it separately without deps.
cd "$ROOT/linuxarena-paper"
rm -rf .venv/lib/python3.15 2>/dev/null; [ -x .venv/bin/python3.13 ] || rm -rf .venv
uv sync --frozen --python 3.13 --no-install-package control-tower
uv pip install --no-deps -e ../control-tower
uv pip install --python .venv/bin/python --no-deps -e "$SIMRT_REPO"  # simrt: this checkout

# Public envs pinned to the paper's v1.0.1 tag. Plain git (`ct registry pull` needs gh auth, and
# pulls HEAD, which needs a newer SDK than control-tower 1.0.1).
cd "$ROOT/control-tower" && mkdir -p .environments
for e in auto_workflow clinical_trial file_indexing media_processing model_registry \
         shared_dotfiles slack_clone tiktok user_tracking web_scraping; do
  [ -d ".environments/$e" ] || git clone -q "https://github.com/linuxarena/$e.git" ".environments/$e"
  git -C ".environments/$e" checkout -q "$ENV_TAG"
done

# Internet-simulator image: ct pulls it by tag before every sample. Print the digest so it can be
# pinned with INTERNET_SIMULATOR_IMAGE=linuxarena/env-internet-simulator@sha256:... (see box_env.sh).
img="${INTERNET_SIMULATOR_IMAGE:-linuxarena/env-internet-simulator:latest}"
if command -v docker >/dev/null 2>&1 && docker pull -q "$img" >/dev/null; then
  echo "internet simulator: $(docker image inspect --format '{{index .RepoDigests 0}}' "$img")"
fi

# Bedrock smoke test via Inspect's bedrock provider (credentials come from the instance role).
cd "$ROOT/linuxarena-paper"
AWS_DEFAULT_REGION=us-east-1 .venv/bin/python - <<'PY'
import asyncio
from inspect_ai.model import get_model
async def main():
    m = get_model("bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0")
    out = await m.generate("Reply with the single word: pong")
    print("bedrock:", out.completion.strip())
asyncio.run(main())
PY
echo "setup done: linuxarena-paper $(git -C "$ROOT/linuxarena-paper" rev-parse --short HEAD)," \
  "control-tower $(git -C "$ROOT/control-tower" rev-parse --short HEAD), envs $ENV_TAG"
