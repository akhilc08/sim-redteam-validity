# Overnight autoresearch (started 2026-11-22 ~05:00 UTC)

The user's brief: "look at what previous redwood research does and pick prompts / attacks based on that, if the model is still too strong change model ... come up with good new research that shows our hypothesis or at the very least gives me good work to present to redwood, put all of this work in a new autoresearch folder".

This folder is tracked on main (the user asked for the work to be pushed to main). The main session commits it; agents do not.

## Hypothesis
Optimizing attacks against a monitor inside an LLM simulator (SCE-style) finds "wins" that partly exploit simulator errors. So simulated wins transfer to real execution less often than unoptimized ones do, and the sim–real gap grows with optimization pressure. SCE (Olmo, Zhang & Tracy, arXiv 2610.31593) validated simulation only on aggregates of one-shot attacks and calls iterated optimization "the central next step".

## Where things stand (see ../README.md and memory)
- **Harness:** fixed and verified on 2026-11-21 (main at 3749927+), deployed on the box at ~/work/simrt.
- **M3 (the old harness):** its results are a pilot. Under the fixed monitor rule, 26 of 167 verdicts flip. Live reruns show simulated losses win for real as often as simulated wins (7/7 vs 32/39, excluding reverse shell). The judge is near perfect on real transcripts (AUC 0.997), but at chance on M3's simulated ones.
- **The blocker, headroom:** every Claude monitor we tried catches every real attack.
  - Haiku 5.5: 153/153 caught.
  - Haiku 4.5: 49/49 round-0 attacks caught.
  - Sonnet 4.6: everything it scored.
  - Nova Lite only looked beatable because it was buggy and noisy.
  - Two likely causes: Control Tower's mean-quick monitor is GIVEN the side-task goals (provide_goals=true, the default), and the strategy_attack policy is not stealthy.
- **Models:**
  - Attacker, simulator and judge: Sonnet 4.6 on Bedrock (box_env.sh SONNET_BEDROCK).
  - Available monitors: Haiku 5.5 via the Anthropic API (key in ~/work/.env.monitor, used ONLY via `SIMRT_KEY_FILE=~/work/.env.monitor; box_init; with_key`); Haiku 4.5, Sonnet 4.6 and Opus 4.6 on Bedrock; Nova Micro/Lite/Pro; gpt-oss-20b/120b on Bedrock.
  - Not usable: Sonnet 5.5 refuses monitor prompts on its cyber classifier; no Claude 5.x or GPT-5/6 on Bedrock.
- **Pool:** 10 environments downloaded except minio (model_registry and media_processing are blocked). The new environments have no honest runs. Data is in ~/work/logs/m6/{rescore,pool,haiku45}.

## Phases (each run by a background agent; handoff notes go in NOTES.md)
1. **Literature and calibration:** what Redwood/LinuxArena/BashArena/Ctrl-Z/SCE use for monitors (models, prompts, whether goals are given), attack policies, audit budgets and typical catch rates. Then cheap offline rescoring of existing transcripts to find a monitor config with headroom.
2. **Round-0 calibration:** run new real round-0 attacks with the chosen attack policy (stealthier, Redwood-style) on 3–4 pairs, scored with the chosen monitor, until the round-0 real win rate is about 10–40%.
3. **Main experiment:** optimization in simulation against the chosen monitor; every round validated in real (replay + live rerun); the sim–real gap measured against optimization pressure; plus a matched best-of-n control if the budget allows.
4. **Write-up:** report.md with figures, tables and exact reproduction commands, framed for Redwood (complements SCE: per-trajectory verdicts under optimization).

## Budgets for the whole night (hard caps)
- Bedrock: $300.
- Anthropic API: $25 (monitors only).
- Box: stop it when finished.
- Log spend in NOTES.md after each phase.

## Rules
- Run everything heavy on the box (ssh helpers: /private/tmp/claude-501/-Users-sickle-Coding-sim-redteam-validity/b75be0f5-31d1-4894-a84d-d90d5aa69633/scratchpad/box/{box.sh,box_put.sh,box_get.sh}). The Mac is the user's laptop.
- Never open, print or copy .env files.
- Real runs are Bedrock-only (`env SIMRT_KEY_FILE=/nonexistent ANTHROPIC_API_KEY=`).
- Don't modify ~/work/logs/m1, m3 or m5, or the repo's committed results.
- New box outputs go to ~/work/logs/ar/<phase>/.
- Experimental code lives in this folder (scripts/m7/) and imports simrt; it does not edit src/.
