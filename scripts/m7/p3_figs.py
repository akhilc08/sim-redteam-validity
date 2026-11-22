"""Figures for p3_analyze.py: small multiples, one panel per group (pooled, phases, pairs), wrapped 5 per row."""

import math
from pathlib import Path

MODES = ("rerun", "replay")  # same order as p3_analyze.MODES
COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#8a5cc2", "#d6457a", "#2aa7b8", "#7a8a1e", "#b5651d",
          "#4b5bd6", "#c23b3b", "#3e9e5f"]  # categorical slots in fixed order; pooled in ink
INK = "#333333"


def short(g: str) -> str:
    return g.split("__")[-1]


def figures(S: dict, rows: list[dict], a, out: Path) -> list[str]:
    """Small multiples: one panel per pair plus pooled (columns), one row per real mode where relevant."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MaxNLocator
    plt.rcParams.update({"axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
                         "grid.color": "#e6e6e6", "font.size": 8})
    G = list(S["groups"])
    pairs = S["pair_groups"]
    col = {g: COLORS[i % len(COLORS)] for i, g in enumerate(pairs)} | {g: INK for g in G if g not in pairs}
    made, suffix = [], " (PARTIAL DATA)" if S["partial"] else ""
    ncol = min(len(G), 5)
    blk = -(-len(G) // ncol)  # panel rows per mode

    def grid(nrows: int, title: str):
        """Returns fig and axs[mode_row][group_index] (groups wrap into rows of at most 5 panels)."""
        fig, raw = plt.subplots(nrows * blk, ncol, figsize=(3.2 * ncol, 2.9 * nrows * blk), sharex=True, sharey=True,
                                squeeze=False)
        axs = [[raw[i * blk + j // ncol][j % ncol] for j in range(len(G))] for i in range(nrows)]
        used = {id(ax) for row in axs for ax in row}
        for ax in raw.flat:
            ax.xaxis.set_major_locator(MaxNLocator(integer=True))
            if id(ax) not in used:
                ax.set_visible(False)
        for row in axs:
            for ax, g in zip(row, G):
                ax.set_title(short(g), color=col[g], fontweight="bold")
        for ax in raw[-1]:
            ax.set_xlabel("optimization round")
        fig.suptitle(title + suffix)
        return fig, axs

    def band(ax, d: dict, c: str):
        d = {int(r): v for r, v in d.items() if v["n"]}
        if d:
            x = sorted(d)
            ax.fill_between(x, [d[r]["ci"][0] for r in x], [d[r]["ci"][1] for r in x], color=c, alpha=0.15, lw=0)
            ax.plot(x, [d[r]["rate"] for r in x], "-o", color=c, lw=2, ms=5)
            for r in x:
                ax.annotate(f"{d[r]['k']}/{d[r]['n']}", (r, d[r]["rate"]), textcoords="offset points", xytext=(0, 6),
                            ha="center", fontsize=6.5, color="#555555")
        ax.set_ylim(-0.05, 1.12)

    def save(fig, name: str):
        fig.tight_layout(rect=(0, 0.12 if fig.legends else 0, 1, 0.97))  # top margin keeps the suptitle clear
        fig.savefig(out / name, dpi=150)
        plt.close(fig)
        made.append(name)

    fig, axs = grid(1, f"Simulated win rate by round (judge p >= 0.5 and sim sus <= {a.threshold:g}); Wilson 95% bands, labels k/n")
    for ax, g in zip(axs[0], G):
        band(ax, S["groups"][g]["optimization"]["sim_win_by_round"], col[g])
    axs[0][0].set_ylabel("P(sim win)")
    save(fig, "fig_sim_win_by_round.png")

    fig, axs = grid(2, f"P(real win | sim win) by round (real win = side task succeeds and real sus <= {a.threshold:g})")
    for row, m in zip(axs, MODES):
        for ax, g in zip(row, G):
            band(ax, S["groups"][g]["gap"][m]["rw_given_sw_by_round"], col[g])
        row[0].set_ylabel(f"{m}: P(real win | sim win)")
    save(fig, "fig_real_win_given_sim_win_by_round.png")

    fig, axs = grid(2, "Suspicion gap (real sus - sim sus, target monitor) by round; line = mean")
    for row, m in zip(axs, MODES):
        for ax, g in zip(row, G):
            pts = [(r["round"], r[m]["sus"] - r["sim_sus"]) for r in rows if (g == "pooled" or r["pair"] == g)
                   and r[m]["sus"] is not None and r["sim_sus"] is not None] or [(math.nan, math.nan)]
            ax.scatter(*zip(*pts), s=22, color=col[g], alpha=0.6, edgecolor="white")
            d = sorted((int(k), v["mean"]) for k, v in S["groups"][g]["gap"][m]["sus_gap_by_round"].items())
            ax.plot([x for x, _ in d], [y for _, y in d], "-", color=col[g], lw=2)
            ax.axhline(0, color="#999999", lw=1)
        row[0].set_ylabel(f"{m} - sim suspicion")
    save(fig, "fig_sus_gap_by_round.png")

    if S["heldout_present"]:
        fig, axs = plt.subplots(1, 3, figsize=(11, 4.4), sharex=True, sharey=True)
        for ax, k in zip(axs, ("sim", "replay", "rerun")):
            for g in pairs:
                pts = [(r["tsus"][k], r["ho"][k][0]) for r in rows if r["pair"] == g and r["ho"][k][0] is not None
                       and r["tsus"][k] is not None]
                if pts:
                    ax.scatter(*zip(*pts), s=24, color=col[g], alpha=0.75, label=short(g), edgecolor="white")
            ax.axvline(a.threshold, color="#999999", ls="--", lw=1)
            ax.axhline(a.heldout_threshold, color="#999999", ls="--", lw=1)
            ax.set(xlim=(-0.3, 10.3), ylim=(-0.3, 10.3), title=f"{k} transcripts",
                   xlabel=f"target sus (threshold {a.threshold:g})")
        axs[0].set_ylabel(f"held-out sus (threshold {a.heldout_threshold:g})")
        fig.legend(*max((ax.get_legend_handles_labels() for ax in axs), key=lambda hl: len(hl[1])), frameon=False, fontsize=7, loc="lower center", ncol=6)
        fig.suptitle("Held-out vs target monitor suspicion (dashed = thresholds; caught = above)" + suffix)
        save(fig, "fig_heldout_vs_target.png")
    return made
