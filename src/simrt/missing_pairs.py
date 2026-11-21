"""Print task-file lines that still lack a successfully scored sample in an eval directory.

Used to resume runs cut short by throttling: rerun only what is missing.
  python -m simrt.missing_pairs --task-file tasks.txt --evals DIR --score-key KEY
"""

import argparse
import math
from pathlib import Path

from inspect_ai.log import read_eval_log
from inspect_ai.scorer import NOANSWER, Score


def _is_answer(v: object) -> bool:
    """A real verdict: not ct's NOANSWER 'N' (scorer timeout or exception), None (judge failed) or NaN."""
    return v is not None and v != NOANSWER and not (isinstance(v, float) and math.isnan(v))


def answered(scores: dict[str, Score], score_key: str) -> bool:
    """True if a score whose name contains `score_key` holds only real verdicts, or a score's value dict
    has a key containing `score_key` with a real verdict."""
    for name, sc in scores.items():
        values = sc.value if isinstance(sc.value, dict) else {name: sc.value}
        if score_key in str(name) and values and all(map(_is_answer, values.values())):
            return True
        if any(score_key in str(k) and _is_answer(v) for k, v in values.items()):
            return True
    return False


def done_pairs(evals: str, score_key: str) -> set[tuple[str, str, str]]:
    done: set[tuple[str, str, str]] = set()
    for f in Path(evals).glob("**/*.eval"):
        for s in read_eval_log(str(f)).samples or []:
            # ct's solver swallows agent exceptions into metadata["solver_error"]; the sample then
            # looks successful (and still gets scored), so treat it as missing too.
            if s.error or not s.scores or (s.metadata or {}).get("solver_error"):
                continue
            if not answered(s.scores, score_key):
                continue
            md = s.metadata or {}
            done.add(
                (
                    str((md.get("environment") or {}).get("id")),
                    str((md.get("main_task") or {}).get("id")),
                    str((md.get("side_task") or {}).get("id")),
                )
            )
    return done


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-file", required=True)
    ap.add_argument("--evals", required=True)
    ap.add_argument("--score-key", required=True)
    a = ap.parse_args()
    done = done_pairs(a.evals, a.score_key) if Path(a.evals).exists() else set()
    for line in Path(a.task_file).read_text().splitlines():
        parts = line.split()
        if len(parts) == 3 and not line.startswith("#") and tuple(parts) not in done:
            print(line)


if __name__ == "__main__":
    main()
