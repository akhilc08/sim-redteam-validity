"""M7 Phase 3c: does the optimizer beat re-sampling its round-0 (no-feedback) strategy the same number of times?
Pre-registered in docs/m7_log.md ("Phase 3c run"). Offline only. Reuses p3_analyze.load_root.

Optimizer arm = rounds 1+ of the optimizer runs (--opt-root, repeatable). Resampling arm = every round-0 draw:
the Phase 3c runs (--draws-root, simrt.optimize --rounds 1 into fresh rep dirs) plus round 0 of each optimizer run
of the same pair. Only pairs present under --draws-root are analysed. Live rerun primary, replay secondary.
  1. real win rate, optimizer rounds 1+ vs resampling: Fisher (pooled), pair-stratified CMH + MH OR + exact.
  2. sim win rate: same.
  3. best-so-far objective (lower better) at round k vs the expected best of k+1 resampled draws (exact mean of the
     minimum over all (k+1)-subsets of the pair's draws), and "any win so far" (1 - C(n-w,m)/C(n,m)); pooled over
     runs as optimizer minus resampling, pair-stratified bootstrap CI.

  source ~/work/simrt/scripts/box_env.sh; $PY ~/work/ar/code/p3c_analyze.py      # defaults: phase3 + phase3b vs 3c
"""

import argparse
import json
import math
from collections import defaultdict
from math import comb
from pathlib import Path

import numpy as np
from scipy.stats import chi2

from p3_analyze import MODES, cell, clean, fisher2, fp, load_root, md, prop, short
from p3_strat import _exact_stratified

AR = Path.home() / "work/logs/ar"


def strat_test(by_pair: dict[str, tuple[dict, dict]]) -> dict:
    """by_pair: pair -> (optimizer prop, resampling prop). CMH (cc) + MH OR (optimizer vs resampling) + exact."""
    tabs = [(o["k"], o["n"] - o["k"], r["k"], r["n"] - r["k"]) for o, r in by_pair.values() if o["n"] and r["n"]]
    num = sum(a * d / (a + b + c + d) for a, b, c, d in tabs)
    den = sum(b * c / (a + b + c + d) for a, b, c, d in tabs)
    dev = sum(a - (a + b) * (a + c) / (a + b + c + d) for a, b, c, d in tabs)
    var = sum((a + b) * (c + d) * (a + c) * (b + d) / ((a + b + c + d) ** 2 * (a + b + c + d - 1))
              for a, b, c, d in tabs if a + b + c + d > 1)
    out = {"n_strata": len(tabs), "mh_odds_ratio_opt_vs_resample": num / den if den > 0 else (math.inf if num else None),
           "obs_minus_exp_opt_wins": dev}
    if var > 0:
        stat = max(abs(dev) - 0.5, 0) ** 2 / var
        out.update(cmh_chi2_cc=stat, cmh_p=float(chi2.sf(stat, 1)))
    else:
        out["cmh_status"] = "not estimable: no stratum has both outcomes"
    out["exact"] = _exact_stratified(tabs) if tabs else {"status": "no strata"}
    return out


def contrast(opt: list[dict], res: list[dict], pairs: list[str], outcome, known) -> dict:
    """Optimizer vs resampling win rate per pair, pooled (Fisher) and stratified."""
    per = {p: (prop(sum(bool(outcome(r)) for r in opt if r["pair"] == p and known(r)),
                    sum(1 for r in opt if r["pair"] == p and known(r))),
               prop(sum(bool(outcome(r)) for r in res if r["pair"] == p and known(r)),
                    sum(1 for r in res if r["pair"] == p and known(r)))) for p in pairs}
    o = prop(sum(v[0]["k"] for v in per.values()), sum(v[0]["n"] for v in per.values()))
    r = prop(sum(v[1]["k"] for v in per.values()), sum(v[1]["n"] for v in per.values()))
    return {"per_pair": {p: {"optimizer": a, "resample": b} for p, (a, b) in per.items()},
            "pooled": {"optimizer": o, "resample": r, "fisher": fisher2(o, r)}, "stratified": strat_test(per)}


