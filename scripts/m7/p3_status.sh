#!/usr/bin/env bash
# p3_status.sh: one line per Phase 3 run dir (rounds done, validated rounds, live optimizer?) + job statuses.
cd "${AR_ROOT:-$HOME/work/logs/ar/phase3}/runs" || exit 0
for d in */rep*; do
  r=$(cat "$d/rounds.jsonl" 2>/dev/null | wc -l); v=$(cat "$d/validation.jsonl" 2>/dev/null | wc -l)
  live=$(pgrep -f "out-dir ${AR_ROOT:-$HOME/work/logs/ar/phase3}/runs/$d( |$)" >/dev/null && echo run || echo -)
  stop=$(grep -c "early stop" "$d/optimize.log" 2>/dev/null)
  echo "$d rounds=$r val=$v $live stop=$stop"
done | sed 's/__[a-z_]*\//\//'
for j in ar_p3_opt ar_p3_optB ar_p3_optA8 ar_p3_val_s0 ar_p3_val ar_p3_held; do
  [ -f "$HOME/work/logs/jobs/$j.out" ] && echo "$j status=$(cat "$HOME/work/logs/jobs/$j.status" 2>/dev/null || echo running)"
done
uptime | sed 's/.*load/load/'
