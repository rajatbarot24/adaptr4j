"""
No-coordination ablation: CB, Retry, and Bulkhead each controlled by an independent
LinUCB bandit that shares the same 6-dim context vector but updates its own matrices.

The three selections are composed into a complete (CB+Retry+BH) policy per window.
Comparing mean reward against main.py (joint LinUCB) quantifies the joint-selection benefit.

Key hypothesis: independent bandits cannot discover Pareto-optimal cross-dimension
combinations because each bandit credits its own choice for the joint outcome, creating
attribution noise that slows convergence to the true optimum.

Usage:
    python no_coord_main.py --menu policy-menu.yml --shadow 5 --window 60 \
                            --max-windows 80 --log-file logs/decisions-abl-S1-nocoord.jsonl
"""

import argparse
import logging
import signal
import time
from dataclasses import dataclass

import numpy as np

from action_logger import ActionLogger
from bandit import LinUCBBandit
from guardrails import Guardrails
from main import _push_policy, compute_reward
from metrics_client import MetricsClient
from policy_menu import PolicyConfig, load_menu

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("adaptr4j.nocoord")

_SLO_P99_MS = 500.0


# ---------------------------------------------------------------------------
# Dimension config types (frozen so they can be used as set keys)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _CBConfig:
    sliding_window_size: int
    failure_rate_threshold: float
    slow_call_rate_threshold: float
    slow_call_duration_ms: int
    wait_duration_in_open_state_ms: int
    permitted_calls_in_half_open_state: int
    minimum_number_of_calls: int


@dataclass(frozen=True)
class _RetryConfig:
    max_attempts: int
    wait_duration_ms: int
    exponential_backoff: bool
    exponential_multiplier: float


@dataclass(frozen=True)
class _BHConfig:
    max_concurrent_calls: int
    max_wait_duration_ms: int


def extract_dimensions(
    policies: list[PolicyConfig],
) -> tuple[list[_CBConfig], list[_RetryConfig], list[_BHConfig]]:
    """Extract unique CB, Retry, BH parameter sets from the policy menu."""
    cb_seen: set[_CBConfig] = set()
    retry_seen: set[_RetryConfig] = set()
    bh_seen: set[_BHConfig] = set()
    cb_list, retry_list, bh_list = [], [], []

    for p in policies:
        cb = _CBConfig(
            p.cb_sliding_window_size, p.cb_failure_rate_threshold,
            p.cb_slow_call_rate_threshold, p.cb_slow_call_duration_ms,
            p.cb_wait_duration_in_open_state_ms,
            p.cb_permitted_calls_in_half_open_state,
            p.cb_minimum_number_of_calls,
        )
        if cb not in cb_seen:
            cb_seen.add(cb)
            cb_list.append(cb)

        retry = _RetryConfig(
            p.retry_max_attempts, p.retry_wait_duration_ms,
            p.retry_exponential_backoff, p.retry_exponential_multiplier,
        )
        if retry not in retry_seen:
            retry_seen.add(retry)
            retry_list.append(retry)

        bh = _BHConfig(p.bulkhead_max_concurrent_calls, p.bulkhead_max_wait_duration_ms)
        if bh not in bh_seen:
            bh_seen.add(bh)
            bh_list.append(bh)

    return cb_list, retry_list, bh_list


def _compose_payload(
    cb: _CBConfig, retry: _RetryConfig, bh: _BHConfig, policy_id: str,
) -> dict:
    return {
        "id":   policy_id,
        "name": f"nocoord-{policy_id}",
        "cbSlidingWindowSize":             cb.sliding_window_size,
        "cbFailureRateThreshold":          cb.failure_rate_threshold,
        "cbSlowCallRateThreshold":         cb.slow_call_rate_threshold,
        "cbSlowCallDurationMs":            cb.slow_call_duration_ms,
        "cbWaitDurationInOpenStateMs":     cb.wait_duration_in_open_state_ms,
        "cbPermittedCallsInHalfOpenState": cb.permitted_calls_in_half_open_state,
        "cbMinimumNumberOfCalls":          cb.minimum_number_of_calls,
        "retryMaxAttempts":                retry.max_attempts,
        "retryWaitDurationMs":             retry.wait_duration_ms,
        "retryExponentialBackoff":         retry.exponential_backoff,
        "retryExponentialMultiplier":      retry.exponential_multiplier,
        "bulkheadMaxConcurrentCalls":      bh.max_concurrent_calls,
        "bulkheadMaxWaitDurationMs":       bh.max_wait_duration_ms,
    }


# ---------------------------------------------------------------------------
# Control loop
# ---------------------------------------------------------------------------