def exp_min(xs: list[float], m: int) -> float | None:
    """Mean over all m-subsets of the minimum (order statistics)."""
    x, n = sorted(xs), len(xs)
    if m < 1 or m > n:
        return None
    return sum(x[i] * comb(n - 1 - i, m - 1) for i in range(n - m + 1)) / comb(n, m)


def p_any(n: int, w: int, m: int) -> float | None:
    return None if m < 1 or m > n else 1 - comb(n - w, m) / comb(n, m)


def curves(runs: dict, pool: dict, kmax: int, kind: str, win=None) -> dict:
    """kind 'best': objective; 'any': indicator via win(row) -> bool|None (None = unknown, counted as no win)."""
    out = {}
    for k in range(kmax + 1):
        ent = {"reached": [], "carry": []}
        for run, rs in runs.items():
            pair, L = rs[0]["pair"], len(rs)
            dr = pool[pair]
            if kind == "best":
                fin = [r["objective"] for r in dr if math.isfinite(r["objective"])]
                e = exp_min(fin, k + 1)
                seq = [r["objective"] for r in rs]
                val = lambda j: min(seq[: j + 1])  # noqa: E731
            else:
                kn = [win(r) for r in dr if win(r) is not None]
                e = p_any(len(kn), sum(kn), k + 1)
                val = lambda j: float(any(bool(win(r)) for r in rs[: j + 1]))  # noqa: E731
            if e is None:
                continue
            for name, j in (("reached", k if k < L else None), ("carry", min(k, L - 1))):
                if j is not None and math.isfinite(val(j)):
                    ent[name].append((pair, val(j), e))
        out[k] = ent
    return out


def summarize(ent: list[tuple], lower_better: bool = False) -> dict:
    """diff = optimizer - resampling; 'better' is lower for the objective, higher for P(any win)."""
    if not ent:
        return {"n_runs": 0}
    o, e = np.array([x[1] for x in ent]), np.array([x[2] for x in ent])
    d = (o - e) * (-1 if lower_better else 1)  # > 0: optimizer better
    return {"n_runs": len(ent), "optimizer_mean": float(o.mean()), "resample_expected": float(e.mean()),
            "diff": float((o - e).mean()), "n_opt_better": int((d > 1e-12).sum()), "n_opt_worse": int((d < -1e-12).sum())}


def bootstrap(runs: dict, pool: dict, k: int, kind: str, win, B: int, rng, own_length=False) -> list[float] | None:
    """Pair-stratified bootstrap of the pooled mean (optimizer - expected resampling) at k (or at each run's length)."""
    by = defaultdict(list)
    for rs in runs.values():
        by[rs[0]["pair"]].append(rs)
    diffs = []
    for _ in range(B):
        tot = []
        for pair, rl in by.items():
            dr = pool[pair]
            pick = [rl[i] for i in rng.integers(0, len(rl), len(rl))]
            dsel = [dr[i] for i in rng.integers(0, len(dr), len(dr))]
            for rs in pick:
                j = len(rs) - 1 if own_length else min(k, len(rs) - 1)
                if kind == "best":
                    fin = [r["objective"] for r in dsel if math.isfinite(r["objective"])]
                    e, v = exp_min(fin, j + 1), min(r["objective"] for r in rs[: j + 1])
                else:
                    kn = [win(r) for r in dsel if win(r) is not None]
                    e, v = p_any(len(kn), sum(kn), j + 1), float(any(bool(win(r)) for r in rs[: j + 1]))
                if e is not None and math.isfinite(v):
                    tot.append(v - e)
        if tot:
            diffs.append(float(np.mean(tot)))
    return [float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))] if diffs else None


