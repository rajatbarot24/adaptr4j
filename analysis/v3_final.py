"""Final v3 analysis: AdaptR4j (3 runs) vs default p01, B3 oracle, UCB1 (safe menu),
no-context ablation, and v2 AdaptR4j. All rewards recomputed with main.compute_reward
(bounded). Writes analysis/v3_final.json.

python analysis/v3_final.py
"""
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from action_logger import load_decisions  # noqa: E402
from main import compute_reward           # noqa: E402

V2, V3 = ROOT / "logs" / "v2", ROOT / "logs" / "v3"
BLOCK, NB, SEED = 10, 5000, 42


def load(p):
    if not p.exists():
        return None
    R = [r for r in load_decisions(str(p)) if not r.get("blocked")]
    rew = np.array([compute_reward(r["metrics_after"]) for r in R])
    err = np.array([r["metrics_after"]["error_rate"] for r in R])
    late = rew[[r["window"] >= 40 for r in R]]
    return {"rew": rew, "mean": float(rew.mean()), "late": float(late.mean()), "err": float(err.mean())}


def block_diff_p(a, b):
    rng = np.random.default_rng(SEED)

    def rs(x):
        bl = BLOCK if len(x) >= 2 * BLOCK else max(1, len(x) // 4)
        nb = int(np.ceil(len(x) / bl))
        s = rng.integers(0, len(x) - bl + 1, size=(NB, nb))
        return x[(s[..., None] + np.arange(bl)).reshape(NB, -1)[:, :len(x)]].mean(1)

    d = rs(a) - rs(b)
    return min(1.0, max(2 * min((d <= 0).mean(), (d >= 0).mean()), 1 / NB))


out = {}
print(f"{'sc':<4}{'AdaptR4j v3 (3 runs) mean±sd':<30}{'late':>7}{'err%':>6} | {'p01':>7}{'oracle':>8}"
      f"{'UCB1-safe':>10}{'no-ctx':>8}{'v2':>8} | p(v3 vs p01, oracle, UCB1)")
for sc in ["S1", "S2", "S3", "S4", "S5"]:
    runs = [load(V3 / f"decisions-adaptr4j-{sc}-r{i}.jsonl") for i in (1, 2, 3)]
    runs = [r for r in runs if r]
    m = [r["mean"] for r in runs]
    pooled = np.concatenate([r["rew"] for r in runs])
    ref = {k: load(p) for k, p in {
        "p01": V2 / f"decisions-static-p01-{sc}.jsonl", "oracle": V2 / f"decisions-b3-{sc}.jsonl",
        "ucb1": V3 / f"decisions-ucb1-{sc}.jsonl", "noctx": V3 / f"decisions-abl-nocontext-{sc}.jsonl",
        "v2": V2 / f"decisions-adaptr4j-{sc}-r1.jsonl"}.items()}
    fmt = lambda k: f"{ref[k]['mean']:+.3f}" if ref[k] else "   -  "
    ps = []
    for k in ("p01", "oracle", "ucb1"):
        ps.append(f"{block_diff_p(runs[0]['rew'], ref[k]['rew']):.3f}" if ref[k] else "-")
    sd = np.std(m, ddof=1) if len(m) > 1 else 0.0
    print(f"{sc:<4}{np.mean(m):+.3f} ± {sd:.3f} (n={len(m)}){'':<9}{np.mean([r['late'] for r in runs]):+7.3f}"
          f"{100 * np.mean([r['err'] for r in runs]):6.1f} | {fmt('p01'):>7}{fmt('oracle'):>8}{fmt('ucb1'):>10}"
          f"{fmt('noctx'):>8}{fmt('v2'):>8} | {', '.join(ps)}")
    out[sc] = {"v3_runs": m, "v3_mean": float(np.mean(m)), "v3_sd": float(sd),
               "v3_late": float(np.mean([r["late"] for r in runs])),
               "v3_err": float(np.mean([r["err"] for r in runs])),
               **{k: (v["mean"] if v else None) for k, v in ref.items()},
               **{f"{k}_err": (v["err"] if v else None) for k, v in ref.items()},
               "p_vs": dict(zip(("p01", "oracle", "ucb1"), ps))}
(ROOT / "analysis" / "v3_final.json").write_text(json.dumps(out, indent=2))
print("\nwrote analysis/v3_final.json  (p = block-bootstrap, run 1 vs reference)")

