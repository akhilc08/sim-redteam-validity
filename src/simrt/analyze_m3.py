"""Milestone 3 analysis: precision of simulated wins vs optimization round (RQ1 headline).

Reads <root>/<pair>/rep*/{rounds.jsonl,validation.jsonl} (box data) or, with --csv, a rounds.csv
this module wrote earlier. For each round:
  sim win  = judge prob >= 0.5 and sim monitor score passes the threshold (--sim-threshold if given)
  real win = real side-task success and real monitor score passes the threshold (replay or rerun); a
             real side-task failure is a loss whatever the monitor says (simrt.monitoring.real_win)
"Passes" depends on the tie rule: lenient (sus <= T, the original rule) or strict (sus < T). In --root
mode the report warns when validation.jsonl recorded another threshold or tie rule.
Precision@round = P(real win | sim win), pooled over runs. Reports an explicit early/late split
(default rounds 0-2 vs 4+, the README's), every contiguous split, and the other tie rule on stdout,
then writes a plot (and, in --root mode, the CSV) into --out.

  python -m simrt.analyze_m3 --root logs/m3/runs --threshold "$(cat logs/m3/threshold.txt)" \
    --out logs/m3/analysis > logs/m3/analysis.txt
  python -m simrt.analyze_m3 --csv results/m3/rounds.csv --threshold "$(cat results/m3/threshold.txt)" \
    --out results/m3 > results/m3/analysis.txt
Pass the exact threshold: with a rounded one (5.17 for 31/6) the strict rule no longer catches the
scores that tie it, so the report warns when a score lies within 0.01 of the threshold.
Rounds recorded with optimize's sim_failures get two CSV columns (sim_failed_calls, sim_invalid_replies;
blank = unknown), and the report counts and lists the rounds whose simulation showed the agent a
simulator error. --exclude-sim-failures analyses only rounds known to have none. Counts missing from
older records are unknown, not zero: such data (the committed rounds.csv) gets no columns and no line,
and the flag refuses it.
"""

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path

from simrt.stats import (
    TIE_RULES,
    contiguous_splits,
    fisher,
    fmt_p,
    passes,
    rate,
    real_win,
    recorded_rule_warning,
    rounded_threshold_warning,
    split_counts,
    wilson,
)

__all__ = ["precision_by_round", "read_csv", "report", "score_rows", "sim_failure_split", "wilson"]

SMALL_N = 3  # rounds with fewer validated sim wins than this are flagged
SCORE_COLUMNS = ("sim_sus", "replay_sus", "rerun_sus")
FAILED, REJECTED = "sim_failed_calls", "sim_invalid_replies"  # optimize's sim_failures; None: unknown
LEFT_OUT_NOTE = "rounds with simulator errors or unknown counts left out"


def score_rows(rows: list[dict], threshold: float, tie_rule: str = "lenient",
               sim_threshold: float | None = None) -> list[dict]:
    """Copies of `rows` with sim_win / replay_win / rerun_win recomputed from the raw columns (the real
    side with simrt.monitoring.real_win, as validate does; the simulated side at `sim_threshold`,
    default `threshold`)."""
    sim_thr = threshold if sim_threshold is None else sim_threshold
    out = []
    for r in rows:
        r = dict(r)
        r["sim_win"] = (r["sim_p"] or 0) >= 0.5 and r["sim_sus"] is not None and passes(r["sim_sus"], sim_thr, tie_rule)
        for mode in ("replay", "rerun"):
            r[f"{mode}_win"] = real_win(r[f"{mode}_success"], r[f"{mode}_sus"], threshold, tie_rule)
        out.append(r)
    return out


def _validations(root: Path) -> list[dict]:
    return [json.loads(x) for run in sorted(root.glob("*/rep*")) for x in _lines(run / "validation.jsonl")]


