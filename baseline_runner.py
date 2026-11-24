"""
Baseline runner for AdaptR4j evaluation.

Strategies:
  static-p01    Always use p01 (balanced-fallback, the ops default)
  static-p04    Always use p04 (oracle best for low-fault scenarios)
  static-p13    Always use p13 (oracle best for high-fault scenarios)
  random        Uniform random policy each window
  ucb1          Context-free UCB1 bandit (no feature vector)
  epsilon-greedy  ε=0.1 greedy
"""

import argparse
import logging
import random
import sys
import time

import numpy as np
import requests

from action_logger import ActionLogger
from guardrails import Guardrails
from main import compute_reward
from metrics_client import MetricsClient
from policy_menu import load_menu

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)-20s — %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("adaptr4j.baseline")

_SLO_P99_MS      = 500.0
_SLO_ERROR_RATE  = 0.05


def _compute_reward(metrics: dict) -> float:
    # Same reward as the AdaptR4j engine (main.compute_reward) so that every
    # strategy is scored identically.  Earlier baseline logs used a softer
    # formula; analysis/paper_stats.py recomputes all logged rewards with this one.
    return round(compute_reward(metrics), 4)


def _apply_policy(service_a_url: str, policy) -> bool:
    try:
        r = requests.post(f"{service_a_url}/policy", json=policy.to_api_payload(), timeout=5)
        return r.status_code == 200
    except Exception:
        return False


# ── Strategies ─────────────────────────────────────────────────────────────

class StaticStrategy:
    def __init__(self, policy_idx: int):
        self._idx = policy_idx

    def select(self, context, n_actions):
        return self._idx, None

    def update(self, action, reward):
        pass


class RandomStrategy:
    def select(self, context, n_actions):
        return random.randrange(n_actions), None

    def update(self, action, reward):
        pass


class UCB1Strategy:
    """Context-free UCB1 — upper confidence bound without feature vectors."""
    def __init__(self, n_actions: int):
        self._n = np.zeros(n_actions)
        self._q = np.zeros(n_actions)
        self._total = 0

    def select(self, context, n_actions):
        self._total += 1
        unvisited = np.where(self._n == 0)[0]
        if len(unvisited):
            return int(unvisited[0]), None
        ucb = self._q + np.sqrt(2.0 * np.log(self._total) / self._n)
        return int(np.argmax(ucb)), ucb

    def update(self, action, reward):
        self._n[action] += 1
        self._q[action] += (reward - self._q[action]) / self._n[action]


class EpsilonGreedyStrategy:
    def __init__(self, n_actions: int, epsilon: float = 0.1):
        self._eps = epsilon
        self._n   = np.zeros(n_actions)
        self._q   = np.zeros(n_actions)

    def select(self, context, n_actions):
        if random.random() < self._eps or not np.any(self._n):
            return random.randrange(n_actions), None
        return int(np.argmax(self._q)), None

    def update(self, action, reward):
        self._n[action] += 1
        self._q[action] += (reward - self._q[action]) / self._n[action]


class RuleStrategy:
    """Operator-style threshold rule with hysteresis (no learning).

    Walks a fixed ladder of increasingly conservative menu policies:
        p01 (3 attempts) -> p04 (no retry, wide) -> p12 (no retry, CB 35%, 20 slots)
            -> p13 (no retry, CB 25%, 10 slots)
    Escalate one step when the last window looked unhealthy
    (p99 > 0.8*SLO or error rate > 2% or RAF > 0.5); de-escalate one step
    after 3 consecutive healthy windows (p99 < 0.3*SLO and error rate < 0.5%).
    This mirrors the 'tighten on trouble, relax when calm' runbooks SREs use,
    and is given the same menu policies the bandit tends to choose.
    """
    LADDER = ["p01", "p04", "p12", "p13"]

    def __init__(self, policies):
        ids = [p.id for p in policies]
        self._idx = [ids.index(pid) for pid in self.LADDER]
        self._level = 0
        self._calm = 0

    def observe(self, metrics: dict) -> None:
        p99 = metrics.get("p99_ms", 0.0)
        err = metrics.get("error_rate", 0.0)
        raf = metrics.get("raf", 0.0)
        if p99 > 0.8 * _SLO_P99_MS or err > 0.02 or raf > 0.5:
            self._level = min(self._level + 1, len(self.LADDER) - 1)
            self._calm = 0
        elif p99 < 0.3 * _SLO_P99_MS and err < 0.005:
            self._calm += 1
            if self._calm >= 3:
                self._level = max(self._level - 1, 0)
                self._calm = 0
        else:
            self._calm = 0

    def select(self, context, n_actions):
        return self._idx[self._level], None

    def update(self, action, reward):
        pass


