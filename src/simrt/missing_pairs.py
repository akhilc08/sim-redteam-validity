"""Print task-file lines that still lack a successfully scored sample in an eval directory.

Used to resume runs cut short by throttling: rerun only what is missing.
  python -m simrt.missing_pairs --task-file tasks.txt --evals DIR --score-key KEY
"""

import argparse
from pathlib import Path

from inspect_ai.log import read_eval_log


def done_pairs(evals: str, score_key: str) -> set[tuple[str, str, str]]:
    done: set[tuple[str, str, str]] = set()
    for f in Path(evals).glob("**/*.eval"):
        for s in read_eval_log(str(f)).samples or []:
            # ct's solver swallows agent exceptions into metadata["solver_error"]; the sample then
            # looks successful (and still gets scored), so treat it as missing too.
            if s.error or not s.scores or (s.metadata or {}).get("solver_error"):
                continue
            if not any(score_key in str(k) or score_key in str(v.value) for k, v in s.scores.items()):
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
