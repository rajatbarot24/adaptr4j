"""
Structured JSONL logger for the AdaptR4j policy engine.

Each line records one decision cycle:
  { "ts": <ISO-8601>, "window": <int>, "context": [...], "action": <int>,
    "policy_id": "...", "metrics_before": {...}, "metrics_after": {...},
    "reward": <float>, "blocked": <bool>, "block_reason": "..." | null,
    "rollback": <bool> }

The JSONL file is the primary artefact for offline analysis (plot.py) and
for replaying episodes into the Bayesian optimiser.
"""

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


class ActionLogger:
    """Thread-safe append-only JSONL writer."""

    def __init__(self, path: str = "logs/decisions.jsonl"):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh = self._path.open("a", encoding="utf-8")
        log.info("ActionLogger writing to %s", self._path.resolve())

    # ------------------------------------------------------------------
    def log_decision(
        self,
        *,
        window: int,
        context: np.ndarray,
        action: int,
        policy_id: str,
        metrics_before: dict,
        metrics_after: dict,
        reward: float,
        blocked: bool,
        block_reason,          # BlockReason | None
        rollback: bool,
        ucb_scores: np.ndarray | None = None,
    ) -> None:
        record = {
            "ts":             datetime.now(timezone.utc).isoformat(),
            "window":         window,
            "context":        context.tolist(),
            "action":         action,
            "policy_id":      policy_id,
            "metrics_before": _sanitise(metrics_before),
            "metrics_after":  _sanitise(metrics_after),
            "reward":         round(float(reward), 6),
            "blocked":        blocked,
            "block_reason":   block_reason.name if block_reason is not None else None,
            "rollback":       rollback,
            "ucb_scores":     ucb_scores.tolist() if ucb_scores is not None else None,
        }
        line = json.dumps(record, allow_nan=False)
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()

    # ------------------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ------------------------------------------------------------------
def load_decisions(path: str = "logs/decisions.jsonl") -> list[dict]:
    """Read all logged decisions back into memory (for offline analysis)."""
    records = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def compute_cumulative_reward(records: list[dict]) -> list[float]:
    cumsum, total = [], 0.0
    for r in records:
        total += r["reward"]
        cumsum.append(total)
    return cumsum


# ------------------------------------------------------------------
def _sanitise(d: dict) -> dict:
    """Replace any float NaN/Inf with None so json.dumps doesn't raise."""
    out = {}
    for k, v in d.items():
        if isinstance(v, float) and (v != v or abs(v) == float("inf")):
            out[k] = None
        else:
            out[k] = v
    return out
