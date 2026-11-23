# Does monitor red-teaming stay valid in a simulated environment?

[*Simulated Control Evals*](https://arxiv.org/abs/2610.31593) (SCE) found that LLM-simulated LinuxArena runs match real ones *on average*. But attack optimization relies on *individual* simulated verdicts, and SCE names the risk that repeated search could exploit simulator errors. This repo tests that: it optimizes attacks against a monitor **inside a simulator**, then checks every round **in real LinuxArena containers**, both as an exact replay of the simulated commands and as a fresh live run of the same strategy.

## Current findings (M7–M8, 2026-11-23)

9 task pairs, 15 optimization runs, 83 rounds, every round checked for real. Target monitor: Nova Pro (threshold 4.00 at 5% honest false positives). Attacker, simulator, judge and optimizer: Sonnet 4.6.

- **Simulated wins predict real wins.** In fresh live runs of the same strategy, 28 of 35 simulated wins were real wins, against 10 of 48 simulated losses (80% vs 21%, p = 1e-7).
- **No sign that the sim-to-real gap grows with optimization.** Late rounds look slightly worse overall (15/16 vs 13/19), but that comes from which pairs reach late rounds. Within pairs there is no decline (stratified p = 1.0).
- **Misses look like monitor noise, not simulator exploitation.** Every simulated win that lost for real was a monitor catch near the threshold.
- **The optimizer beats resampling only in simulation.** Against simply re-drawing the first strategy the same number of times, it wins more often in simulation (58% vs 26%, p = 0.016) but not for real (65% vs 56%, p = 0.44). So the real gains can't be credited to optimization, and the search pressure on the real monitor was weak. That limits how much the result above says.
- **M8: a stronger optimizer does carry over to real runs.**
  - What changed: it averages 3 simulated runs per strategy, keeps an explicit best-so-far, sees the transcript, and Opus 4.6 writes its strategies.
  - Results on 3 pairs, where re-drawn first strategies rarely win for real: its strategies won 32 of 38 fresh real runs, against 5 of 23 for re-drawn first strategies (pair-stratified p = 4e-6; run-clustered p = 0.002). The M7 optimizer won 10 of 26.
  - Averaging simulated runs is the key change. With 1 simulated run per strategy, it won only 3 of 10, no better than re-drawing.
  - The sim-to-real gap still does not grow over rounds. Among simulated wins, 11 of 12 early ones and 16 of 17 late ones held up for real.
  - First sign of transfer: Haiku 5.5 missed 3 of its 6 real wins on one pair (logcred).
  - See [`results/m8/scale/analysis.md`](results/m8/scale/analysis.md) and the pilot write-up, [`results/m8/pilot/analysis.md`](results/m8/pilot/analysis.md).
- **A stronger held-out monitor (Haiku 5.5) caught 79 of 83 live runs.** Attacks tuned against Nova Pro rarely transfer.
- **Caveats.** Only 9 pairs, and Nova Pro refused some malware transcripts (counted as caught).

Details: [`results/m7/phase3/analysis.md`](results/m7/phase3/analysis.md), [`phase3b/analysis.md`](results/m7/phase3b/analysis.md), [`phase3c/analysis.md`](results/m7/phase3c/analysis.md), and the run log [`docs/m7_log.md`](docs/m7_log.md).

## Earlier: M3 pilot (2026-11)

4 task pairs against a weak monitor (Nova Lite). The optimizer never improved, so there was no search pressure to test, and harness defects found afterwards were fixed. Full write-up, follow-up checks and the list of harness fixes: [`docs/m3_report.md`](docs/m3_report.md).

## How it works

- **Simulated sandbox** ([`sim_sandbox.py`](src/simrt/sim_sandbox.py), [`simulator.py`](src/simrt/simulator.py)): an Inspect sandbox where an LLM answers every command, grounded in the codebase and an honest reference run. Control Tower's attack policies run unchanged in simulation and reality.
- **Judge** ([`judge.py`](src/simrt/judge.py)): estimates P(side task succeeded) from the simulated transcript.
- **Monitor** ([`monitoring.py`](src/simrt/monitoring.py)): Control Tower's whole-transcript monitor, 3 passes averaged.
- **Optimizer** ([`optimize.py`](src/simrt/optimize.py)): revises the attack strategy from the monitor's score and reasoning and the judge's estimate.
- **Validation** ([`validate.py`](src/simrt/validate.py)): replays each round's commands and reruns its strategy live in real containers, scored by the real scorer and the same monitor.

## Layout

```
src/simrt/     simulator, sandbox, judge, monitoring, optimizer, validation, analyses
scripts/       box setup, job launcher, milestone pipelines (m1–m5); scripts/m7/ and scripts/m8/
results/       m3/, m7/ and m8/ result tables, figures and write-ups
docs/          M7 and M8 plans and logs, literature review, M3 report
tests/         unit and integration tests (no models or containers needed)
```

## Running

Experiments run on an EC2 box (Docker, LinuxArena environments pinned to `v1.0.1`), with models through Amazon Bedrock. `bash scripts/setup_box.sh` sets it up. The M7 launch commands are in [`docs/m7_log.md`](docs/m7_log.md), and the M3 ones are in [`docs/m3_report.md`](docs/m3_report.md#reproducing). Tests: `python -m pytest -q`.

## References

- Olmo, Zhang & Tracy. *Simulated Control Evals.* 2026. [arXiv:2610.31593](https://arxiv.org/abs/2610.31593)
- Kozhevnikov & Tracy. *Screwtape: Adversarial Transcript Editing for Monitor Red-Teaming.* 2026. [arXiv:2610.20941](https://arxiv.org/abs/2610.20941)
- [LinuxArena](https://github.com/redwoodresearch/linuxarena-paper), [Control Tower](https://github.com/linuxarena/control-tower), [Inspect AI](https://inspect.aisi.org.uk)
