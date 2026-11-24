"""
AdaptR4j policy engine — main control loop.

Runs a 60-second observation window loop:
  1. Fetch metrics from Prometheus  (MetricsClient)
  2. Check safety guardrails        (Guardrails)
  3. Select policy via LinUCB       (LinUCBBandit)
  4. Push policy to Service A       (POST /policy)
  5. Wait one window
  6. Re-fetch metrics, compute reward
  7. Update bandit model
  8. Update anomaly detector        (AnomalyDetector)
  9. Feed transition to RL explorer (RLExplorer)
 10. Log the full decision record   (ActionLogger)

Components:
  - Component 1 (BayesianPolicyOptimiser): run offline via bayesian_opt.py
  - Component 2 (LinUCBBandit):            online, runs every window
  - Component 3 (RLExplorer):              online, trains in background
  - Component 4 (AnomalyDetector):         online, shadow-mode signal
"""

import argparse
import logging
import signal
import sys
import time

import numpy as np
import requests

from action_logger import ActionLogger
from anomaly_detector import AnomalyDetector
from bandit import LinUCBBandit
from guardrails import Guardrails
from metrics_client import MetricsClient
from policy_menu import load_menu
from rl_explorer import RLExplorer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("adaptr4j.main")

# ---------------------------------------------------------------------------
# Reward function
# ---------------------------------------------------------------------------
_SLO_P99_MS    = 500.0
_SLO_ERR_RATE  = 0.05
_DELTA_MS      = _SLO_P99_MS * 0.05   # ±5% of SLO = safety invariant delta


