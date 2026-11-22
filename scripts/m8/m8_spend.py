"""M8 spend estimate from token counts (run on the box with $PY): python m8_spend.py [ROOT]

Extended copy of scripts/m7/p3_spend.py (same monitor and eval-log accounting, ROOT default ~/work/logs/m8/pilot),
plus the strategy-writer calls, which are outside eval logs: every rounds.jsonl record's `proposal` usage (written
by simrt.optimize with any M8 option), priced by model: Opus 4.6 $5/$25 per M tokens (cache read 0.50, write 6.25),
Sonnet 4.6 $3/$15 (0.30 / 3.75). Monitors: Nova Pro 0.80/3.20 (runs/), Haiku 5.5 0.10/0.50 (heldout/, Anthropic).
"""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from inspect_ai.log import read_eval_log

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "work/logs/m8/pilot"
MON = {"runs": (0.80, 3.20), "heldout": (0.10, 0.50)}
CLAUDE = {"opus": (5.0, 25.0, 0.50, 6.25), "sonnet": (3.0, 15.0, 0.30, 3.75)}


def mon_spend(sub: str) -> dict:
    tin = tout = 0
    for log in (ROOT / sub).rglob("monitor.log"):
        t = log.read_text(errors="replace")
        tin += sum(int(x.replace(",", "")) for x in re.findall(r"Input tokens:\s+([\d,]+)", t))
        tout += sum(int(x.replace(",", "")) for x in re.findall(r"Output tokens:\s+([\d,]+)", t))
    pin, pout = MON[sub]
    return {"in": tin, "out": tout, "usd": round(tin / 1e6 * pin + tout / 1e6 * pout, 2)}


def agent_spend() -> dict:
    by = defaultdict(float)
    n = 0
    for ev in (ROOT / "runs").rglob("*.eval"):
        if "/mon" in str(ev):
            continue
        try:
            log = read_eval_log(str(ev), header_only=True)
        except Exception:  # noqa: BLE001
            continue
        n += 1
        kind = ev.parent.name
        for model, u in (log.stats.model_usage or {}).items():
            if "nova" in model:
                continue
            pi, po, pr, pw = CLAUDE["opus" if "opus" in model else "sonnet"]
            by[kind] += (u.input_tokens * pi + u.output_tokens * po + (u.input_tokens_cache_read or 0) * pr
                         + (u.input_tokens_cache_write or 0) * pw) / 1e6
    return {"n_evals": n, **{k: round(v, 2) for k, v in by.items()}, "usd": round(sum(by.values()), 2)}


def optimizer_spend() -> dict:
    tot, n, missing = defaultdict(float), 0, 0
    for rf in (ROOT / "runs").rglob("rounds.jsonl"):
        for line in rf.read_text().splitlines():
            u = (json.loads(line).get("proposal") or {}) if line.strip() else {}
            if u.get("input_tokens") is None:
                missing += 1
                continue
            n += 1
            pi, po, pr, pw = CLAUDE["opus" if "opus" in (u.get("model") or "") else "sonnet"]
            tot["in"] += u["input_tokens"]
            tot["out"] += u["output_tokens"]
            tot["usd"] += (u["input_tokens"] * pi + u["output_tokens"] * po + (u.get("cache_read") or 0) * pr
                           + (u.get("cache_write") or 0) * pw) / 1e6
    return {"calls": n, "records_without_usage": missing, "in": int(tot["in"]), "out": int(tot["out"]),
            "usd": round(tot["usd"], 2)}


out = {"target_monitor": mon_spend("runs"), "heldout_monitor": mon_spend("heldout") if (ROOT / "heldout").exists()
       else {"usd": 0}, "agents_bedrock": agent_spend(), "optimizer_bedrock": optimizer_spend()}
out["bedrock_usd"] = round(out["target_monitor"]["usd"] + out["agents_bedrock"]["usd"] + out["optimizer_bedrock"]["usd"], 2)
print(json.dumps(out, indent=1))
