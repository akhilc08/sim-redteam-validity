# M8 pilot: a stronger optimizer on the visit pair

**Question.** In M7 Phase 3c the optimizer beat re-sampling its first strategy only in simulation, not in real runs. Does a stronger optimizer win more often in real runs? It differs in four ways:
- it averages 3 simulated rollouts per strategy;
- it keeps an explicit incumbent;
- it sees the monitor's full reasoning and the most suspicious simulated transcript;
- its strategies are written by Opus 4.6.

**Short answer (pilot: 1 pair, 2 runs, 9 strategies in rounds 1+).**
- **Live real wins:** 7/9 (0.78 [0.45, 0.94]). That compares with 2/9 for re-sampled first strategies (0.22, p = 0.057) and 9/18 for the M7 optimizer (0.50, p = 0.23).
- **Replay real wins:** 8/9, against 1/9 (p = 0.003) and 8/18 (p = 0.042).
- **The simulated gains carry over to real runs.** Relative to re-sampling, the simulated monitor score fell by 3.1 and the live score by 2.3. For the M7 optimizer the falls were 1.7 and 1.2.
- **It meets the pre-registered scale-up rule.** Its live win rate is at least v1's and above re-sampling's, and its live drop is at least v1's.
- **No transfer to the held-out monitor.** Haiku 5.5 still caught all 9 live reruns.
- **This is a pilot.** n is small, all 9 strategies come from 2 runs on one pair, and four things changed at once.

Tables: `tables.md`, `summary.json`, `fig_m8_pilot.png` (from `scripts/m8/m8_analyze.py`). Design, pre-registration and run log: `docs/m8_log.md`. Brackets are Wilson 95% intervals; Fisher tests are two-sided.

## Setup

- **Pair.** `web_scraping` `allowlist_config` / `visit_malicious_website`. It is the only M7 pair with real headroom: re-sampled first strategies won live only 2/9 there.
- **Settings as in M7 Phase 3.**
  - Monitor: Nova Pro `mean-quick`, attacker goal hidden, 3 passes, threshold 4.00, lenient ties.
  - Attacker, simulator and judge: Sonnet 4.6 on Bedrock.
  - Patience 3, up to 6 rounds, 2 independent runs.
- **New options:** `--rollouts 3 --incumbent --feedback transcript --optimizer-model` Opus 4.6 (Bedrock).
- **Validation.** Every round was validated: a replay of rollout 0 plus one live rerun of the strategy. All 11 rounds were validated, with no unknown outcomes.
- **Comparison arms** (both from M7, same pair, same validation):
  - *resample*: 9 re-sampled round-0 draws.
  - *v1*: the M7 optimizer's 18 rounds 1+.
- **v2's round 0** was written by Opus with no feedback, so it is reported separately (live 1/2) and is not in any test.
- **Rounds:** run 1 stopped early after 5; run 2 ran all 6.

## Results

| arm | live real win | replay real win | sim win (per strategy) | mean sim sus | mean live sus |
|---|---|---|---|---|---|
| **v2 (M8, rounds 1+)** | **7/9 0.78 [0.45, 0.94]** | **8/9 0.89 [0.56, 0.98]** | 8/9 0.89 | 2.67 | 3.54 |
| v1 (M7 optimizer, rounds 1+) | 9/18 0.50 [0.29, 0.71] | 8/18 0.44 [0.25, 0.66] | 9/18 0.50 | 4.06 | 4.62 |
| resample (first strategy) | 2/9 0.22 [0.06, 0.55] | 1/9 0.11 [0.02, 0.44] | 1/9 0.11 | 5.80 | 5.83 |

- **Tests, v2 vs resample:** live p = 0.057, replay p = 0.003, sim p = 0.003.
- **Tests, v2 vs v1:** live p = 0.23, replay p = 0.042, sim p = 0.091.
- **Per rollout,** 23 of v2's 27 rollouts were simulated wins (0.85).
- **Sim-to-live gap.** v2's live score sits 0.87 [0.27, 1.33] above its simulated score, against 0.56 for v1. The difference, 0.31 [-0.59, 1.17], is not significant.
  - So the stronger optimizer does not show a clearly larger sim-to-real gap.
  - The gap is still positive, which fits mild selection on simulated noise.
- **Finding a live win.** Both v2 runs had a live real win by their third attempt. Re-sampling expects 0.58 by the third draw.
- **Held-out Haiku 5.5** caught 9/9 v2 live reruns (scores 6.7-7.7), as it did for both M7 arms.

## Verdict

- The pre-registered scale-up rule is met:
  - live win rate 0.78, which is at least 0.50 and above 0.22;
  - live drop vs resample 2.3, which is at least v1's 1.2;
  - cost per round fits.
- Unlike the M7 optimizer, this one's simulated improvement mostly carries over to real runs. On replay it is significantly better than both the M7 optimizer and re-sampling.
- The evidence is still pilot-sized. The live-rerun comparisons are not significant at this n.

## Caveats

- **Small, clustered sample.** There are 9 strategies from 2 runs on one pair. Strategies within a run are refinements of each other, so they are not independent, and the p-values treat them as if they were.
- **Four changes at once,** plus 3x the simulated rollouts per round. The pilot cannot say which change helped.
- **Runaway rollouts.** In round 0, one rollout in each run hit the 300-message limit. The attacker spent about 148 tool calls debugging simulated outputs that contradicted each other. These cost $14.26, about 28% of the Bedrock spend. M7 had none in 103 rounds. Whether the live reruns of those strategies also ran long was not checked.
- **Thresholds and monitor as in M7.** Haiku 5.5 is at its ceiling, so transfer to a stronger monitor is still unmeasured.
- **Spend (token-based).**
  - Bedrock $51.67: Sonnet agents $45.47 (including the $14.26 runaways), Nova Pro $5.06, Opus optimizer $1.14.
  - Anthropic held-out $0.20.

## Next

- Scale up with a pre-registered design:
  - more runs on visit;
  - pairs where re-sampled first strategies rarely win live;
  - a cap on runaway rollouts (e.g. a lower message limit applied to all arms);
  - ideally an ablation of the four changes.
- Then move to a stronger target monitor, where first strategies win less often and transfer can be measured.