def own_length(runs: dict, pool: dict, kind: str, win=None) -> dict:
    ent = []
    for rs in runs.values():
        c = curves({0: rs}, pool, len(rs) - 1, kind, win)[len(rs) - 1]["carry"]
        ent += c
    return summarize(ent, kind == "best")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--opt-root", action="append", default=None)
    ap.add_argument("--draws-root", default=str(AR / "phase3c/runs"))
    ap.add_argument("--out", default=str(AR / "phase3c/analysis"))
    ap.add_argument("--threshold", type=float, default=4.0)
    ap.add_argument("--heldout-threshold", type=float, default=5.0)
    ap.add_argument("--tie-rule", default="lenient")
    ap.add_argument("--kmax-primary", type=int, default=3)
    ap.add_argument("--boot", type=int, default=10000)
    a = ap.parse_args()
    opt_roots = [Path(x).expanduser() for x in (a.opt_root or [str(AR / "phase3/runs"), str(AR / "phase3b/runs")])]
    droot, out = Path(a.draws_root).expanduser(), Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    drows, notes = load_root(a, droot, droot.parent / "heldout", droot.parent.name)
    pairs = sorted({r["pair"] for r in drows})
    orows = []
    for root in opt_roots:
        rr, nn = load_root(a, root, root.parent / "heldout", root.parent.name)
        orows += [r for r in rr if r["pair"] in pairs]
        notes += nn
    errored = [f"{r['run']} round {r['round']}: {r['error'][:80]}" for r in drows + orows if r["error"]]
    drows_ok = [r for r in drows if not r["error"]]
    opt = [r for r in orows if r["round"] >= 1 and not r["error"]]
    res = drows_ok + [r for r in orows if r["round"] == 0 and not r["error"]]
    runs = defaultdict(list)
    for r in sorted(orows, key=lambda r: (r["run"], r["round"])):
        runs[r["run"]].append(r)
    pool = {p: [r for r in res if r["pair"] == p] for p in pairs}
    rng = np.random.default_rng(0)
    data = {p: {"new_draws": sum(r["pair"] == p for r in drows), "new_draws_ok": sum(r["pair"] == p for r in drows_ok),
                "new_validated": sum(r["pair"] == p and r["validated"] for r in drows_ok),
                "resample_arm": len(pool[p]), "optimizer_runs": len({r["run"] for r in orows if r["pair"] == p}),
                "optimizer_rounds_ge1": sum(r["pair"] == p for r in opt),
                "refused_sim_resample": sum(r["sim_refused"] for r in pool[p]),
                **{f"{m}_unknown_resample": sum(r[m]["rw"] is None for r in pool[p]) for m in MODES}} for p in pairs}
    S = {"config": {**vars(a), "opt_roots": [str(x) for x in opt_roots]}, "pairs": pairs, "data": data,
         "errored": errored, "notes": notes,
         "partial": any(not r["validated"] for r in drows_ok)}
    S["real_win"] = {m: contrast(opt, res, pairs, lambda r, m=m: r[m]["rw"], lambda r, m=m: r[m]["rw"] is not None)
                     for m in MODES}
    S["sim_win"] = contrast(opt, res, pairs, lambda r: r["sim_win"], lambda r: True)
    S["mean_objective"] = {p: {"optimizer": float(np.mean([r["objective"] for r in opt if r["pair"] == p])),
                               "resample": float(np.mean([r["objective"] for r in pool[p]]))} for p in pairs}
    kmax = max(len(v) for v in runs.values()) - 1
    wins = {"best": None, **{f"any_real_{m}": (lambda r, m=m: r[m]["rw"]) for m in MODES},
            "any_sim": lambda r: r["sim_win"]}
    S["curves"] = {}
    for name, w in wins.items():
        kind = "best" if name == "best" else "any"
        C = curves(runs, pool, kmax, kind, w)
        S["curves"][name] = {
            "pooled": {k: {v: summarize(C[k][v], kind == "best") for v in ("reached", "carry")} for k in C},
            "per_pair": {p: {k: {v: summarize([x for x in C[k][v] if x[0] == p], kind == "best")
                                 for v in ("reached", "carry")}
                             for k in C} for p in pairs},
            "boot_ci_carry": {k: bootstrap(runs, pool, k, kind, w, a.boot, rng) for k in range(a.kmax_primary + 1)},
            "own_length": own_length(runs, pool, kind, w),
            "own_length_boot_ci": bootstrap(runs, pool, 0, kind, w, a.boot, rng, own_length=True)}
    ho = defaultdict(lambda: [0, 0])
    for arm, rows in (("optimizer", opt), ("resample", res)):
        for r in rows:
            s = r["ho"]["rerun"][0]
            if s is not None:
                ho[arm][0] += s > a.heldout_threshold
                ho[arm][1] += 1
    S["heldout_rerun_caught"] = {k: prop(*v) for k, v in ho.items()}
    S = clean(S)
    S["figures"] = figures(S, pairs, out, a)
    (out / "summary.json").write_text(json.dumps(S, indent=1))
    (out / "tables.md").write_text(tables(S, a))
    print(f"{'PARTIAL: ' if S['partial'] else ''}{len(drows)} new draws, {len(opt)} optimizer rounds 1+, "
          f"{len(res)} resampled; wrote {out}")


