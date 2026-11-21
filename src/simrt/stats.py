"""Exact tests and intervals for the small-count analyses (analyze_m3, reanalysis, defenses).

scipy is a Control Tower runtime dependency, so Fisher, binomial and Pearson tests come from it.
The Wilson interval, Zelen's homogeneity test and the split permutation test are written out.
"""

import itertools
import math
import random
from collections import Counter
from collections.abc import Iterable, Sequence
from functools import lru_cache

from scipy.stats import binomtest, fisher_exact, pearsonr

# The audit rule is defined once, in simrt.monitoring (optimize and validate use it too): monitor means sit
# on a 1/6 grid and the threshold is itself one of them, so ties are compared with a tolerance (EPS).
from simrt.monitoring import EPS, TIE_RULES, is_caught, real_win  # noqa: F401 (TIE_RULES, real_win re-exported)


def passes(sus: float, threshold: float, tie_rule: str = "lenient") -> bool:
    """True if a monitor score is NOT caught (the complement of simrt.monitoring.is_caught).

    lenient: sus <= threshold (the repo's original rule: a score equal to the threshold is not audited)
    strict:  sus <  threshold (a tie is audited)
    """
    return not is_caught(sus, threshold, tie_rule)


def near_ties(scores: Iterable[float | None], threshold: float, tol: float = 0.01) -> list[float]:
    """Distinct scores within tol of the threshold that the tie rules do not treat as ties (more than
    EPS away). Monitor means sit on a coarse grid, so such a score points to a rounded threshold
    (5.17 or 5.1667 for 31/6), under which the lenient and strict rules give that score the same verdict."""
    return sorted({s for s in scores if s is not None and EPS < abs(s - threshold) <= tol})


def rounded_threshold_warning(scores: Iterable[float | None], threshold: float) -> str | None:
    near = near_ties(scores, threshold)
    if not near:
        return None
    return (f"WARNING: {len(near)} distinct score(s) lie within 0.01 of the threshold {threshold:.10g} without tying "
            f"it ({', '.join(f'{s:.10g}' for s in near)}), so both tie rules give them the same verdict. If the "
            "threshold was rounded, pass the exact value from threshold.txt.")