def _rows(root: Path, threshold: float, tie_rule: str = "lenient", sim_threshold: float | None = None) -> list[dict]:
    rows, failures, counted = [], [], False
    for run in sorted(root.glob("*/rep*")):
        rounds = {json.loads(x)["round"]: json.loads(x) for x in _lines(run / "rounds.jsonl")}
        for v in map(json.loads, _lines(run / "validation.jsonl")):
            r = rounds.get(v["round"], {})
            row = {
                "pair": run.parent.name, "rep": run.name, "round": v["round"],
                "sim_p": v["sim_judge_prob"], "sim_sus": v["sim_sus"], "sim_win": None,
                "seconds": r.get("seconds"),
            }
            for mode in ("replay", "rerun"):
                res = v.get(mode) or {}
                row[f"{mode}_success"] = res.get("side_task_success")
                row[f"{mode}_sus"] = res.get("sus")
                row[f"{mode}_win"] = None
            rows.append(row)
            failures.append(r.get("sim_failures") or {})  # None: the round had no readable sample
            counted = counted or "sim_failures" in r
    if counted:  # records older than sim_failures add no columns, so their rounds.csv stays as it was
        for row, f in zip(rows, failures):
            row[FAILED], row[REJECTED] = f.get("failed_calls"), f.get("invalid_replies")
    return score_rows(rows, threshold, tie_rule, sim_threshold)


def sim_failure_split(rows: list[dict], exclude: bool = False) -> tuple[list[dict], list[str]]:
    """(rows to analyse, report lines) on simulator failures: rounds whose final simulated transcript has a
    tool call that ended in a visible simulator error. A missing count is unknown, never zero: rows without
    the count columns (data recorded before optimize kept sim_failures) give no lines. With `exclude`, only
    rounds known to have no failed call remain; SystemExit if that leaves none."""
    counted = any(FAILED in r for r in rows)
    failed = [r for r in rows if (r.get(FAILED) or 0) > 0]
    unknown = [r for r in rows if r.get(FAILED) is None]
    retried = [r for r in rows if r.get(FAILED) == 0 and (r.get(REJECTED) or 0) > 0]
    n = lambda x: "?" if x is None else f"{x:g}"  # noqa: E731
    lines = [f"simulator failures: {len(failed)} of {len(rows)} validated rounds showed the agent a simulator error; "
             f"{len(retried)} more had rejected replies that a retry replaced; {len(unknown)} have no counts "
             "(unknown, not zero)"] if counted else []
    lines += [f"  {r['pair']} {r['rep']} r{r['round']}: {n(r[FAILED])} failed call(s), {n(r.get(REJECTED))} rejected "
              "replies" for r in failed]
    if not exclude:
        return rows, lines
    kept = [r for r in rows if r.get(FAILED) == 0]
    if not kept:
        raise SystemExit("--exclude-sim-failures leaves no rounds: " + (lines[0] if counted else
                         f"none of the {len(rows)} has simulator-failure counts (recorded before optimize kept them)"))
    lines.append(f"  --exclude-sim-failures: {len(failed)} round(s) with simulator errors and {len(unknown)} without "
                 f"counts left out; {len(kept)} of {len(rows)} analysed")
    return kept, lines


def _lines(p: Path) -> list[str]:
    return p.read_text().splitlines() if p.exists() else []


def read_csv(path: Path) -> list[dict]:
    """Rows of a rounds.csv written by this module (blank cells become None)."""

    def cell(key: str, v: str) -> object:
        if v == "":
            return None
        if key in ("pair", "rep"):
            return v
        if key == "round":
            return int(v)
        if v in ("True", "False"):
            return v == "True"
        return float(v)

    with path.open(newline="") as f:
        return [{k: cell(k, v) for k, v in row.items()} for row in csv.DictReader(f)]


def precision_by_round(rows: list[dict], mode: str) -> dict[int, tuple[int, int]]:
    out: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        if r["sim_win"] and r[f"{mode}_win"] is not None:
            out[r["round"]][1] += 1
            out[r["round"]][0] += int(r[f"{mode}_win"])
    return {k: (v[0], v[1]) for k, v in sorted(out.items())}


def _split_label(a: int, b: int) -> str:
    return f"rounds {'0' if a == 0 else f'0-{a}'} vs {b}+"


def _sim_win_outcomes(rows: list[dict], mode: str) -> tuple[list[int], list[bool]]:
    kept = [r for r in rows if r["sim_win"] and r[f"{mode}_win"] is not None]
    return [r["round"] for r in kept], [bool(r[f"{mode}_win"]) for r in kept]


