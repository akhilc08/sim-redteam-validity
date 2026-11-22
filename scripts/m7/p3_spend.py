"""Phase 3 spend estimate from token counts (run on the box with $PY): python p3_spend.py [ROOT]

Monitors: "Input/Output tokens" lines in every monitor.log under ROOT (default ~/work/logs/ar/phase3); the model
is the one in the pass marker's path context (runs/ = Nova Pro target, heldout/ = Haiku 5.5).
Agents: inspect eval-log stats.model_usage of every *.eval under runs/ (attacker, simulator, scorer calls made
inside the eval). The optimizer's own strategy-writer calls are outside eval logs, so they are not counted.
Prices $/M tokens: Sonnet 4.6 3/15 (cache read 0.30, write 3.75), Nova Pro 0.80/3.20, Haiku 5.5 0.10/0.50.
"""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from inspect_ai.log import read_eval_log

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "work/logs/ar/phase3"
MON = {"runs": (0.80, 3.20), "heldout": (0.10, 0.50)}


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
        except Exception:
            continue
        n += 1
        kind = ev.parent.name
        for model, u in (log.stats.model_usage or {}).items():
            if "nova" in model:
                continue
            usd = (u.input_tokens * 3 + u.output_tokens * 15 + (u.input_tokens_cache_read or 0) * 0.30
                   + (u.input_tokens_cache_write or 0) * 3.75) / 1e6
            by[kind] += usd
    return {"n_evals": n, **{k: round(v, 2) for k, v in by.items()}, "usd": round(sum(by.values()), 2)}


out = {"target_monitor": mon_spend("runs"), "heldout_monitor": mon_spend("heldout") if (ROOT / "heldout").exists()
       else {"usd": 0}, "agents_bedrock": agent_spend()}
out["bedrock_usd"] = round(out["target_monitor"]["usd"] + out["agents_bedrock"]["usd"], 2)
print(json.dumps(out, indent=1))
