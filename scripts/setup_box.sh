#!/usr/bin/env bash
# Set up LinuxArena (linuxarena-paper + control-tower) on the EC2 dev box.
# Idempotent. Run as `ubuntu` on the box.
set -euo pipefail

CT_REV=699d5ca5c26ab3126fa29c6fa73dbf898aa0ca53
ROOT=~/work
export PATH="$HOME/.local/bin:$PATH"
mkdir -p "$ROOT" && cd "$ROOT"

[ -d linuxarena-paper ] || git clone -q --depth 1 https://github.com/redwoodresearch/linuxarena-paper.git
if [ ! -d control-tower ]; then
  git clone -q https://github.com/linuxarena/control-tower.git
  git -C control-tower checkout -q "$CT_REV"
fi

# control-tower's private `src/research` submodule breaks `uv sync`; install it separately without deps.
cd linuxarena-paper
rm -rf .venv/lib/python3.15 2>/dev/null; [ -x .venv/bin/python3.13 ] || rm -rf .venv
uv sync --frozen --python 3.13 --no-install-package control-tower
uv pip install --no-deps -e ../control-tower
CT="$PWD/.venv/bin/ct"

# Public envs pinned to the paper's v1.0.1 tag. Plain git (`ct registry pull` needs gh auth, and
# pulls HEAD, which needs a newer SDK than control-tower 1.0.1).
cd ../control-tower && mkdir -p .environments
for e in auto_workflow clinical_trial file_indexing media_processing model_registry \
         shared_dotfiles slack_clone tiktok user_tracking web_scraping; do
  [ -d ".environments/$e" ] || git clone -q "https://github.com/linuxarena/$e.git" ".environments/$e"
  git -C ".environments/$e" checkout -q v1.0.1
done

# Bedrock smoke test via Inspect's bedrock provider (credentials come from the instance role).
cd ../linuxarena-paper
AWS_DEFAULT_REGION=us-east-1 .venv/bin/python - <<'PY'
import asyncio
from inspect_ai.model import get_model
async def main():
    m = get_model("bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0")
    out = await m.generate("Reply with the single word: pong")
    print("bedrock:", out.completion.strip())
asyncio.run(main())
PY
echo "setup done"
