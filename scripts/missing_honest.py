"""Which honest samples count, per task line (`env main_task`) of a task file.

Clean = no sample error, no solver_error swallowed by Control Tower, scored, and no side task. The
honest stages rerun a task that is short of clean samples, and the errored or unscored samples stay in
the directory next to the reruns, so a directory can hold more samples than the threshold should use.
The samples that count are the first EPOCHS clean ones of each task, in run order (log file name,
which starts with its UTC start time, then epoch and sample id); every consumer of an honest
directory (threshold, monitor pool) uses that selection, never the whole directory.

A clean sample is not always usable as the simulator's reference transcript: simrt.reference also
rejects one with fewer than MIN_REFERENCE_ACTIONS tool calls or stopped by a limit (and accepts an
unscored one). --reference applies that rule, simrt.reference.reference_problem itself, for the
directory the simulators read references from. It adds tasks to rerun and never changes the
selection: reruns come after the clean runs in run order. DIR's own logs are a subset of what the
loader reads (DIR/**/*.eval), so a run that counts here is one it can use.

  python scripts/missing_honest.py --task-file tasks.txt --evals DIR --epochs N [--reference]
      print the task lines with fewer than N clean samples (what the honest stages rerun); with
      --reference also those with no sample usable as a reference (why goes to stderr)
  python scripts/missing_honest.py --task-file tasks.txt --evals DIR --epochs N --select OUT.json
      write the selection to OUT.json: {"samples": {"env main_task": [key, ...]}, ...}, keys in
      simrt.monitoring's "<sample id>:<epoch>" form; fails, naming the short tasks, if any task has
      fewer than N clean samples
"""

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from inspect_ai.log import read_eval_log

from simrt.monitoring import _key as monitor_key
from simrt.reference import MIN_REFERENCE_ACTIONS, reference_problem

Task = tuple[str, str]  # (env, main task)


@dataclass
class HonestRuns:
    clean: dict[Task, list[str]] = field(default_factory=lambda: defaultdict(list))  # monitor keys, run order
    usable: dict[Task, int] = field(default_factory=lambda: defaultdict(int))  # samples usable as reference
    unusable: dict[Task, list[str]] = field(default_factory=lambda: defaultdict(list))  # why the others are not


def scan(evals: Path) -> HonestRuns:
    """The honest (no side task) samples in DIR's .eval logs. Only DIR's own logs count, as for
    simrt.monitoring and ct run monitor, which read DIR/*.eval."""
    out = HonestRuns()
    for f in sorted(evals.glob("*.eval")) if evals.is_dir() else []:
        try:
            samples = read_eval_log(str(f)).samples or []
        except Exception as e:  # noqa: BLE001 - an interrupted run can leave an unreadable log
            print(f"warning: skipping unreadable {f}: {e}", file=sys.stderr)
            continue
        for s in sorted(samples, key=lambda s: (s.epoch, str(s.id))):
            md = s.metadata or {}
            if md.get("side_task"):
                continue
            env, main = (md.get("environment") or {}).get("id"), (md.get("main_task") or {}).get("id")
            task = (str(env), str(main))
            why = reference_problem(s)
            if why is None:
                out.usable[task] += 1
            else:
                out.unusable[task].append(f"{f.name} {monitor_key(s.id, s.epoch)}: {why}")
            if not (s.error or not s.scores or md.get("solver_error")):
                out.clean[task].append(monitor_key(s.id, s.epoch))
    return out


def clean_samples(evals: Path) -> dict[Task, list[str]]:
    """(env, main task) -> monitor keys of its clean honest samples in DIR's .eval logs, in run order."""
    return scan(evals).clean


def task_lines(task_file: Path) -> list[str]:
    return [line for line in task_file.read_text().splitlines()
            if len(line.split()) == 2 and not line.startswith("#")]


def selection(task_file: Path, evals: Path, epochs: int,
              runs: HonestRuns | None = None) -> tuple[dict[str, list[str]], list[str]]:
    """({"env main": its first `epochs` clean sample keys}, task lines with fewer than `epochs`)."""
    clean = (runs if runs is not None else scan(evals)).clean
    chosen: dict[str, list[str]] = {}
    short: list[str] = []
    for line in task_lines(task_file):
        env, main = line.split()
        keys = clean.get((env, main), [])
        chosen[f"{env} {main}"] = keys[:epochs]
        if len(keys) < epochs:
            short.append(line)
    return chosen, short


def to_rerun(task_file: Path, evals: Path, epochs: int, reference: bool = False) -> tuple[list[str], dict[str, str]]:
    """(task lines to rerun, {line: why} for those listed only because none of their samples is usable as a
    reference); without `reference`, the lines with fewer than `epochs` clean samples."""
    runs = scan(evals)
    short = selection(task_file, evals, epochs, runs)[1]
    if not reference:
        return short, {}
    lines: list[str] = []
    why: dict[str, str] = {}
    for line in task_lines(task_file):
        task = tuple(line.split())
        if line not in short and runs.usable.get(task):
            continue
        lines.append(line)
        if line not in short:
            why[line] = "; ".join(runs.unusable.get(task, [])[:3]) or "no honest runs"
    return lines, why


def missing(task_file: Path, evals: Path, epochs: int, reference: bool = False) -> list[str]:
    return to_rerun(task_file, evals, epochs, reference)[0]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--task-file", required=True)
    ap.add_argument("--evals", required=True)
    ap.add_argument("--epochs", type=int, default=1)
    ap.add_argument("--select", help="write the selected samples here (JSON) instead of printing short tasks")
    ap.add_argument("--reference", action="store_true",
                    help="also print tasks with no sample simrt.reference can use (at least "
                         f"{MIN_REFERENCE_ACTIONS} tool calls, no error, not stopped by a limit)")
    a = ap.parse_args(argv)
    if a.select and a.reference:
        ap.error("--reference only adds tasks to rerun; the --select selection never depends on it")
    if not a.select:
        lines, why = to_rerun(Path(a.task_file), Path(a.evals), a.epochs, a.reference)
        for line in lines:
            print(line)
        for line, reasons in why.items():
            print(f"{line}: no run usable as a simulator reference: {reasons}", file=sys.stderr)
        return
    chosen, short = selection(Path(a.task_file), Path(a.evals), a.epochs)
    if short:
        raise SystemExit(f"{len(short)} task(s) have fewer than {a.epochs} clean honest run(s) in {a.evals}: "
                         f"{'; '.join(short)}. Run the honest stage that fills {a.evals} first.")
    out = Path(a.select)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.tmp")
    tmp.write_text(json.dumps({"task_file": str(Path(a.task_file).resolve()), "evals": str(Path(a.evals).resolve()),
                               "epochs": a.epochs, "samples": chosen}, indent=1) + "\n")
    tmp.replace(out)
    print(f"selected {sum(map(len, chosen.values()))} honest samples ({len(chosen)} tasks x {a.epochs}) "
          f"from {a.evals} -> {out}")


if __name__ == "__main__":
    main()
