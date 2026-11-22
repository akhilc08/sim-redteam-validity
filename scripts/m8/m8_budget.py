"""M8 budget guard (run on the box with $PY, under scripts/launch.sh): stop an optimizer run at a round boundary when
going on would break the Bedrock cap. Every POLL seconds, for each run dir under ROOT/runs/*/rep* whose optimizer is
alive (its opt.lock is held): when its rounds.jsonl gains a line, compute
    projected = spent (m8_spend.py) + VAL_USD x (recorded rounds not yet validated)
                + mean $/recorded round x (live runs)          # the next round of every live run
and if projected x 1.1 > CAP, kill that run's processes (every process whose command line holds the run dir: the
optimizer, its rollouts and their monitor calls), so it ends after its last recorded round. Validation is never
skipped: the autoval tail validates the recorded rounds. Decisions are printed with UTC timestamps.
  python m8_budget.py [--root ~/work/logs/m8/pilot] [--cap 110] [--val-usd 1.0] [--poll 20]
"""
import argparse
import fcntl
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
    lock = run / "opt.lock"
    if not lock.exists():
        return False
    with lock.open() as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
        return False


def spent(root: Path) -> float:
    out = subprocess.run([sys.executable, str(Path(__file__).with_name("m8_spend.py")), str(root)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out[out.index("{"):])["bedrock_usd"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(Path.home() / "work/logs/m8/pilot"))
    ap.add_argument("--cap", type=float, default=110.0)
    ap.add_argument("--val-usd", type=float, default=1.0, help="expected validation cost per round (replay + rerun)")
    ap.add_argument("--poll", type=float, default=20.0)
    a = ap.parse_args()
    root = Path(a.root).expanduser()
    seen: dict[Path, int] = {}
    print(f"{now()} budget guard: cap ${a.cap}, validation ${a.val_usd}/round, margin 10%", flush=True)
    while True:
        runs = sorted(p for p in (root / "runs").glob("*/rep*") if p.is_dir())
        live = [r for r in runs if alive(r)]
        for r in live:
            n = lines(r / "rounds.jsonl")
            if n <= seen.get(r, 0):
                continue
            seen[r] = n
            s = spent(root)
            rounds = sum(lines(x / "rounds.jsonl") for x in runs)
            unval = sum(max(lines(x / "rounds.jsonl") - lines(x / "validation.jsonl"), 0) for x in runs)
            per = s / rounds if rounds else 0.0
            proj = s + a.val_usd * unval + per * len(live)
            stop = proj * 1.1 > a.cap
            print(f"{now()} {r.name}: {n} rounds recorded; spent ${s:.2f} over {rounds} rounds (${per:.2f}/round), "
                  f"{unval} to validate, {len(live)} live -> projected ${proj:.2f} (+10% ${proj * 1.1:.2f}) "
                  f"{'STOP' if stop else 'go on'}", flush=True)
            if stop:
                subprocess.run(["pkill", "-TERM", "-f", f"{r}/"], check=False)
                subprocess.run(["pkill", "-TERM", "-f", f"--out-dir {r}"], check=False)
                print(f"{now()} stopped {r} after round {n - 1}", flush=True)
        if not live and runs:
            print(f"{now()} no live optimizer left; guard exits", flush=True)
            return
        time.sleep(a.poll)


if __name__ == "__main__":
    main()