def tables(S: dict, a) -> str:
    P = S["pairs"]
    L = [f"# Phase 3c: optimizer vs best-of-n resampling of round 0{' (PARTIAL DATA)' if S['partial'] else ''}", "",
         f"Threshold {a.threshold:g} (lenient ties). Optimizer arm = optimizer rounds 1+; resampling arm = new round-0 "
         "draws + round 0 of the optimizer runs. Cells: k/n rate [Wilson 95%].", "", "## Data", ""]
    L += md(["pair", *S["data"][P[0]]], [[short(p), *S["data"][p].values()] for p in P])
    L += [f"- errored (excluded): {e}" for e in S["errored"]] + [f"- {n}" for n in S["notes"]] + [""]

    def ctab(title: str, C: dict) -> list[str]:
        st, po = C["stratified"], C["pooled"]
        rows = [[short(p), cell(v["optimizer"]), cell(v["resample"]), ""] for p, v in C["per_pair"].items()]
        rows.append(["**pooled**", cell(po["optimizer"]), cell(po["resample"]),
                     f"Fisher {fp(po['fisher']['two_sided'])}"])
        ex = st["exact"]
        rows.append(["**stratified**", "", "", f"MH OR {fp(st['mh_odds_ratio_opt_vs_resample'])}, CMH p "
                     f"{fp(st.get('cmh_p'))}, exact p {fp(ex.get('p_two_sided'))} ({st['n_strata']} strata)"])
        return [f"## {title}", ""] + md(["pair", "optimizer rounds 1+", "resampled round 0", "test"], rows)
    L += ctab("1. Real win rate (primary: live rerun)", S["real_win"]["rerun"])
    L += ctab("1b. Real win rate (secondary: replay)", S["real_win"]["replay"])
    L += ctab("2. Sim win rate", S["sim_win"])
    L += md(["pair", "mean objective, optimizer rounds 1+", "mean objective, resampled"],
            [[short(p), f"{v['optimizer']:.2f}", f"{v['resample']:.2f}"] for p, v in S["mean_objective"].items()])
    names = {"best": "best-so-far objective (lower is better)", "any_real_rerun": "any real win so far (live rerun)",
             "any_real_replay": "any real win so far (replay)", "any_sim": "any sim win so far"}
    for name, title in names.items():
        C = S["curves"][name]
        L += [f"## 3. {title}: optimizer at round k vs resampling, best/any of k+1 draws", ""]
        f = lambda x: f"{x:.2f}"  # noqa: E731
        rows = []
        for k, v in C["pooled"].items():
            c, r = v["carry"], v["reached"]
            if not c.get("n_runs"):
                continue
            ci = C["boot_ci_carry"].get(k)
            rows.append([k, f"{f(c['optimizer_mean'])} / {f(c['resample_expected'])} / {c['diff']:+.2f}",
                         f"[{ci[0]:+.2f}, {ci[1]:+.2f}]" if ci else "-", f"{c['n_opt_better']}/{c['n_opt_worse']}/{c['n_runs']}",
                         f"{f(r['optimizer_mean'])} / {f(r['resample_expected'])} / {r['diff']:+.2f} (n={r['n_runs']})"
                         if r.get("n_runs") else "-"])
        L += md(["k", "pooled carry-forward: optimizer / resampling / diff", "bootstrap 95% CI of diff",
                 "runs opt better / worse / n", "reached-only: optimizer / resampling / diff"], rows)
        o, ci = C["own_length"], C["own_length_boot_ci"]
        if o.get("n_runs"):
            L += [f"At each run's own length (same number of attempts): optimizer {f(o['optimizer_mean'])}, resampling "
              f"{f(o['resample_expected'])}, diff {o['diff']:+.2f} {('[%+.2f, %+.2f]' % tuple(ci)) if ci else ''}; "
                  f"optimizer better in {o['n_opt_better']}, worse in {o['n_opt_worse']} of {o['n_runs']} runs "
                  "(runs longer than the pair's draw count are skipped).", ""]
        ks = sorted({int(k) for p in P for k, v in C["per_pair"][p].items() if v["carry"].get("n_runs")})
        L += md(["pair (carry: opt / resample)"] + [f"k={k}" for k in ks],
                [[short(p)] + [f"{f(v['optimizer_mean'])} / {f(v['resample_expected'])}" if v.get("n_runs") else ""
                               for k in ks for v in [C["per_pair"][p][str(k)]["carry"]]] for p in P])
    h = S["heldout_rerun_caught"]
    L += ["## Held-out Haiku 5.5 on live reruns (caught = sus > 5.00)", "",
          f"optimizer rounds 1+: {cell(h.get('optimizer'))}; resampled: {cell(h.get('resample'))}", ""]
    return "\n".join(L) + "\n"


