"""Run the success judge over existing eval logs (e.g. REAL attack runs) -> {sample_id: prob}.

  python -m simrt.judge_cli --evals DIR --model MODEL --envs-dir DIR --out probs.json
"""

import argparse
import asyncio
import json
from pathlib import Path

from inspect_ai.log import read_eval_log

from simrt.judge import judge_transcript


async def _run(evals: str, model: str, envs_dir: str, concurrency: int) -> dict[str, dict[str, object]]:
    sem = asyncio.Semaphore(concurrency)
    samples = [
        s
        for f in sorted(Path(evals).glob("**/*.eval"))
        for s in (read_eval_log(str(f)).samples or [])
        if (s.metadata or {}).get("side_task")
    ]

    async def one(s) -> tuple[str, dict[str, object]]:  # noqa: ANN001
        async with sem:
            v = await judge_transcript(model, envs_dir, dict(s.metadata), list(s.messages))
            return str(s.id), {"prob": v.probability, "rationale": v.rationale[-3000:]}

    return dict(await asyncio.gather(*(one(s) for s in samples)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evals", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--envs-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--concurrency", type=int, default=4)
    a = ap.parse_args()
    res = asyncio.run(_run(a.evals, a.model, a.envs_dir, a.concurrency))
    Path(a.out).write_text(json.dumps(res, indent=1))
    Path(a.out).with_suffix(".probs.json").write_text(json.dumps({k: v["prob"] for k, v in res.items()}))
    print(f"judged {len(res)} samples -> {a.out}")


if __name__ == "__main__":
    main()
