"""Summary of v2 (fixed-harness) runs: reward, user-facing failures, latency, policy.

python analysis/v2_summary.py [logs/v2]
"""
import csv
import glob
import os
import sys
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from action_logger import load_decisions  # noqa: E402
from main import compute_reward           # noqa: E402

D = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "logs" / "v2"


def block_ci(x, block=10, n=5000, seed=42):
    rng = np.random.default_rng(seed)
    b = block if len(x) >= 2 * block else max(1, len(x) // 4)
    nb = int(np.ceil(len(x) / b))
    s = rng.integers(0, len(x) - b + 1, size=(n, nb))
    m = x[(s[..., None] + np.arange(b)).reshape(n, -1)[:, :len(x)]].mean(1)
    return np.percentile(m, [2.5, 97.5])


print(f"{'run':<22}{'n':>4}{'reward':>8}  {'95% block CI':<17}{'userFail%':>10}{'edge p99':>9}"
      f"{'p99<500':>8}{'conv':>5}  dominant")
for f in sorted(glob.glob(str(D / "decisions-*.jsonl"))):
    tag = os.path.basename(f)[10:-6]
    if tag.startswith("sweep"):
        continue
    R = [r for r in load_decisions(f) if not r.get("blocked")]
    if not R:
        continue
    rew = np.array([compute_reward(r["metrics_after"]) for r in R])
    p99 = np.array([r["metrics_after"]["p99_ms"] for r in R])
    lo, hi = block_ci(rew)
    conv = next((R[i]["window"] for i in range(len(rew) - 2) if rew[i:i + 3].min() > 0), None)
    pid, cnt = Counter(r["policy_id"] for r in R).most_common(1)[0]
    uf = "-"
    st = D / f"locust-{tag}_stats.csv"
    if st.exists():
        for row in csv.DictReader(open(st, newline="")):
            if row["Name"] == "Aggregated" and int(row["Request Count"]):
                uf = f"{100 * int(row['Failure Count']) / int(row['Request Count']):.1f}"
    print(f"{tag:<22}{len(R):>4}{rew.mean():+8.3f}  [{lo:+.2f},{hi:+.2f}]   {uf:>9}"
          f"{np.median(p99):9.0f}{100 * (p99 <= 500).mean():7.0f}%{str(conv):>5}  {pid} {100 * cnt / len(R):.0f}%")
