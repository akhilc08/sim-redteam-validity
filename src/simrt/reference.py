"""Honest reference transcripts (real executed runs), indexed by main task id."""

from functools import lru_cache
from pathlib import Path

from inspect_ai.log import read_eval_log

from simrt.transcript import render_actions


@lru_cache(maxsize=4)
def _index(reference_dir: str) -> dict[str, str]:
    """main_task_id -> path of an .eval file + sample index ("path::idx"). First match wins."""
    index: dict[str, str] = {}
    for path in sorted(Path(reference_dir).glob("**/*.eval")):
        log = read_eval_log(str(path), header_only=False)
        for i, s in enumerate(log.samples or []):
            mt = (s.metadata or {}).get("main_task") or {}
            mt_id = mt.get("id") if isinstance(mt, dict) else None
            if mt_id and mt_id not in index and not (s.metadata or {}).get("side_task"):
                index[mt_id] = f"{path}::{i}"
    return index


@lru_cache(maxsize=64)
def load_reference(reference_dir: str, main_task_id: str | None, max_chars: int) -> str:
    if not main_task_id:
        return "(no reference transcript available)"
    loc = _index(reference_dir).get(main_task_id)
    if loc is None:
        return "(no reference transcript available for this main task)"
    path, idx = loc.rsplit("::", 1)
    sample = (read_eval_log(path).samples or [])[int(idx)]
    return render_actions(sample.messages, output_chars=4_000, total_chars=max_chars)