class SweepStrategy:
    """Apply every menu policy for `per_policy` consecutive windows, in menu order.

    Used once per scenario to find the best fixed policy in hindsight (the B3 oracle):
    analysis/pick_b3.py picks the policy with the highest mean reward in its windows.
    """
    def __init__(self, n_actions: int, per_policy: int):
        self._n, self._k, self._i = n_actions, per_policy, 0

    def select(self, context, n_actions):
        idx = min(self._i // self._k, self._n - 1)
        self._i += 1
        return idx, None

    def update(self, action, reward):
        pass


def _build_strategy(name: str, policies, sweep_per_policy: int = 2):
    n = len(policies)
    if name.startswith("static:"):
        pid = name.split(":", 1)[1]
        return StaticStrategy(next(i for i, p in enumerate(policies) if p.id == pid))
    if name == "sweep":
        return SweepStrategy(n, sweep_per_policy)
    if name == "rule":
        return RuleStrategy(policies)
    if name == "static-p01":
        idx = next(i for i, p in enumerate(policies) if p.id == "p01")
        return StaticStrategy(idx)
    if name == "static-p04":
        idx = next(i for i, p in enumerate(policies) if p.id == "p04")
        return StaticStrategy(idx)
    if name == "static-p13":
        idx = next(i for i, p in enumerate(policies) if p.id == "p13")
        return StaticStrategy(idx)
    if name == "random":
        return RandomStrategy()
    if name == "ucb1":
        return UCB1Strategy(n)
    if name == "epsilon-greedy":
        return EpsilonGreedyStrategy(n, epsilon=0.1)
    raise ValueError(f"Unknown strategy: {name}")


# ── Main loop ───────────────────────────────────────────────────────────────

def run(args):
    policies   = load_menu(args.menu)
    client     = MetricsClient(args.prometheus)
    # Baselines are fixed strategies: only shadow mode applies.  The RAF hard stop and
    # rollback are part of AdaptR4j, and earlier runs that let rollback block baseline
    # windows lost up to a third of their scored windows.
    guardrails = Guardrails(shadow_mode_windows=args.shadow, no_guardrails=True)
    strategy   = _build_strategy(args.strategy, policies, args.sweep_per_policy)

    log.info("Baseline runner: strategy=%s windows=%d shadow=%d log=%s",
             args.strategy, args.max_windows, args.shadow, args.log_file)

    with ActionLogger(args.log_file) as logger:
        for window_num in range(1, args.max_windows + 1):
            metrics_before = client.fetch()
            context        = client.to_context_vector(metrics_before)

            blocked, block_reason = guardrails.check({"raf": metrics_before.get("raf", 1.0)})

            if not blocked:
                action, scores = strategy.select(context, len(policies))
                policy = policies[action]
                _apply_policy(args.service_a, policy)
            else:
                action = 0
                policy = policies[0]
                scores = None

            time.sleep(args.window)

            metrics_after = client.fetch()
            reward        = _compute_reward(metrics_after)
            guardrails.record_reward(reward)

            if not blocked:
                strategy.update(action, reward)
            if hasattr(strategy, "observe"):
                strategy.observe(metrics_after)

            log.info(
                "Window %4d | p99=%5.0f ms | err=%.3f | raf=%.2f | reward=%+.3f | policy=%s | blocked=%s",
                window_num,
                metrics_after.get("p99_ms", 0.0),
                metrics_after.get("error_rate", 0.0),
                metrics_after.get("raf", 1.0),
                reward, policy.id, blocked,
            )

            logger.log_decision(
                window=window_num,
                context=context,
                action=action,
                policy_id=policy.id,
                metrics_before=metrics_before,
                metrics_after=metrics_after,
                reward=reward,
                blocked=blocked,
                block_reason=block_reason,
                rollback=False,
                ucb_scores=scores,
            )

    log.info("Baseline finished: strategy=%s after %d windows", args.strategy, args.max_windows)


def main():
    p = argparse.ArgumentParser(description="AdaptR4j baseline runner")
    p.add_argument("--strategy", required=True,
                   help="static-p01 | static:<policy-id> | sweep | random | ucb1 | "
                        "epsilon-greedy | rule")
    p.add_argument("--sweep-per-policy", type=int, default=2)
    p.add_argument("--menu",        default="policy-menu.yml")
    p.add_argument("--prometheus",  default="http://localhost:9090")
    p.add_argument("--service-a",   default="http://localhost:8080")
    p.add_argument("--window",      type=int,   default=60)
    p.add_argument("--max-windows", type=int,   default=80)
    p.add_argument("--shadow",      type=int,   default=5)
    p.add_argument("--log-file",    default="logs/decisions-baseline.jsonl")
    run(p.parse_args())


if __name__ == "__main__":
    main()
