"""Phase 3b additions to p3_analyze.py (pre-registered in docs/m7_log.md): pair-stratified early-vs-late tests among
sim wins (Cochran-Mantel-Haenszel with the MH odds ratio, exact conditional stratified test) and a Firth logistic
rw ~ round + pair fixed effects. Also holds the IRLS/Firth fitter p3_analyze.logistic uses."""

import math

import numpy as np
from scipy.stats import chi2, hypergeom

from simrt.stats import normal_sf, wilson


def prop(k: int, n: int) -> dict:
    return {"k": k, "n": n, "rate": k / n if n else None, "ci": list(wilson(k, n)) if n else None}


def _irls(X: np.ndarray, y: np.ndarray, firth: bool, iters: int = 200) -> tuple[np.ndarray, np.ndarray, bool]:
    b = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(X @ b, -30, 30)))
        w = p * (1 - p)
        cov, u = np.linalg.pinv(X.T @ (X * w[:, None])), y - p
        if firth:  # Firth (1993): score + h_i (1/2 - p_i), h = diag of the weighted hat matrix
            h = np.einsum("ij,jk,ik->i", X * np.sqrt(w)[:, None], cov, X * np.sqrt(w)[:, None])
            u = u + h * (0.5 - p)
        step = cov @ (X.T @ u)
        b = b + step / max(1.0, np.abs(step).max() / 5)  # damp huge steps
        if np.abs(step).max() < 1e-9:
            return b, cov, True
    return b, cov, False


def _exact_stratified(tables: list[tuple[int, int, int, int]]) -> dict:
    """Exact conditional test of a common odds ratio of 1 across strata: given every stratum's margins, the
    late-win count a_i is hypergeometric; S = sum a_i is their convolution. Two-sided p = P(S with probability
    <= P(observed)); lower = P(S <= observed) (late rounds win less)."""
    dist = np.array([1.0])
    lo = 0
    for a, b, c, d in tables:  # a late win, b late loss, c early win, d early loss
        T, m1, n1 = a + b + c + d, a + c, a + b
        ks = np.arange(max(0, n1 - (T - m1)), min(n1, m1) + 1)
        pk = hypergeom.pmf(ks, T, m1, n1)
        dist = np.convolve(dist, pk)
        lo += int(ks[0])
    obs = sum(t[0] for t in tables) - lo
    po = dist[obs]
    return {"p_two_sided": float(min(1.0, dist[dist <= po * (1 + 1e-7)].sum())),
            "p_lower_late_worse": float(min(1.0, dist[: obs + 1].sum())),
            "p_upper_late_better": float(min(1.0, dist[obs:].sum()))}


def a5_stratified(rows: list[dict], m: str, early_max: int) -> dict:
    """Pre-registered (Phase 3b): early vs late among sim wins, stratified by task pair."""
    sw = [r for r in rows if r["sim_win"] and r[m]["rw"] is not None]
    strata, used, skipped = {}, [], []
    for pair in sorted({r["pair"] for r in sw}):
        rs = [r for r in sw if r["pair"] == pair]
        late = [r for r in rs if r["round"] > early_max]
        early = [r for r in rs if r["round"] <= early_max]
        t = (sum(bool(r[m]["rw"]) for r in late), sum(not r[m]["rw"] for r in late),
             sum(bool(r[m]["rw"]) for r in early), sum(not r[m]["rw"] for r in early))
        strata[pair] = {"late": prop(t[0], len(late)), "early": prop(t[2], len(early)), "table_lw_ll_ew_el": list(t)}
        (used if early and late else skipped).append((pair, t))
    out: dict = {"strata": strata, "n_strata_informative": len(used),
                 "uninformative_strata": [p for p, _ in skipped]}
    tabs = [t for _, t in used]
    num = sum(a * d / (a + b + c + d) for a, b, c, d in tabs)
    den = sum(b * c / (a + b + c + d) for a, b, c, d in tabs)
    out["mh_odds_ratio_late_vs_early"] = (num / den if den > 0 else (math.inf if num > 0 else None))
    dev = sum(a - (a + b) * (a + c) / (a + b + c + d) for a, b, c, d in tabs)
    var = sum((a + b) * (c + d) * (a + c) * (b + d) / ((a + b + c + d) ** 2 * (a + b + c + d - 1))
              for a, b, c, d in tabs if a + b + c + d > 1)
    if var > 0:
        stat = max(abs(dev) - 0.5, 0) ** 2 / var
        out["cmh"] = {"chi2_cc": stat, "p_two_sided": float(chi2.sf(stat, 1)), "obs_minus_exp_late_wins": dev,
                      "var": var}
    else:
        out["cmh"] = {"status": "not estimable: no informative stratum has both outcomes"}
    out["exact_stratified"] = _exact_stratified(tabs) if tabs else {"status": "no informative strata"}
    # Firth logistic rw ~ round + pair fixed effects (pairs with sim wins only)
    pairs = sorted({r["pair"] for r in sw})
    fe: dict = {"n": len(sw), "pairs": len(pairs)}
    if len(sw) >= 3 and len({r["round"] for r in sw}) > 1 and len({bool(r[m]["rw"]) for r in sw}) > 1:
        X = np.column_stack([np.ones(len(sw)), [r["round"] for r in sw]] +
                            [[float(r["pair"] == p) for r in sw] for p in pairs[1:]])
        y = np.array([float(r[m]["rw"]) for r in sw])
        b, cov, conv = _irls(X, y, firth=True, iters=500)
        se = math.sqrt(max(cov[1, 1], 0))
        z = b[1] / se if se > 0 else math.nan
        fe.update(coef_round=float(b[1]), se=se, z=z, p_wald=2 * normal_sf(abs(z)) if math.isfinite(z) else None,
                  odds_ratio_per_round=math.exp(b[1]), converged=conv, method="firth")
    else:
        fe["status"] = "not estimable"
    out["logit_round_pair_fe_firth"] = fe
    return out
