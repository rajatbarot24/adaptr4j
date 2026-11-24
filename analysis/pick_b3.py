"""Pick the B3 oracle (best fixed menu policy in hindsight) from a sweep log.

python analysis/pick_b3.py logs/decisions-sweep-S1.jsonl   -> prints e.g. "p05"
"""
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from action_logger import load_decisions  # noqa: E402
from main import compute_reward           # noqa: E402

scores = defaultdict(list)
for r in load_decisions(sys.argv[1]):
    if not r.get("blocked"):
        scores[r["policy_id"]].append(compute_reward(r["metrics_after"]))
ranked = sorted(scores.items(), key=lambda kv: -sum(kv[1]) / len(kv[1]))
if "-v" in sys.argv:
    for pid, v in ranked[:8]:
        print(f"{pid}: {sum(v) / len(v):+.3f} ({len(v)} windows)", file=sys.stderr)
print(ranked[0][0])
