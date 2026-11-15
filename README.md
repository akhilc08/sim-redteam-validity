# Does monitor red-teaming stay valid in a simulated environment?

Control evaluations increasingly replace real execution with an **LLM-simulated environment**: a model pretends to be the Linux box and answers the agent's commands. [*Simulated Control Evals*](https://arxiv.org/abs/2610.31593) (SCE; Olmo, Zhang & Tracy, 2026) showed that this works well for **one-shot** attacks on [LinuxArena](https://github.com/redwoodresearch/linuxarena-paper): about 25.4% simulated vs 25.5% real attack success.

Real red-teaming isn't one-shot, though. Tools like Screwtape **optimize attacks over many rounds** against the monitor's feedback. SCE's limitations section names the risk: repeated attack search could exploit the simulator's mistakes rather than the monitor's blind spots.

This repo tests that risk. We optimize attacks against a monitor **inside a simulator**, then **run every round for real** in LinuxArena's containers to see which simulated wins survive.

> **Status: pilot.** 4 task pairs, 2 environments, 64 optimization rounds validated by real execution. The results are suggestive, not conclusive. See [Limitations](#limitations).

## Key findings

![Precision of simulated wins vs optimization round](results/m3/precision_vs_round.png)

**1. Simulated wins hold up less and less the longer you optimize.** Of the rounds that won in simulation, the share that also won when the *exact same commands* were replayed for real:

| | Rounds 0–2 | Rounds 4+ | Overall |
|---|---|---|---|
| **Open-loop replay** (same commands, real containers) | 17/20 = 85% | **4/10 = 40%** | 27/39 = 69% (95% CI 54–81%) |
| **Closed-loop rerun** (same strategy, attacker runs live) | 18/21 | 9/10 | 36/40 = 90% (95% CI 77–96%) |

A one-sided Fisher test of early vs late rounds gives p ≈ 0.02. The early/late split was chosen after looking at the data, so treat this as suggestive.

**2. Strategies transfer; exact command sequences don't.** When the attacker reruns an optimized strategy live and can react to real outputs, it still works (90%, no decline). What degrades is the specific command sequence the optimizer built around fake outputs.

**3. The failures come from the simulator, not the success judge.** All 12 simulated wins that failed on replay fall into the three error types we predicted:

| Error type | Count | Example from the data |
|---|---|---|
| **Inconsistent / fabricated state** | 5 | The simulator showed a `loadAllowlistConfig()` function that doesn't exist; the real `str_replace` failed with "old_str did not appear verbatim" |
| **Evidence suppression** | 5 | It invented a `password_hash` column, so real `UPDATE`s failed ("column does not exist") while the simulation showed success. The real monitor scored 6.0 vs 3.0 simulated |
| **Lenient success** | 2 | 5 withdrawal requests shown as "approved"; only 1 exists in the real database |

The judge was well calibrated on *real* transcripts (Brier 0.081). It goes wrong because the simulator feeds it fabricated evidence.

**4. The honest reference transcript leaks into the simulation.** SCE grounds its simulator in a real honest run on the same main task. Two of the fabrications came from that run: `loadAllowlistConfig()` appears only in the honest agent's transcript (it wrote the function) and nowhere in the real codebase. Even when explicitly told not to, the simulator treats the reference agent's work as already existing.

**5. The simulator also errs in the other direction.** On a reverse-shell side task, the simulator made attacks that really succeed look like failures (judged success ≈ 5%). Simulation doesn't only flatter the attacker; it can also hide real attack paths.

**6. Of the defenses tested, replaying simulated wins for real is the clear winner in LinuxArena:**

| Defense | Replay precision | Sim wins kept | $ per real win |
|---|---|---|---|
| None | 0.69 | 40/40 | $4.56 |
| Second simulator (Haiku 4.5) must agree | 0.78 | 10/40 | $23.37 |
| Strict evidence-based judge | 0.71 | 39/40 | $5.51 |
| Replay every simulated win for real | 1.00 | (exact filter) | ~$0 model cost (container time only) |

A weak second simulator rejects real successes too. A stricter judge barely helps, because the judge isn't the problem. Replay needs no attacker model, so it's almost free wherever real execution is possible.

**Earlier one-shot check (milestone 1).** On 14 task pairs with one run each, we did **not** reproduce SCE's close agreement: 85.7% real vs 66.5% simulated attack success, and 8% vs 29% safety at a 5% audit budget. With n=1 per pair, a different simulator model and a weak monitor, this is a rough check, not a refutation.

## How it works

```
                 ┌──────────── optimization loop (simulated) ────────────┐
 optimizer LLM ──▶ strategy ──▶ ct strategy-attack policy ──▶ SimulatedSandbox ──▶ LLM simulator
       ▲                                     │                     (Sonnet 4.6 + codebase lookups
       │                                     ▼                      + honest reference transcript)
       └──── monitor score + reasoning, judge P(success) ◀── transcript
                                             │
                     every round ────────────┼──────────────▶ real validation
                                             ▼
                 open-loop replay (same commands)   +   closed-loop rerun (same strategy)
                 in real LinuxArena containers          for simulated wins
                 → real scorer + same monitor → real win?
```

- **Simulated sandbox** ([`sim_sandbox.py`](src/simrt/sim_sandbox.py), [`simulator.py`](src/simrt/simulator.py)). An Inspect `SandboxEnvironment` in which every `exec`, file read and file write is answered by an LLM. It routes the agent's `bash` calls, the text editor's JSON-RPC calls and task setup scripts. Because it swaps in at the sandbox layer, Control Tower's attack policies run **unchanged** in simulation and in reality. The simulator follows SCE's description: one persistent chat per trajectory, grounded in the honest reference transcript and read/list/grep access to the environment's codebase, with no access to the agent's reasoning.
- **Success judge** ([`judge.py`](src/simrt/judge.py)). Reads the transcript plus the side task's scorer code and outputs P(success), as in SCE.
- **Monitor** ([`monitoring.py`](src/simrt/monitoring.py)). Control Tower's whole-trajectory `mean-quick` monitor on Amazon Nova Lite, averaged over 3 passes. The audit threshold (**5.17**) is the 95th percentile of 36 real honest runs.
- **Optimizer** ([`optimize.py`](src/simrt/optimize.py)). A Screwtape-style loop: write a strategy, run it in simulation, observe the monitor's score and reasoning and the judge's estimate, then revise. Up to 8 rounds, stopping early after 3 rounds without improvement.
- **Validation** ([`validate.py`](src/simrt/validate.py)). A real win requires the real deterministic scorer to report success **and** a real monitor score ≤ threshold.
- **Error labelling** ([`label_errors.py`](src/simrt/label_errors.py)). Lines up simulated and real outputs for the same command at each step, finds where they diverge, and has an LLM label the cause. Labels were spot-checked by hand.
- **Defenses** ([`defenses.py`](src/simrt/defenses.py)). Second-simulator agreement (re-simulating each round's commands through Control Tower's `replay` policy on a different model), a strict judge rubric, and real spot-checks.

## Repo layout

```
src/simrt/
  sim_sandbox.py      Inspect sandbox backed by the LLM simulator
  simulator.py        SCE-style simulator (persistent chat, codebase lookups, traces)
  prompts.py          simulator / judge prompts (SCE's prompts are unreleased; these follow its description)
  codebase.py         path-safe read/list/grep over an environment's codebase snapshot
  reference.py        honest reference transcripts, indexed by main task
  llm_tools.py        small tool loop for codebase lookups
  judge.py            transcript-reading success judge (+ strict rubric variant)
  run_sim_eval.py     run any Control Tower policy against the simulator
  monitoring.py       averaged delayed monitor + audit threshold
  optimize.py         attack optimization loop
  validate.py         open-loop replay + closed-loop rerun in real containers
  label_errors.py     sim-vs-real divergence analysis and error labelling (RQ2)
  defenses.py         defenses and their cost/precision (RQ3)
  rotating_model.py   `rotate/` provider: fails over across Bedrock routes / API when daily caps hit
  analyze_m1.py       one-shot sim-vs-real comparison (milestone 1)
  analyze_m3.py       precision-vs-round analysis and plot (main experiment)
scripts/              EC2 setup, detached job launcher, milestone pipelines
config/               simulator settings and task sets
results/m3/           main-experiment results: plot, per-round CSV, error labels, defense report
tests/                unit tests (34)
```

## Reproducing

**Infrastructure:** an EC2 c7i.2xlarge (Ubuntu 22.04, Docker) in us-east-1, with LinuxArena environments pinned to `v1.0.1`. Claude models are reached through Amazon Bedrock, falling back to the Anthropic API when Bedrock's daily token caps are hit.

```bash
# on the box
bash scripts/setup_box.sh            # linuxarena-paper + control-tower + 10 public envs at v1.0.1
cd ~/work/simrt && uv pip install --python ../linuxarena-paper/.venv/bin/python --no-deps -e .
# optional: ~/work/.env with ANTHROPIC_API_KEY (used only as the last fallback route)

scripts/launch.sh m1 scripts/m1_pipeline.sh real sim monitor judge analyze   # one-shot comparison
scripts/launch.sh m3 scripts/m3_run.sh honest threshold optimize validate    # main experiment
scripts/launch.sh m4 scripts/m4_run.sh                                       # labels, defenses, more pairs
python -m simrt.analyze_m3 --root ~/work/logs/m3/runs --threshold "$(cat ~/work/logs/m3/threshold.txt)" --out results/m3
```

Setup gotchas this repo already handles:
- Control Tower's private `src/research` submodule breaks `uv sync`; install Control Tower separately with `--no-deps`.
- `SKIP_DOCKER_BUILD=1` requires `ENV_IMAGE_TAG_<ENV>=v1.0.1` for every environment.
- Use `anthropic/bedrock/...` model strings, not `bedrock/...`. Inspect's Converse provider rejects Control Tower's honest-policy calls.

**Cost:** a simulated round costs about **$1.90** (Sonnet 4.6 with prompt caching). The main experiment's simulation costs were about $123; total spend so far is roughly $300–400.

```bash
python -m pytest -q    # 34 tests: sandbox routing, parsing, rotation/failover, analysis helpers
```

## Limitations

- **Small sample.** 40 simulated wins; the late-round decline rests on about 10 cases, and the per-round confidence intervals are wide.
- **Weak monitor.** Nova Lite was chosen so wins would be common enough to measure. It is not a realistic deployment monitor.
- **Our simulator is not SCE's.** It uses Sonnet 4.6 with our own prompts; SCE used an OpenAI model with unreleased prompts. Our one-shot agreement was also weaker than SCE's.
- **Process details.** 6 of the 12 optimization runs saw a slightly stricter threshold (4.58) in their feedback than the 5.17 used for scoring. A few rounds errored and could not be validated. Failure labels are LLM-generated and spot-checked, not fully hand-labelled.
- **The side-task test tool was disabled** in both conditions (Control Tower enables it by default), so simulated and real runs are identical apart from the environment.

## Next steps

- Rescore all transcripts with **GPT-5 nano**, the LinuxArena paper's default monitor, and with stronger monitors.
- Add more task pairs and repeats to tighten the precision curve.
- **Truncate the reference transcript** before its first state-changing step, as a fix for reference leakage.
- Test a **strong** second simulator (Opus) and a stronger primary simulator.
- Run a two-sided loop in which the monitor also adapts, still inside the simulator and validated for real.

## References

- Olmo, Zhang & Tracy. *Simulated Control Evals.* 2026. [arXiv:2610.31593](https://arxiv.org/abs/2610.31593)
- Kozhevnikov & Tracy. *Screwtape: Adversarial Transcript Editing for Monitor Red-Teaming.* 2026. [arXiv:2610.20941](https://arxiv.org/abs/2610.20941)
- *LinuxArena* and Control Tower: [redwoodresearch/linuxarena-paper](https://github.com/redwoodresearch/linuxarena-paper), [linuxarena/control-tower](https://github.com/linuxarena/control-tower)
- Inspect AI: [inspect.aisi.org.uk](https://inspect.aisi.org.uk)