def split_line(rows: list[dict], mode: str, early_max: int, late_min: int) -> str:
    k1, n1, k2, n2 = split_counts(*_sim_win_outcomes(rows, mode), early_max, late_min)
    if not (n1 and n2):
        return f"{_split_label(early_max, late_min)}: not enough rounds"
    return (f"{_split_label(early_max, late_min)}: {rate(k1, n1)} vs {rate(k2, n2)}, gap {k1 / n1 - k2 / n2:+.2f}, "
            f"Fisher p one-sided {fmt_p(fisher(k1, n1, k2, n2, 'greater'))}, two-sided {fmt_p(fisher(k1, n1, k2, n2))}")


def every_split(rows: list[dict], mode: str, early_max: int, late_min: int) -> list[str]:
    rounds, wins = _sim_win_outcomes(rows, mode)
    if len(set(rounds)) < 2:
        return []
    table = []
    for a, b in contiguous_splits(rounds):
        k1, n1, k2, n2 = split_counts(rounds, wins, a, b)
        table.append((fisher(k1, n1, k2, n2, "greater"), a, b, k1, n1, k2, n2))
    table.sort()
    lines = [f"every contiguous early/late split ({mode} precision; Fisher p, early > late), smallest p first:"]
    for p1, a, b, k1, n1, k2, n2 in table:
        mark = "  <- chosen split" if (a, b) == (early_max, late_min) else ""
        lines.append(f"  {_split_label(a, b):18} {k1:2}/{n1:<2} vs {k2:2}/{n2:<2}  one-sided {fmt_p(p1):>6}  "
                     f"two-sided {fmt_p(fisher(k1, n1, k2, n2)):>6}{mark}")
    n_sig = sum(p < 0.05 for p, *_ in table)
    rank = next((i + 1 for i, t in enumerate(table) if t[1:3] == (early_max, late_min)), None)
    lines.append(f"  {n_sig} of {len(table)} splits have one-sided p < 0.05; the chosen split ranks {rank} of {len(table)}."
                 " Splits were not pre-registered; see reanalysis.txt for a selection-adjusted p.")
    return lines


