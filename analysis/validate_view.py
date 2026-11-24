"""Per-window comparison: what the reward saw vs what Locust users experienced.

python analysis/validate_view.py logs/diag/decisions-val-S1.jsonl logs/diag/locust-val-S1_stats_history.csv
"""
import csv
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from action_logger import load_decisions  # noqa: E402

dec, hist = sys.argv[1], sys.argv[2]
rows = []
for r in csv.DictReader(open(hist, newline="")):
    if r.get("Name") == "Aggregated" and r.get("Total Request Count"):
        rows.append((int(r["Timestamp"]), int(float(r["User Count"])),
                     int(r["Total Request Count"]), int(r["Total Failure Count"])))


def at(ts):
    best = None
    for row in rows:
        if row[0] <= ts:
            best = row
    return best


print(f"{'win':>4} {'policy':<6}{'blk':<5}{'users':>6}{'reward':>8}{'p99':>7}{'err(reward)':>12}{'fail(users)':>12}")
agree = []
for r in load_decisions(dec):
    end = datetime.fromisoformat(r["ts"]).timestamp()
    a, b = at(end - 60), at(end)
    if not a or not b or b[2] == a[2]:
        continue
    user_fail = (b[3] - a[3]) / (b[2] - a[2])
    m = r["metrics_after"]
    agree.append(abs(user_fail - m["error_rate"]))
    print(f"{r['window']:>4} {r['policy_id']:<6}{str(r['blocked'])[0]:<5}{b[1]:>6}{r['reward']:+8.3f}"
          f"{m['p99_ms']:7.0f}{m['error_rate'] * 100:11.1f}%{user_fail * 100:11.1f}%")
if agree:
    print(f"\nmean |reward error rate - user failure rate| = {100 * sum(agree) / len(agree):.2f} pp")