def compute_reward(metrics: dict) -> float:
    """
    Reward ∈ [-2, +1].

    Positive when p99 is well below SLO; penalised for error rate and RAF.
    Hard penalty of -2 when p99 breaches SLO.

    v3 (Nov 2026): the result is clipped at -2.  Before, the error penalty was
    unbounded, so a window in which a strict circuit breaker rejected ~100 % of
    traffic scored about -20 and dominated every mean.  A window can now never be
    worse than an SLO breach.
    """
    p99   = metrics["p99_ms"]
    err   = metrics["error_rate"]
    raf   = metrics["raf"]

    if p99 > _SLO_P99_MS:
        return -2.0

    # Normalised latency saving [0, 1]
    lat_reward = max(0.0, 1.0 - p99 / _SLO_P99_MS)

    # Penalties
    err_pen = err / _SLO_ERR_RATE              # 1.0 at SLO boundary
    raf_pen = max(0.0, raf - 1.0) * 0.5       # 0 at raf=1, 0.5 at raf=2

    return max(-2.0, lat_reward - err_pen - raf_pen)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _push_policy(service_a_url: str, payload: dict, timeout: float = 5.0) -> bool:
    try:
        r = requests.post(
            f"{service_a_url}/policy",
            json=payload,
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
        log.info("Policy applied: id=%s version=%s", data.get("policy_id"), data.get("version"))
        return True
    except Exception as exc:
        log.error("Failed to push policy: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Control loop
# ---------------------------------------------------------------------------

def run(
    policies,
    bandit: LinUCBBandit,
    guardrails: Guardrails,
    metrics_client: MetricsClient,
    anomaly_detector: AnomalyDetector,
    rl_explorer: RLExplorer,
    action_logger: ActionLogger,
    service_a_url: str,
    window_seconds: int,
    max_windows: int,
    no_context: bool = False,
    fixed_policy: str = None,
) -> None:
    log.info(
        "AdaptR4j starting — %d policies, window=%ds, max_windows=%d",
        len(policies), window_seconds, max_windows,
    )

    # Graceful shutdown
    _stop = [False]
    def _handler(sig, _frame):
        log.info("Shutdown signal received")
        _stop[0] = True
    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)

    current_fault = np.zeros(3)   # [latency_ms, jitter_ms, error_rate_pct]

    for window_idx in range(1, max_windows + 1):
        if _stop[0]:
            break

        # ------------------------------------------------------------------
        # 1. Fetch metrics BEFORE applying a new policy
        # ------------------------------------------------------------------
        metrics_before = metrics_client.fetch()
        context = metrics_client.to_context_vector(metrics_before, slo_p99_ms=_SLO_P99_MS)
        if no_context:
            context = np.ones(bandit.n_features)  # ablation: strip context information

        # ------------------------------------------------------------------
        # 2. Safety guardrail check
        # ------------------------------------------------------------------
        blocked, block_reason = guardrails.check(metrics_before)

        # ------------------------------------------------------------------
        # 3. Anomaly detection (always runs for signal)
        # ------------------------------------------------------------------
        is_anomaly = anomaly_detector.update(context)
        if is_anomaly:
            log.warning("Anomaly detected — score=%.3f",
                        anomaly_detector.anomaly_score(context))

        # ------------------------------------------------------------------
        # 4. Select action (even when blocked, for logging)
        # ------------------------------------------------------------------
        action_idx, ucb_scores = bandit.select_action(context)
        chosen_policy = policies[action_idx]
        # B3 / fixed-policy override: always select the specified policy
        if fixed_policy is not None:
            for i, p in enumerate(policies):
                if p.id == fixed_policy:
                    action_idx, chosen_policy = i, p
                    break

        # ------------------------------------------------------------------
        # 5. Apply policy (unless guardrail blocked)
        # ------------------------------------------------------------------
        if not blocked:
            _push_policy(service_a_url, chosen_policy.to_api_payload())
        else:
            log.info("Window %d: blocked by %s", window_idx, block_reason.name)
            if block_reason is not None and block_reason.name == "ROLLBACK_TRIGGERED":
                # v3: roll back to the best policy observed so far, not blindly to the
                # menu's first entry (p01 itself misses the SLO in most fault scenarios).
                target = bandit.best_known() if bandit.safe_explore else None
                if target is None:
                    target = 0
                action_idx, chosen_policy = target, policies[target]
                log.info("Rollback target: %s", chosen_policy.id)
                _push_policy(service_a_url, chosen_policy.to_api_payload())

        # ------------------------------------------------------------------
        # 6. Wait for the observation window
        # ------------------------------------------------------------------
        time.sleep(window_seconds)

        # ------------------------------------------------------------------
        # 7. Fetch metrics AFTER
        # ------------------------------------------------------------------
        metrics_after = metrics_client.fetch()
        context_after = metrics_client.to_context_vector(metrics_after, slo_p99_ms=_SLO_P99_MS)

        reward = compute_reward(metrics_after)
        rollback = guardrails.record_reward(reward)

        # ------------------------------------------------------------------
        # 8. Bandit update (skip in shadow mode to avoid contaminating model)
        # ------------------------------------------------------------------
        if not guardrails.in_shadow_mode and not blocked:
            bandit.update(action_idx, context, reward)

        # ------------------------------------------------------------------
        # 9. RL explorer transition
        # ------------------------------------------------------------------
        rl_explorer.observe(context, action_idx, current_fault, context_after)
        current_fault = rl_explorer.adversarial_scenario(context_after, action_idx)

        # ------------------------------------------------------------------
        # 10. Log
        # ------------------------------------------------------------------
        action_logger.log_decision(
            window=window_idx,
            context=context,
            action=action_idx,
            policy_id=chosen_policy.id,
            metrics_before=metrics_before,
            metrics_after=metrics_after,
            reward=reward,
            blocked=blocked,
            block_reason=block_reason,
            rollback=rollback,
            ucb_scores=ucb_scores,
        )

        log.info(
            "Window %4d | p99=%5.0f ms | err=%.3f | raf=%.2f | "
            "reward=%+.3f | policy=%s | blocked=%s | anomaly=%s",
            window_idx,
            metrics_after["p99_ms"],
            metrics_after["error_rate"],
            metrics_after["raf"],
            reward,
            chosen_policy.id,
            blocked,
            is_anomaly,
        )

    log.info("AdaptR4j finished after %d windows", window_idx)
    log.info("Bandit: total_updates=%d  alpha=%.3f", bandit.total_updates, bandit.current_alpha)
    log.info("Guardrails: %s", guardrails.stats)
    log.info("Anomaly detector: %s", anomaly_detector.stats)
    log.info("RL explorer: %s", rl_explorer.stats)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _build_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AdaptR4j online policy engine")
    p.add_argument("--menu",        default="policy-menu.yml")
    p.add_argument("--prometheus",  default="http://localhost:9090")
    p.add_argument("--service-a",   default="http://localhost:8080")
    p.add_argument("--window",      type=int,   default=60,  help="Observation window seconds")
    p.add_argument("--max-windows", type=int,   default=500)
    p.add_argument("--log-file",    default="logs/decisions.jsonl")
    p.add_argument("--alpha",         type=float, default=1.0, help="LinUCB alpha")
    p.add_argument("--shadow",        type=int,   default=30,  help="Shadow-mode windows")
    p.add_argument("--no-guardrails", action="store_true",
                   help="Ablation: disable RAF hard stop and rollback")
    p.add_argument("--safe-explore",  action="store_true",
                   help="v3: stop selecting arms whose recent reward is far below the best arm's")
    p.add_argument("--no-context",    action="store_true",
                   help="Ablation: replace live context with constant ones vector")
    p.add_argument("--fixed-policy",  default=None, metavar="POLICY_ID",
                   help="B3 baseline: always apply this policy_id, bypassing UCB selection")
    p.add_argument("--target-uri",    default="/api/checkout",
                   help="Edge endpoint to use for RAF/throughput queries "
                        "(e.g. /api/checkout-fanout for the S7 fan-out scenario)")
    return p.parse_args()


def main():
    args = _build_args()
    policies   = load_menu(args.menu)
    n_actions  = len(policies)
    n_features = 6

    bandit = LinUCBBandit(n_actions=n_actions, n_features=n_features, alpha=args.alpha,
                          safe_explore=args.safe_explore)
    guardrails = Guardrails(slo_p99_ms=_SLO_P99_MS, shadow_mode_windows=args.shadow,
                            no_guardrails=args.no_guardrails)
    metrics_client   = MetricsClient(prometheus_url=args.prometheus, target_uri=args.target_uri)
    anomaly_detector = AnomalyDetector()
    rl_explorer      = RLExplorer(n_features=n_features)

    with ActionLogger(path=args.log_file) as action_logger:
        run(
            policies=policies,
            bandit=bandit,
            guardrails=guardrails,
            metrics_client=metrics_client,
            anomaly_detector=anomaly_detector,
            rl_explorer=rl_explorer,
            action_logger=action_logger,
            service_a_url=args.service_a,
            window_seconds=args.window,
            max_windows=args.max_windows,
            no_context=args.no_context,
            fixed_policy=args.fixed_policy,
        )


if __name__ == "__main__":
    main()