def run(
    cb_configs: list[_CBConfig],
    retry_configs: list[_RetryConfig],
    bh_configs: list[_BHConfig],
    cb_bandit: LinUCBBandit,
    retry_bandit: LinUCBBandit,
    bh_bandit: LinUCBBandit,
    guardrails: Guardrails,
    metrics_client: MetricsClient,
    action_logger: ActionLogger,
    service_a_url: str,
    window_seconds: int,
    max_windows: int,
) -> None:
    log.info(
        "No-coord starting — CB:%d × Retry:%d × BH:%d = %d combinations, window=%ds",
        len(cb_configs), len(retry_configs), len(bh_configs),
        len(cb_configs) * len(retry_configs) * len(bh_configs),
        window_seconds,
    )

    # Safe rollback payload (first config in each dimension = canonical fallback)
    _safe_payload = _compose_payload(
        cb_configs[0], retry_configs[0], bh_configs[0], "nc-safe"
    )

    _stop = [False]

    def _handler(sig, _frame):
        log.info("Shutdown signal received")
        _stop[0] = True

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)

    for window_idx in range(1, max_windows + 1):
        if _stop[0]:
            break

        # ── 1. Fetch pre-window metrics ──────────────────────────────────
        metrics_before = metrics_client.fetch()
        context = metrics_client.to_context_vector(metrics_before, slo_p99_ms=_SLO_P99_MS)

        # ── 2. Safety guardrail check ────────────────────────────────────
        blocked, block_reason = guardrails.check(metrics_before)

        # ── 3. Independent bandit selections ────────────────────────────
        cb_idx,    _ = cb_bandit.select_action(context)
        retry_idx, _ = retry_bandit.select_action(context)
        bh_idx,    _ = bh_bandit.select_action(context)

        # policy_id encodes all three choices for offline analysis
        policy_id = f"nc-cb{cb_idx}-r{retry_idx}-bh{bh_idx}"
        payload = _compose_payload(
            cb_configs[cb_idx], retry_configs[retry_idx], bh_configs[bh_idx], policy_id,
        )

        # ── 4. Apply policy (unless guardrail blocked) ───────────────────
        if not blocked:
            _push_policy(service_a_url, payload)
        else:
            log.info("Window %d: blocked by %s", window_idx, block_reason.name)
            if block_reason is not None and block_reason.name == "ROLLBACK_TRIGGERED":
                _push_policy(service_a_url, _safe_payload)

        # ── 5. Wait ──────────────────────────────────────────────────────
        time.sleep(window_seconds)

        # ── 6. Fetch post-window metrics ─────────────────────────────────
        metrics_after = metrics_client.fetch()
        reward = compute_reward(metrics_after)
        rollback = guardrails.record_reward(reward)

        # ── 7. Update all three bandits with the same reward ─────────────
        # Each bandit credits its own choice; interaction effects are invisible.
        if not guardrails.in_shadow_mode and not blocked:
            cb_bandit.update(cb_idx,    context, reward)
            retry_bandit.update(retry_idx, context, reward)
            bh_bandit.update(bh_idx,    context, reward)

        # ── 8. Log ───────────────────────────────────────────────────────
        action_logger.log_decision(
            window=window_idx,
            context=context,
            action=cb_idx,          # primary slot kept for schema compatibility
            policy_id=policy_id,    # encodes all three indices
            metrics_before=metrics_before,
            metrics_after=metrics_after,
            reward=reward,
            blocked=blocked,
            block_reason=block_reason,
            rollback=rollback,
            ucb_scores=None,
        )

        log.info(
            "Window %4d | p99=%5.0f ms | err=%.3f | raf=%.2f | "
            "reward=%+.3f | policy=%s | blocked=%s",
            window_idx,
            metrics_after["p99_ms"],
            metrics_after["error_rate"],
            metrics_after["raf"],
            reward,
            policy_id,
            blocked,
        )

    log.info("No-coord finished after %d windows", window_idx)
    log.info(
        "CB updates=%d  Retry updates=%d  BH updates=%d",
        cb_bandit.total_updates, retry_bandit.total_updates, bh_bandit.total_updates,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _build_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AdaptR4j no-coordination ablation")
    p.add_argument("--menu",        default="policy-menu.yml")
    p.add_argument("--prometheus",  default="http://localhost:9090")
    p.add_argument("--service-a",   default="http://localhost:8080")
    p.add_argument("--window",      type=int, default=60)
    p.add_argument("--max-windows", type=int, default=80)
    p.add_argument("--log-file",    default="logs/decisions-abl-nocoord.jsonl")
    p.add_argument("--shadow",      type=int, default=5)
    p.add_argument("--alpha",       type=float, default=1.0)
    return p.parse_args()


def main() -> None:
    args = _build_args()
    policies = load_menu(args.menu)
    cb_configs, retry_configs, bh_configs = extract_dimensions(policies)

    log.info(
        "Extracted: %d CB × %d Retry × %d BH from %d menu policies",
        len(cb_configs), len(retry_configs), len(bh_configs), len(policies),
    )

    n_features = 6
    cb_bandit    = LinUCBBandit(n_actions=len(cb_configs),    n_features=n_features, alpha=args.alpha)
    retry_bandit = LinUCBBandit(n_actions=len(retry_configs), n_features=n_features, alpha=args.alpha)
    bh_bandit    = LinUCBBandit(n_actions=len(bh_configs),    n_features=n_features, alpha=args.alpha)

    guardrails     = Guardrails(slo_p99_ms=_SLO_P99_MS, shadow_mode_windows=args.shadow)
    metrics_client = MetricsClient(prometheus_url=args.prometheus)

    with ActionLogger(path=args.log_file) as action_logger:
        run(
            cb_configs=cb_configs,
            retry_configs=retry_configs,
            bh_configs=bh_configs,
            cb_bandit=cb_bandit,
            retry_bandit=retry_bandit,
            bh_bandit=bh_bandit,
            guardrails=guardrails,
            metrics_client=metrics_client,
            action_logger=action_logger,
            service_a_url=args.service_a,
            window_seconds=args.window,
            max_windows=args.max_windows,
        )


if __name__ == "__main__":
    main()
