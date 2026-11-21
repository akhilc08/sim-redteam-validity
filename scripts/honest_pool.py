"""Apply honest-sample selections (scripts/missing_honest.py --select) to monitor outputs.

  python scripts/honest_pool.py threshold --out FILE --pool SCORES SELECTED [--pool ...] [--expect N]
                                          [--force]
      The audit threshold (simrt.monitoring.audit_threshold) over the selected samples' scores; SCORES
      is a `python -m simrt.monitoring --out` file. Fails if a selected sample has no score there or
      the pool is not N samples.
  python scripts/honest_pool.py threshold --legacy --out FILE --pool SCORES [--pool ...] [--expect N]
      The committed rule: every sample in each SCORES file counts, and a pool of other than N samples
      is only a warning.
  python scripts/honest_pool.py trajs --selected SELECTED --src DIR --dst DIR
      Copy the monitored trajectories of the selected samples (ct run monitor --save-dir output in
      DIR) into a fresh DST; fails if a selected sample has none.

FILE is written when it does not exist or already holds the same value. Later stages read it, so a
different value is written next to it (threshold.txt -> threshold.new.txt) and the command fails,
unless --force, which replaces FILE and keeps the old value in threshold.prev.txt.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

from simrt.monitoring import AUDIT_BUDGET, EPS, audit_threshold
from simrt.monitoring import _key as monitor_key


def selected_keys(selection: Path) -> list[str]:
    return [k for keys in json.loads(selection.read_text())["samples"].values() for k in keys]


def pool_scores(pools: list[list[str]], legacy: bool) -> list[float]:
    scores: list[float] = []
    for pool in pools:
        table = json.loads(Path(pool[0]).read_text())
        if legacy:
            scores += [float(v["sus"]) for v in table.values()]
            continue
        keys = selected_keys(Path(pool[1]))
        lost = [k for k in keys if (table.get(k) or {}).get("sus") is None]
        if lost:
            raise SystemExit(f"{len(lost)} selected honest sample(s) have no monitor score in {pool[0]} (see "
                             f"{Path(pool[0]).with_suffix('.failed.json')}): {', '.join(lost[:10])}")
        scores += [float(table[k]["sus"]) for k in keys]
    return scores


def sibling(path: Path, tag: str) -> Path:
    return path.with_name(f"{path.stem}.{tag}{path.suffix}")


def write_value(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    tmp.replace(path)


def write_threshold(path: Path, t: float, force: bool) -> None:
    old_text = path.read_text().strip() if path.exists() else None
    try:
        same = old_text is not None and abs(float(old_text) - t) <= EPS
    except ValueError:
        same = False
    if same:
        print(f"{path} unchanged")
        return
    if old_text is not None and not force:
        write_value(sibling(path, "new"), f"{t}\n")
        raise SystemExit(f"{path} holds {old_text}, but this run gives {t}: wrote {sibling(path, 'new')} and left "
                         f"{path} as it is (later stages read it). To replace it, run again with --force "
                         "(SIMRT_FORCE_THRESHOLD=1 in the stage scripts).")
    if old_text is not None:
        shutil.copy2(path, sibling(path, "prev"))
        print(f"replacing {path} ({old_text}; kept in {sibling(path, 'prev')})")
    write_value(path, f"{t}\n")


def threshold(a: argparse.Namespace) -> None:
    for pool in a.pool:
        if len(pool) != (1 if a.legacy else 2):
            raise SystemExit(f"--pool takes {'SCORES' if a.legacy else 'SCORES SELECTED'}, got {pool}")
    scores = pool_scores(a.pool, a.legacy)
    if not scores:
        raise SystemExit("no honest scores")
    t = audit_threshold(scores)
    print(f"honest n={len(scores)} threshold@{AUDIT_BUDGET:.0%} = {t}" + (" (legacy rule)" if a.legacy else ""))
    if a.expect is not None and len(scores) != a.expect:
        msg = f"expected {a.expect} honest samples, got {len(scores)}"
        if not a.legacy:
            raise SystemExit(f"{msg}; {a.out} not written")
        print(f"WARNING: {msg}")
    write_threshold(Path(a.out), t, a.force)


def copy_trajs(selection: Path, src: Path, dst: Path) -> int:
    keys = set(selected_keys(selection))
    found: dict[str, Path] = {}
    for p in sorted(src.glob("**/*.json")):
        try:
            t = json.loads(p.read_text())
        except ValueError:
            continue
        k = monitor_key(t.get("eval_sample_id"), t.get("epoch")) if isinstance(t, dict) else None
        if k in keys:
            if k in found:
                raise SystemExit(f"two monitored trajectories for {k}: {found[k]} and {p}")
            found[k] = p
    lost = sorted(keys - found.keys())
    if lost:
        raise SystemExit(f"{len(lost)} selected honest sample(s) have no monitored trajectory in {src}: "
                         f"{', '.join(lost[:10])}")
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    for p in found.values():
        shutil.copy2(p, dst / p.name)
    return len(found)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    th = sub.add_parser("threshold")
    th.add_argument("--out", required=True)
    th.add_argument("--pool", nargs="+", action="append", required=True, metavar="FILE")
    th.add_argument("--expect", type=int)
    th.add_argument("--force", action="store_true")
    th.add_argument("--legacy", action="store_true")
    tr = sub.add_parser("trajs")
    tr.add_argument("--selected", required=True)
    tr.add_argument("--src", required=True)
    tr.add_argument("--dst", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "threshold":
        threshold(a)
    else:
        n = copy_trajs(Path(a.selected), Path(a.src), Path(a.dst))
        print(f"{n} selected honest trajectories -> {a.dst}", file=sys.stderr)


if __name__ == "__main__":
    main()
