#!/usr/bin/env bash
# m8_scale_finish.sh: final step of the M8 scale-up on the box. Waits until the v2 and v2-r1 validation tails
# (p3_autoval.sh jobs m8s_auto_v2 / m8s_auto_v2r1: validation, held-out Haiku 5.5, per-root p3_analyze, spend) have
# ended, then writes the pre-registered analysis (m8_scale_analyze.py -> $S/analysis) and the token-based spend over
# the three roots ($S/spend.json), and touches $S/DONE. Usage: scripts/launch.sh m8s_finish bash scripts/m8/m8_scale_finish.sh
set -uo pipefail
export SIMRT_KEY_FILE=/nonexistent ANTHROPIC_API_KEY=
S="$HOME/work/logs/m8/scale"; J="$HOME/work/logs/jobs"; PY="$HOME/work/linuxarena-paper/.venv/bin/python"
cd "$HOME/work/simrt"
until [ -f "$J/m8s_auto_v2.status" ] && [ -f "$J/m8s_auto_v2r1.status" ]; do sleep 60; done
echo "$(date -u +%FT%TZ) tails done: v2 $(cat "$J/m8s_auto_v2.status"), v2r1 $(cat "$J/m8s_auto_v2r1.status")"
rm -f "$S/GUARD_WAIT"
"$PY" scripts/m8/m8_scale_analyze.py --out "$S/analysis" || echo "analyze exited $?"
"$PY" scripts/m8/m8_spend.py "$S/resample" "$S/v2" "$S/v2r1" > "$S/spend.json" 2>/dev/null || echo "spend exited $?"
touch "$S/DONE"
echo "$(date -u +%FT%TZ) finished"