def recorded_rule_warning(records: Iterable[dict], threshold: float, sim_threshold: float | None = None,
                          tie_rule: str = "lenient") -> str | None:
    """A WARNING line when validation.jsonl records say validate scored them under another threshold,
    simulated threshold or tie rule than this analysis uses (old records without these fields say nothing)."""
    used: dict[str, float | str] = {"threshold": threshold, "tie_rule": tie_rule,
                                    "sim_threshold": threshold if sim_threshold is None else sim_threshold}
    found: Counter[str] = Counter()
    for rec in records:
        for key, want in used.items():
            got = rec.get(key)
            if got is not None and (got != want if isinstance(want, str) else abs(float(got) - want) > EPS):
                found[f"{key} {got}"] += 1
    if not found:
        return None
    seen = ", ".join(f"{k} ({n} record(s))" for k, n in sorted(found.items()))
    return (f"WARNING: validate recorded {seen}, but this analysis uses threshold {threshold:.10g}, simulated "
            f"threshold {used['sim_threshold']:.10g} and tie rule {tie_rule}, so its verdicts can differ from validate's.")


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for k successes out of n (nan, nan when n == 0)."""
    if n == 0:
        return (math.nan, math.nan)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


@lru_cache(maxsize=None)
def fisher(k1: int, n1: int, k2: int, n2: int, alternative: str = "two-sided") -> float:
    """Fisher's exact p comparing k1/n1 with k2/n2. alternative="greater": the first rate is higher."""
    if n1 == 0 or n2 == 0:
        return math.nan
    return float(fisher_exact([[k1, n1 - k1], [k2, n2 - k2]], alternative=alternative).pvalue)


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p from the two discordant counts."""
    return 1.0 if b + c == 0 else float(binomtest(b, b + c, 0.5).pvalue)


def zelen_exact(tables: Sequence[tuple[int, int, int, int]]) -> float:
    """Zelen's exact test that 2x2 tables (a, b, c, d) = [[a, b], [c, d]] share one odds ratio.

    Conditions on each table's margins and on the sum of the a cells; two-sided (sums the
    probabilities of all configurations no more likely than the observed one).
    """
    margins = [(a + b, c + d, a + c) for a, b, c, d in tables]  # row 1, row 2, column 1

    def weight(cells: Sequence[int]) -> int:
        return math.prod(math.comb(r1, x) * math.comb(r2, m1 - x) for x, (r1, r2, m1) in zip(cells, margins))

    supports = [range(max(0, m1 - r2), min(r1, m1) + 1) for r1, r2, m1 in margins]
    total_a = sum(t[0] for t in tables)
    weights = [weight(cfg) for cfg in itertools.product(*supports) if sum(cfg) == total_a]
    observed = weight([t[0] for t in tables])
    return sum(w for w in weights if w <= observed) / sum(weights)


def pearson(x: Sequence[float], y: Sequence[float]) -> tuple[float, float]:
    """(r, two-sided p); nan when fewer than 3 points or a constant series."""
    if len(x) < 3 or len(set(x)) < 2 or len(set(y)) < 2:
        return (math.nan, math.nan)
    res = pearsonr(x, y)
    return (float(res.statistic), float(res.pvalue))


def normal_sf(z: float) -> float:
    """P(Z > z) for a standard normal."""
    return 0.5 * math.erfc(z / math.sqrt(2))


# ---- early/late splits of the optimization rounds -------------------------------------------------

def contiguous_splits(rounds: Sequence[int]) -> list[tuple[int, int]]:
    """Every (a, b) with a < b: early = rounds <= a, late = rounds >= b (rounds in between dropped). None
    without rounds (e.g. no sim win left after --exclude-sim-failures), as with a single round."""
    if not rounds:
        return []
    lo, hi = min(rounds), max(rounds)
    return [(a, b) for a in range(lo, hi) for b in range(a + 1, hi + 1)]


def split_counts(rounds: Sequence[int], wins: Sequence[bool], a: int, b: int) -> tuple[int, int, int, int]:
    """(early wins, early n, late wins, late n) for the split early <= a, late >= b."""
    early = [w for r, w in zip(rounds, wins) if r <= a]
    late = [w for r, w in zip(rounds, wins) if r >= b]
    return sum(early), len(early), sum(late), len(late)


def _min_split_p(rounds: Sequence[int], wins: Sequence[bool], splits: Sequence[tuple[int, int]]) -> float:
    levels = sorted(set(rounds))
    k = dict.fromkeys(levels, 0)
    n = dict.fromkeys(levels, 0)
    for r, w in zip(rounds, wins):
        n[r] += 1
        k[r] += int(w)
    best = math.inf
    for a, b in splits:
        k1, n1 = sum(k[r] for r in levels if r <= a), sum(n[r] for r in levels if r <= a)
        k2, n2 = sum(k[r] for r in levels if r >= b), sum(n[r] for r in levels if r >= b)
        p = fisher(k1, n1, k2, n2, "greater")
        if not math.isnan(p):
            best = min(best, p)
    return best


def split_permutation_p(rounds: Sequence[int], wins: Sequence[bool], n_perm: int = 10_000, seed: int = 0) -> float:
    """Selection-adjusted p for "some early/late split shows a drop": the share of outcome
    permutations (outcomes shuffled across rounds) whose smallest one-sided split p is at most
    the observed smallest p over all contiguous splits."""
    splits = contiguous_splits(rounds)
    observed = _min_split_p(rounds, wins, splits)
    rng = random.Random(seed)
    shuffled = list(wins)
    hits = 0
    for _ in range(n_perm):
        rng.shuffle(shuffled)
        hits += _min_split_p(rounds, shuffled, splits) <= observed * (1 + 1e-9)
    return (hits + 1) / (n_perm + 1)


# ---- formatting --------------------------------------------------------------------------------

def rate(k: int, n: int) -> str:
    """'k/n = 0.69' (or 'k/n' when n == 0)."""
    return f"{k}/{n} = {k / n:.2f}" if n else f"{k}/{n}"


def rate_ci(k: int, n: int) -> str:
    """'k/n = 0.69 [0.54-0.81]' with a 95% Wilson interval."""
    if not n:
        return f"{k}/{n}"
    lo, hi = wilson(k, n)
    return f"{rate(k, n)} [{lo:.2f}-{hi:.2f}]"


def fmt_p(p: float) -> str:
    if math.isnan(p):
        return "n/a"
    return "<0.001" if p < 0.001 else f"{p:.3f}"
