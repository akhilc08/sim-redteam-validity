"""M8 budget guard (run on the box with $PY, under scripts/launch.sh): stop an optimizer run at a round boundary when
going on would break the Bedrock cap. Every POLL seconds, for each run dir under ROOT/runs/*/rep* whose optimizer is
alive (a simrt.optimize process with its --out-dir runs): when its rounds.jsonl gains a line, compute
    projected = spent (m8_spend.py) + VAL_USD x (recorded rounds not yet validated)
                + mean $/recorded round x (live runs)          # the next round of every live run
and if projected x 1.1 > CAP, kill that run's processes (every process whose command line holds the run dir: the
optimizer, its rollouts and their monitor calls), so it ends after its last recorded round. Validation is never
skipped: the autoval tail validates the recorded rounds. Decisions are printed with UTC timestamps.
  python m8_budget.py [--root ~/work/logs/m8/pilot] [--cap 110] [--val-usd 1.0] [--poll 20]
Scale-up: --root is repeatable (one root per arm; spend summed over all roots, the $/round rate of a live run is its
own root's, or --default-rate before that root has a recorded round), --reserve USD is held back for later stages
that are not launched yet, and while --wait-file exists the guard keeps running when no optimizer is live.
"""
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%SZ")


def lines(p: Path) -> int:
    return len([x for x in p.read_text().splitlines() if x.strip()]) if p.exists() else 0


def alive(run: Path) -> bool:
    """An optimizer (simrt.optimize with --out-dir run) is running. Not opt.lock: validate holds that lock too, and a
    run being validated must not be charged a next round (scale-up fix, 04:15 UTC)."""
    pat = f"simrt.optimize .*--out-dir {run} "
    return subprocess.run(["pgrep", "-f", pat], capture_output=True).returncode == 0


def spent(roots: list[Path]) -> tuple[float, dict[Path, float]]:
    out = subprocess.run([sys.executable, str(Path(__file__).with_name("m8_spend.py")), "--per-root",
                          *[str(r) for r in roots]], capture_output=True, text=True, check=True).stdout
    d = json.loads(out[out.index("{"):])
    return d["bedrock_usd"], {Path(k): v["bedrock_usd"] for k, v in d["roots"].items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", action="append", default=None, help="repeatable (default ~/work/logs/m8/pilot)")
    ap.add_argument("--cap", type=float, default=110.0)
    ap.add_argument("--val-usd", type=float, default=1.0, help="expected validation cost per round (replay + rerun)")
    ap.add_argument("--poll", type=float, default=20.0)
    ap.add_argument("--default-rate", type=float, default=4.0, help="$/round for a root with no recorded round yet")
    ap.add_argument("--reserve", type=float, default=0.0, help="USD held back for stages not launched yet")
    ap.add_argument("--wait-file", default=None, help="keep running while this file exists")
    a = ap.parse_args()
    roots = [Path(x).expanduser() for x in (a.root or [str(Path.home() / "work/logs/m8/pilot")])]
    seen: dict[Path, int] = {}
    print(f"{now()} budget guard: cap ${a.cap}, validation ${a.val_usd}/round, reserve ${a.reserve}, margin 10%, "
          f"roots {[str(r) for r in roots]}", flush=True)
    while True:
        runs = sorted(p for root in roots for p in (root / "runs").glob("*/rep*") if p.is_dir())
        live = [r for r in runs if alive(r)]
        for r in live:
            n = lines(r / "rounds.jsonl")
            if n <= seen.get(r, 0):
                continue
            seen[r] = n
            s, per_root = spent(roots)
            rounds = sum(lines(x / "rounds.jsonl") for x in runs)
            unval = sum(max(lines(x / "rounds.jsonl") - lines(x / "validation.jsonl"), 0) for x in runs)

            def rate(root: Path) -> float:
                k = sum(lines(x / "rounds.jsonl") for x in runs if x.is_relative_to(root))
                return per_root.get(root, 0.0) / k if k else a.default_rate

            nxt = sum(rate(next(root for root in roots if x.is_relative_to(root))) for x in live)
            proj = s + a.val_usd * unval + nxt + a.reserve
            stop = proj * 1.1 > a.cap
            print(f"{now()} {r}: {n} rounds recorded; spent ${s:.2f} over {rounds} rounds, "
                  f"{unval} to validate, {len(live)} live (next rounds ${nxt:.2f}), reserve ${a.reserve:.2f} -> "
                  f"projected ${proj:.2f} (+10% ${proj * 1.1:.2f}) {'STOP' if stop else 'go on'}", flush=True)
            if stop:
                subprocess.run(["pkill", "-TERM", "-f", f"{r}/"], check=False)
                subprocess.run(["pkill", "-TERM", "-f", "--", f"--out-dir {r} "], check=False)
                print(f"{now()} stopped {r} after round {n - 1}", flush=True)
        if not live and runs and not (a.wait_file and Path(a.wait_file).expanduser().exists()):
            print(f"{now()} no live optimizer left; guard exits", flush=True)
            return
        time.sleep(a.poll)


if __name__ == "__main__":
    main()
