"""Run the success judge over existing eval logs (e.g. REAL attack runs) -> {sample_id: prob}.

  python -m simrt.judge_cli --evals DIR --model MODEL --envs-dir DIR --out probs.json

OUT holds {sample_id: {"prob", "rationale", "judge_error", "judge_usage"}}; "prob" is None when the
judge failed. OUT with suffix .probs.json holds {sample_id: prob} for the samples judged successfully.
Both files are always written; the exit status is 1 when any sample was not judged successfully.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from inspect_ai.log import EvalSample, read_eval_log

from simrt.judge import judge_transcript


def load_samples(evals: str) -> list[EvalSample]:
    """Attack samples (with a side task). Read before any event loop runs: read_eval_log runs its own."""
    return [
        s
        for f in sorted(Path(evals).glob("**/*.eval"))
        for s in (read_eval_log(str(f)).samples or [])
        if (s.metadata or {}).get("side_task")
    ]


async def judge_samples(
    samples: list[EvalSample], model: str, envs_dir: str, concurrency: int
) -> dict[str, dict[str, object]]:
    sem = asyncio.Semaphore(concurrency)

    async def one(s: EvalSample) -> tuple[str, dict[str, object]]:
        async with sem:
            try:
                v = await judge_transcript(model, envs_dir, dict(s.metadata), list(s.messages))
            except Exception as e:  # noqa: BLE001 - one failed call must not lose the other samples
                return str(s.id), {"prob": None, "rationale": "", "judge_error": f"{type(e).__name__}: {e}"[:500],
                                   "judge_usage": {}}
            return str(s.id), {"prob": v.probability, "rationale": v.rationale[-3000:], "judge_error": v.error,
                               "judge_usage": v.usage}

    return dict(await asyncio.gather(*(one(s) for s in samples)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evals", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--envs-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--concurrency", type=int, default=4)
    a = ap.parse_args()
    samples = load_samples(a.evals)
    res = asyncio.run(judge_samples(samples, a.model, a.envs_dir, a.concurrency))
    Path(a.out).write_text(json.dumps(res, indent=1))
    probs = {k: v["prob"] for k, v in res.items() if v["prob"] is not None}
    Path(a.out).with_suffix(".probs.json").write_text(json.dumps(probs))
    failed = sorted(k for k in res if k not in probs)
    print(f"judged {len(res)} samples -> {a.out}" + (f" ({len(failed)} without a probability)" if failed else ""))
    if failed:  # a wrong model id, auth or throttling must stop the pipeline, as an exception did before
        print(f"judge failed for {len(failed)} of {len(res)} samples: {', '.join(failed[:20])}; "
              f"reasons are under judge_error in {a.out}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
