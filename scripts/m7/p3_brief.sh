#!/usr/bin/env bash
# p3_brief.sh: one-line Phase 3 progress (total rounds, validated rounds, live optimizers/validators, 429 count).
cd "$HOME/work/logs/ar/phase3/runs" || exit 0
r=$(cat */rep*/rounds.jsonl 2>/dev/null | wc -l); v=$(cat */rep*/validation.jsonl 2>/dev/null | wc -l)
o=$(pgrep -fc "simrt.optimize"); va=$(pgrep -fc "simrt.validate"); h=$(pgrep -fc "simrt.monitoring")
q=$(grep -rh "Too many tokens per day" --include=*.log . 2>/dev/null | wc -l)
echo "$(date -u +%H:%M) rounds=$r validated=$v opt=$o val=$va mon=$h 429day=$q auto=$(cat ~/work/logs/jobs/ar_p3_auto.status 2>/dev/null || echo run) $(grep -v 'giving up' ~/work/logs/jobs/ar_p3_auto.out | tail -n1 | cut -c1-80)"
