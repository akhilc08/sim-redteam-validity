"""Run a Control Tower policy against the LLM-simulated environment.

Identical to `ct run eval` (same dataset, same policy solver incl. task setup), except:
- every sample's sandbox is the `simulated` sandbox (simrt.sim_sandbox), and
- the real scorer is replaced by the SCE-style success judge, whose epochs are reduced with
  `judged_mean` (the log header's mean leaves out samples the judge gave no probability).

Model retries follow the same policy as real runs. `ct run eval` passes neither max_retries nor timeout
to Inspect's eval() (control_tower/ct_runs/ai_eval/task.py:237-245), so retryable errors (throttling,
overload, 5xx) are retried with exponential backoff and no cap on attempts or total time; ct gives
policy models a 120 s per-attempt timeout in both conditions. --max-retries/--timeout set a cap (the
committed results used --max-retries 6 --timeout 600, which made throttled simulated rounds error out
while real runs waited).

Usage (on the box, from the control-tower dir):
  python -m simrt.run_sim_eval --task-file tasks.txt --policy attack \
      -p model=anthropic/bedrock/us.anthropic.claude-sonnet-4-6 -p enable_side_task_test=false \
      --sim-settings sim.json --judge-model anthropic/bedrock/us.anthropic.claude-sonnet-4-6 --log-dir logs/sim
"""

import argparse
from pathlib import Path

from inspect_ai import Epochs, Task, eval
from inspect_ai.dataset import MemoryDataset
from inspect_ai.util import SandboxEnvironmentSpec

import simrt.sim_sandbox  # noqa: F401  (registers the "simulated" sandbox type)
from simrt.judge import judged_mean, sim_success_judge
from simrt.sim_sandbox import SIM_SANDBOX_NAME
from simrt.simulator import SimSettings


def _parse_kv(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in pairs:
        k, sep, v = p.partition("=")
        if not sep:
            raise SystemExit(f"bad -p value (want key=value): {p}")
        out[k.strip().replace("-", "_")] = v.strip()
    return out


def build_task(
    task_file: str,
    policy: str,
    policy_args: dict[str, str],
    sim_settings: str,
    judge_model: str,
    message_limit: int,
) -> Task:
    from control_tower.ct_runs.ai_eval.dataset import build_eval_dataset, resolve_task_combinations
    from control_tower.ct_runs.ai_eval.task_selection import TaskSelectionInput
    from control_tower.protocols.function_registry.validation import validate_function_args
    from control_tower.protocols.policies.registry import policies
    from control_tower.protocols.policies.solver import build_solver

    selection = TaskSelectionInput(task_file=task_file)
    combos = resolve_task_combinations(selection)
    dataset = build_eval_dataset(selection=selection, task_combinations=combos)

    settings_path = str(Path(sim_settings).resolve())
    settings = SimSettings.load(settings_path)
    samples = []
    for s in dataset:
        s.sandbox = SandboxEnvironmentSpec(type=SIM_SANDBOX_NAME, config=settings_path)
        samples.append(s)

    info = policies.get(policy)
    args = validate_function_args(info, policy_args)
    return Task(
        dataset=MemoryDataset(samples, name=f"sim-{policy}"),
        solver=build_solver(info.function, args),
        scorer=sim_success_judge(judge_model, settings.envs_dir),
        epochs=Epochs(1, reducer=judged_mean()),  # eval(epochs=N) sets the count and keeps this reducer
        message_limit=message_limit,
        metadata={
            "simrt_mode": "simulated",
            "policy": policy,
            "policy_args": policy_args,
            "sim_settings": settings.__dict__,
            "judge_model": judge_model,
            "task_file": task_file,
        },
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--task-file", required=True)
    ap.add_argument("--policy", default="attack")
    ap.add_argument("-p", "--policy-arg", action="append", default=[])
    ap.add_argument("--sim-settings", required=True)
    ap.add_argument("--judge-model", required=True)
    ap.add_argument("--log-dir", default="logs/sim")
    ap.add_argument("--epochs", type=int, default=1)
    ap.add_argument("--max-samples", type=int, default=8)
    ap.add_argument("--message-limit", type=int, default=300)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--max-retries", type=int, default=None,
                    help="cap on model attempts (default: none, as in ct run eval; committed results used 6)")
    ap.add_argument("--timeout", type=int, default=None,
                    help="cap in seconds on a model call incl. retries (default: none, as in ct run eval; committed results used 600)")
    a = ap.parse_args()

    task = build_task(
        a.task_file, a.policy, _parse_kv(a.policy_arg), a.sim_settings, a.judge_model, a.message_limit
    )
    eval(
        task,
        log_dir=a.log_dir,
        epochs=a.epochs,
        max_samples=a.max_samples,
        limit=a.limit,
        fail_on_error=False,
        max_retries=a.max_retries,
        timeout=a.timeout,
    )


if __name__ == "__main__":
    main()
