"""For every Locust run in logs/: real user counts and user-facing failure ratio."""
import csv
import glob
import os
from datetime import datetime
from pathlib import Path

LOGS = Path(__file__).resolve().parent.parent / "logs"

rows = []
for hist in glob.glob(str(LOGS / "**" / "*_stats_history.csv"), recursive=True):
    users, t0 = [], None
    try:
        for r in csv.DictReader(open(hist, newline="")):
            if r.get("Name") != "Aggregated" or not r.get("User Count"):
                continue
            users.append(int(float(r["User Count"])))
            t0 = t0 or int(r["Timestamp"])
    except Exception as e:  # noqa: BLE001
        continue
    stats = hist.replace("_stats_history.csv", "_stats.csv")
    req = fail = None
    if os.path.exists(stats):
        for r in csv.DictReader(open(stats, newline="")):
            if r.get("Name") == "Aggregated":
                req, fail = int(r["Request Count"]), int(r["Failure Count"])
    if not users:
        continue
    rows.append((t0 or 0, os.path.relpath(hist, LOGS).replace("_stats_history.csv", ""),
                 min(users), max(users), req, fail))

print(f"{'start':<17}{'run':<44}{'users min-max':>14}{'requests':>11}{'fail %':>8}")
for t0, name, lo, hi, req, fail in sorted(rows):
    pct = f"{100 * fail / req:.1f}" if req else "-"
    print(f"{datetime.fromtimestamp(t0).strftime('%Y-%m-%d %H:%M'):<17}{name:<44}{lo:>6}-{hi:<7}{req or 0:>11}{pct:>8}")
