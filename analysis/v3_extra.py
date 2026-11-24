"""Extra per-configuration numbers for the paper: convergence window, edge p99 median,
user-facing failure % (Locust), exploration windows.  Prints a table; writes v3_extra.json."""
import csv
import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from action_logger import load_decisions  # noqa: E402
from main import compute_reward           # noqa: E402

V2, V3 = ROOT / "logs" / "v2", ROOT / "logs" / "v3"


def locust_fail(d, tag):
    p = d / f"locust-{tag}_stats.csv"
    if not p.exists():
        return None
    for r in csv.DictReader(open(p, newline="")):
        if r["Name"] == "Aggregated" and int(r["Request Count"]):
            return 100 * int(r["Failure Count"]) / int(r["Request Count"])


def one(d, tag):
    p = d / f"decisions-{tag}.jsonl"
    if not p.exists():
        return None
    R = [r for r in load_decisions(str(p)) if not r.get("blocked")]
    rew = np.array([compute_reward(r["metrics_after"]) for r in R])
    # convergence: first window after which the same policy is chosen for 10 consecutive windows
    pols = [r["policy_id"] for r in R]
    conv = next((R[i]["window"] for i in range(len(pols) - 9) if len(set(pols[i:i + 10])) == 1), None)
    top, n = Counter(pols).most_common(1)[0]
    return {"conv": conv, "p99_med": float(np.median([r["metrics_after"]["p99_ms"] for r in R])),
            "user_fail": locust_fail(d, tag), "dominant": top, "dom_pct": 100 * n / len(R),
            "distinct": len(set(pols)), "mean": float(rew.mean())}


out = {}
rows = []
for sc in ["S1", "S2", "S3", "S4", "S5"]:
    for label, d, tag in [("v3", V3, f"adaptr4j-{sc}-r1"), ("v3r2", V3, f"adaptr4j-{sc}-r2"),
                          ("v3r3", V3, f"adaptr4j-{sc}-r3"), ("v2", V2, f"adaptr4j-{sc}-r1"),
                          ("p01", V2, f"static-p01-{sc}"), ("oracle", V2, f"b3-{sc}"),
                          ("ucb1safe", V3, f"ucb1-{sc}"), ("noctx", V3, f"abl-nocontext-{sc}")]:
        x = one(d, tag)
        if x:
            out[f"{sc}/{label}"] = x
            rows.append((sc, label, x))
print(f"{'sc':<4}{'config':<10}{'mean':>8}{'conv':>6}{'p99med':>8}{'userFail%':>10}{'distinct':>9}  dominant")
for sc, label, x in rows:
    uf = f"{x['user_fail']:.1f}" if x["user_fail"] is not None else "-"
    print(f"{sc:<4}{label:<10}{x['mean']:+8.3f}{str(x['conv']):>6}{x['p99_med']:8.0f}{uf:>10}{x['distinct']:>9}  "
          f"{x['dominant']} {x['dom_pct']:.0f}%")
(ROOT / "analysis" / "v3_extra.json").write_text(json.dumps(out, indent=2))
