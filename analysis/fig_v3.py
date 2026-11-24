"""Figures for the WIP paper.  python analysis/fig_v3.py -> paper/figures/fig_s5_timeline.pdf"""
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from action_logger import load_decisions  # noqa: E402
from main import compute_reward           # noqa: E402

OUT = ROOT / "paper" / "figures"
OUT.mkdir(parents=True, exist_ok=True)


def series(p):
    R = [r for r in load_decisions(str(p)) if not r.get("blocked")]
    return np.array([r["window"] for r in R]), np.array([compute_reward(r["metrics_after"]) for r in R])


def smooth(y, k=5):
    return np.convolve(y, np.ones(k) / k, mode="same")


fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.3), sharey=True)
for ax, sc, title in [(axes[0], "S4", "S4: error storm (30% errors)"),
                      (axes[1], "S5", "S5: latency spikes (0/500 ms, 5 min)")]:
    for label, p, style in [
        ("AdaptR4j (safe)", ROOT / "logs/v3" / f"decisions-adaptr4j-{sc}-r1.jsonl", dict(color="#1f5fa8", lw=1.6)),
        ("UCB1 (safe menu)", ROOT / "logs/v3" / f"decisions-ucb1-{sc}.jsonl", dict(color="#8a8a8a", lw=1.0)),
        ("Best fixed policy", ROOT / "logs/v2" / f"decisions-b3-{sc}.jsonl", dict(color="#2e8b57", lw=1.2, ls="--")),
        ("Default p01", ROOT / "logs/v2" / f"decisions-static-p01-{sc}.jsonl", dict(color="#c0392b", lw=1.2, ls=":")),
    ]:
        w, r = series(p)
        k = 10 if sc == "S5" else 5      # S5: average over one full 10-min spike cycle
        ax.plot(w[k // 2: len(w) - k // 2], smooth(r, k)[k // 2: len(w) - k // 2], label=label, **style)
    ax.set_title(title, fontsize=8)
    ax.set_xlabel("window (1 min)", fontsize=7)
    ax.tick_params(labelsize=7)
    ax.set_ylim(-2.1, 1.05)
    ax.grid(alpha=0.25)
axes[0].set_ylabel("reward (moving mean)", fontsize=7)
axes[0].set_title("S4: error storm (30% errors), 5-window mean", fontsize=8)
axes[1].set_title("S5: latency spikes, 10-window (one cycle) mean", fontsize=8)
h, l = axes[0].get_legend_handles_labels()
fig.legend(h, l, loc="lower center", ncol=4, fontsize=7, frameon=False)
fig.tight_layout(rect=(0, 0.08, 1, 1))
fig.savefig(OUT / "fig_timeline.pdf"); fig.savefig(OUT / "fig_timeline_preview.png", dpi=150)
print("wrote", OUT / "fig_timeline.pdf")

