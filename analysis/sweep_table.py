"""Per-policy outcome in each scenario sweep (2 windows per policy).

python analysis/sweep_table.py            -> p99 / error table + catastrophic policies
"""
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from action_logger import load_decisions  # noqa: E402

V2 = ROOT / "logs" / "v2"
SC = ["S1", "S2", "S3", "S4", "S5"]


def sweep(sc):
    d = defaultdict(list)
    for r in load_decisions(str(V2 / f"decisions-sweep-{sc}.jsonl")):
        if not r.get("blocked"):
            d[r["policy_id"]].append((r["metrics_after"]["p99_ms"], r["metrics_after"]["error_rate"]))
    return {k: (np.mean([x[0] for x in v]), np.mean([x[1] for x in v])) for k, v in d.items()}


S = {sc: sweep(sc) for sc in SC}
pols = sorted(set().union(*[s.keys() for s in S.values()]))
print("policy " + "".join(f"{sc + ' p99/err%':>17}" for sc in SC))
for p in pols:
    row = ""
    for sc in SC:
        if p in S[sc]:
            p99, e = S[sc][p]
            mark = "!" if e > 0.5 else ("*" if p99 <= 500 and e <= 0.05 else " ")
            row += f"{p99:9.0f}/{100 * e:5.1f}{mark} "
        else:
            row += f"{'-':>17}"
    print(f"{p:<6} {row}")
print("\n! = >50% user-visible errors (catastrophic)   * = meets both SLOs (p99<=500 ms, err<=5%)")
for sc in SC:
    ok = [p for p, (p99, e) in S[sc].items() if p99 <= 500 and e <= 0.05]
    bad = [p for p, (p99, e) in S[sc].items() if e > 0.5]
    print(f"{sc}: {len(ok):2d} policies meet both SLOs: {' '.join(sorted(ok))}")
    print(f"{'':4}{len(bad):2d} catastrophic: {' '.join(sorted(bad))}")
