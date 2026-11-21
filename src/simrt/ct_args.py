"""Control Tower policy arguments shared by simulated and real runs, so both use the same attacker."""

from pathlib import Path

# Inspect's on-disk generate cache returns a stored response whenever a call's whole input repeats (the
# first exploration call of every round, repeat and rerun of a task pair is identical), so those runs would
# not be independent draws. ct turns it on by default for strategy-attack in two places:
# - the `model_cache` sub-argument of the `model` arg becomes GenerateConfig(cache=True), which every react
#   step uses (control_tower/protocols/function_registry/custom_handlers.py:148-225);
# - the policy's own `cache` arg is the planning call's CachePolicy (strategy_attack/policy.py:275, 347).
NO_CACHE_ARGS = ["-p", "model_cache=false", "-p", "cache=false"]


def strategy_attack_args(model: str, strat_file: Path | str) -> list[str]:
    """`--policy strategy-attack` with its args: side-task test tool off, response cache off.

    With enable_side_task_test=false the policy's prompts still tell the agent to use run_side_task_test
    (strategy_attack/policy.py:133, user_prompt.md:21,29-30); ct has no argument to change that. A partial
    remedy is possible from simrt but not applied (it changes the attacker relative to the committed
    results, so it is the user's call): the strategy text, which simrt writes (optimize.run_sim_round) and
    validate's closed-loop rerun reuses, is shown to the attacker before the planning prompt in both
    conditions (strategy_attack/policy.py:186-197, 347-401), so a fixed note there (or in OPTIMIZER_SYSTEM)
    could say the tool is unavailable; the exploration and execution prompts would still mention it.
    """
    return [
        "--policy", "strategy-attack", "-p", f"model={model}", "-p", "enable_side_task_test=false",
        "-p", f"strat_file={strat_file}", *NO_CACHE_ARGS,
    ]