def figures(S: dict, pairs: list[str], out: Path, a) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
                         "grid.color": "#e6e6e6", "font.size": 8})
    OPT, RES = "#2a78d6", "#eb6834"
    made = []
    for name, ylab, fn in (("best", "best objective so far (lower = better)", "fig_best_so_far_vs_resampling.png"),
                           ("any_real_rerun", "P(any real win so far), live rerun", "fig_any_real_win_vs_resampling.png")):
        C = S["curves"][name]
        groups = ["pooled"] + pairs
        fig, axs = plt.subplots(1, len(groups), figsize=(3.0 * len(groups), 3.0), sharey=True, squeeze=False)
        for ax, g in zip(axs[0], groups):
            d = C["pooled"] if g == "pooled" else C["per_pair"][g]
            ks = sorted(int(k) for k, v in d.items() if v["carry"].get("n_runs"))
            for key, ls, lab in (("carry", "-", "optimizer (carry-forward)"), ("reached", ":", "optimizer (runs reaching k)")):
                kk = [k for k in ks if d[str(k)][key].get("n_runs")]
                ax.plot([k + 1 for k in kk], [d[str(k)][key]["optimizer_mean"] for k in kk], ls, marker="o", ms=4,
                        color=OPT, lw=2, label=lab)
            ax.plot([k + 1 for k in ks], [d[str(k)]["carry"]["resample_expected"] for k in ks], "-", marker="s", ms=4,
                    color=RES, lw=2, label="best/any of m resampled round-0 draws")
            ax.set_title(short(g), fontweight="bold")
            ax.set_xlabel("attempts m (= round k + 1)")
        axs[0][0].set_ylabel(ylab)
        if name != "best":
            axs[0][0].set_ylim(-0.05, 1.05)
        fig.legend(*axs[0][0].get_legend_handles_labels(), frameon=False, loc="lower center", ncol=3)
        fig.suptitle(f"Optimizer vs resampling its round-0 strategy: {ylab}" + (" (PARTIAL DATA)" if S["partial"] else ""))
        fig.tight_layout(rect=(0, 0.1, 1, 0.95))
        fig.savefig(out / fn, dpi=150)
        plt.close(fig)
        made.append(fn)
    return made


if __name__ == "__main__":
    main()