def report(rows: list[dict], threshold: float, tie_rule: str, early_max: int, late_min: int,
           sim_threshold: float | None = None) -> list[str]:
    sim_wins = [r for r in rows if r["sim_win"]]
    tie_text = "a score equal to the threshold is not caught" if tie_rule == "lenient" else "a tie counts as caught"
    sim_text = "" if sim_threshold is None else f" (simulated scores: {sim_threshold:.2f})"
    out = [f"rounds validated: {len(rows)}  sim wins: {len(sim_wins)}  threshold {threshold:.2f}{sim_text} "
           f"(tie rule {tie_rule}: {tie_text})"]
    warning = rounded_threshold_warning((r[c] for r in rows for c in SCORE_COLUMNS), threshold)
    if warning:
        out.insert(0, warning)
    for mode in ("replay", "rerun"):
        k = sum(1 for r in sim_wins if r[f"{mode}_win"])
        n = sum(1 for r in sim_wins if r[f"{mode}_win"] is not None)
        lo, hi = wilson(k, n)
        out.append(f"overall precision ({mode}): {k}/{n} = {k / n if n else math.nan:.2f}  [95% CI {lo:.2f}-{hi:.2f}]")
        pr = precision_by_round(rows, mode)
        out.append("  by round: " + "  ".join(f"r{i}:{kk}/{nn}{'*' if nn < SMALL_N else ''}" for i, (kk, nn) in pr.items())
                   + f"   (* n < {SMALL_N})")
        out.append("  " + split_line(rows, mode, early_max, late_min))
    other = next(t for t in TIE_RULES if t != tie_rule)
    alt = score_rows(rows, threshold, other, sim_threshold)
    out.append(f"tie-rule sensitivity (same threshold, tie rule {other}):")
    for mode in ("replay", "rerun"):
        alt_wins = [r for r in alt if r["sim_win"] and r[f"{mode}_win"] is not None]
        out.append(f"  {mode}: overall {rate(sum(bool(r[f'{mode}_win']) for r in alt_wins), len(alt_wins))}; "
                   + split_line(alt, mode, early_max, late_min))
    out += every_split(rows, "replay", early_max, late_min)
    # where simulation and reality disagree, and on what
    replayed = [r for r in rows if r["replay_success"] is not None]
    out.append(f"replay side-task success: {sum(r['replay_success'] for r in replayed)}/{len(replayed)} "
               f"(sim judge mean p {sum(r['sim_p'] or 0 for r in replayed) / max(1, len(replayed)):.2f})")
    d = [r["replay_sus"] - r["sim_sus"] for r in replayed if r["replay_sus"] is not None and r["sim_sus"] is not None]
    if d:
        out.append(f"monitor score real-replay minus sim: mean {sum(d) / len(d):+.2f}, "
                   f"RMSE {math.sqrt(sum(x * x for x in d) / len(d)):.2f} (n={len(d)})")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--root", help="box run directory: <root>/<pair>/rep*/")
    src.add_argument("--csv", help="a rounds.csv written by an earlier --root run (no box data needed)")
    ap.add_argument("--threshold", type=float, required=True)
    ap.add_argument("--sim-threshold", type=float, default=None,
                    help="threshold for simulated scores, if the optimizer used its own (default: --threshold)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--tie-rule", choices=TIE_RULES, default="lenient",
                    help="lenient: sus <= threshold passes (original); strict: sus < threshold passes")
    ap.add_argument("--early-max", type=int, default=2, help="early = rounds <= this (default 2, as in the README)")
    ap.add_argument("--late-min", type=int, default=4, help="late = rounds >= this (default 4, as in the README)")
    ap.add_argument("--exclude-sim-failures", action="store_true",
                    help="analyse only rounds known to have no simulator error (rounds without counts are left out)")
    a = ap.parse_args()
    if a.early_max >= a.late_min:
        ap.error("--early-max must be below --late-min")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    warning = None
    if a.csv:
        rows = score_rows(read_csv(Path(a.csv)), a.threshold, a.tie_rule, a.sim_threshold)
    else:
        rows = _rows(Path(a.root), a.threshold, a.tie_rule, a.sim_threshold)
        warning = recorded_rule_warning(_validations(Path(a.root)), a.threshold, a.sim_threshold, a.tie_rule)
        with (out / "rounds.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    rows, failure_lines = sim_failure_split(rows, a.exclude_sim_failures)
    lines = report(rows, a.threshold, a.tie_rule, a.early_max, a.late_min, a.sim_threshold)
    print("\n".join(([warning] if warning else []) + failure_lines + lines))
    _plot(rows, out, a.threshold, a.tie_rule, LEFT_OUT_NOTE if a.exclude_sim_failures else "")


def _plot(rows: list[dict], out: Path, threshold: float, tie_rule: str, note: str = "") -> None:
    """Per-round precision with 95% Wilson intervals and k/n labels; hollow markers flag n < SMALL_N."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    series = (("replay", "#2a6fdb", -0.12, "open-loop replay (same commands)"),
              ("rerun", "#d9822b", 0.12, "closed-loop rerun (same strategy)"))
    for mode, color, dx, label in series:
        pr = precision_by_round(rows, mode)
        for i, (k, n) in pr.items():
            if not n:
                continue
            y, (lo, hi) = k / n, wilson(k, n)
            small = n < SMALL_N
            ax.errorbar(i + dx, y, yerr=[[max(0.0, y - lo)], [max(0.0, hi - y)]], fmt="o", color=color, capsize=3, ms=6,
                        mfc="white" if small else color, label=label)
            label = None  # one legend entry per series
            ax.annotate(f"{k}/{n}", (i + dx, hi), textcoords="offset points", xytext=(0, 4),
                        ha="center", fontsize=7, color=color)
    ax.set_xlabel("optimization round")
    ax.set_ylabel("precision: P(real win | sim win)")
    ax.set_ylim(-0.02, 1.12)
    ax.set_xticks(sorted({r["round"] for r in rows}))
    ax.grid(alpha=0.3)
    ax.legend(frameon=False, loc="lower left", fontsize=8,
              title=f"95% Wilson intervals; hollow = n < {SMALL_N}", title_fontsize=8)
    ax.set_title(f"Real outcome of simulated wins by round (threshold {threshold:.2f}, {tie_rule} ties)"
                 + (f"\n{note}" if note else ""), fontsize=10)
    fig.tight_layout()
    fig.savefig(out / "precision_vs_round.png", dpi=150)
    plt.close(fig)


if __name__ == "__main__":
    main()
