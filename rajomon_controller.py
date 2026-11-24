"""
Rajomon-inspired admission controller for AdaptR4j evaluation.

Implements Rajomon's core token-bucket mechanism (Xing et al., NSDI '25) at the
application-library layer using Resilience4j Bulkhead as the token bucket.

Key design:
  - CB held at 90% failure threshold (effectively disabled -- not the control variable)
  - Retry = 1 attempt only (no amplification -- Rajomon does not manage retries)
  - Bulkhead = dynamically controlled maxConcurrentCalls (the admission lever)

Control law:
  - p99 > SLO_MS: reduce maxConcurrentCalls by 20% (token price rises → fewer admitted)
  - p99 < SLO_MS/2 for HEALTHY_STREAK consecutive windows: restore 10%
  - Bounded [MIN_CONC, MAX_CONC]

Outputs the same JSONL schema as main.py so finalize_paper.py and stats.py work unchanged.

Usage:
    python rajomon_controller.py --shadow 5 --window 60 --max-windows 80 \\
                                 --log-file logs/decisions-bl-S1-rajomon.jsonl
"""

import argparse
import logging
import signal
import time

import requests

from action_logger import ActionLogger
from guardrails import BlockReason
from main import compute_reward
from metrics_client import MetricsClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("adaptr4j.rajomon")

_SLO_P99_MS           = 500.0
_INITIAL_CONC         = 50      # start fully open (matches W2 user count)
_MIN_CONC             = 3       # hard floor -- always admit a few probes
_MAX_CONC             = 50      # ceiling -- never exceed initial
_STEP_DOWN            = 0.80    # multiply on SLO violation
_STEP_UP              = 1.10    # multiply on recovery
_HEALTHY_STREAK_NEEDED = 3      # consecutive sub-SLO/2 windows before restoring


# ---------------------------------------------------------------------------
# Policy payload builder
# ---------------------------------------------------------------------------

def _build_payload(concurrent: int) -> dict:
    """
    Full PolicyConfig-compatible JSON with CB near-disabled and no retries.
    Only bulkheadMaxConcurrentCalls varies; everything else is fixed.
    """
    return {
        "id":   f"rajomon-bh{concurrent:03d}",
        "name": f"rajomon-admission-{concurrent}",
        # CB: 90% failure threshold -- trips only under extreme fault injection,
        # not under normal S1/S2 error rates (4% / 10%). Effectively disabled.
        "cbSlidingWindowSize":              20,
        "cbFailureRateThreshold":           90.0,
        "cbSlowCallRateThreshold":         100.0,
        "cbSlowCallDurationMs":           10000,
        "cbWaitDurationInOpenStateMs":    60000,
        "cbPermittedCallsInHalfOpenState":    5,
        "cbMinimumNumberOfCalls":            10,
        # Retry: single attempt -- no amplification, Rajomon sheds load instead
        "retryMaxAttempts":                   1,
        "retryWaitDurationMs":              100,
        "retryExponentialBackoff":          False,
        "retryExponentialMultiplier":         1.0,
        # Bulkhead: the admission control lever
        "bulkheadMaxConcurrentCalls":  concurrent,
        "bulkheadMaxWaitDurationMs":          0,  # immediate rejection when full
    }


def _push(url: str, payload: dict) -> bool:
    try:
        r = requests.post(f"{url}/policy", json=payload, timeout=5.0)
        r.raise_for_status()
        return True
    except Exception as exc:
        log.error("Policy push failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Main control loop
# ---------------------------------------------------------------------------

def run(
    metrics_client: MetricsClient,
    action_logger: ActionLogger,
    service_a_url: str,
    window_seconds: int,
    max_windows: int,
    shadow_windows: int,
) -> None:
    log.info(
        "Rajomon controller starting — window=%ds max_windows=%d shadow=%d "
        "initial_conc=%d step_down=%.0f%% step_up=%.0f%%",
        window_seconds, max_windows, shadow_windows,
        _INITIAL_CONC, (1 - _STEP_DOWN) * 100, (_STEP_UP - 1) * 100,
    )

    _stop = [False]
    def _handler(sig, _):
        log.info("Shutdown signal received")
        _stop[0] = True
    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)

    current_concurrent = _INITIAL_CONC
    healthy_streak = 0

    # Apply the baseline Rajomon policy once before shadow mode so Service A
    # runs with 1-retry / high-CB-threshold settings throughout.
    log.info("Applying initial Rajomon policy (bh=%d)", current_concurrent)
    _push(service_a_url, _build_payload(current_concurrent))
    time.sleep(2)

    for window_idx in range(1, max_windows + 1):
        if _stop[0]:
            break

        in_shadow = (window_idx <= shadow_windows)

        # --- fetch metrics before adjustment --------------------------------
        metrics_before = metrics_client.fetch()
        context = metrics_client.to_context_vector(metrics_before, slo_p99_ms=_SLO_P99_MS)

        # --- reactive control (post-shadow only) ----------------------------
        if not in_shadow:
            p99 = metrics_before["p99_ms"]

            if p99 > _SLO_P99_MS:
                new_conc = max(_MIN_CONC, int(current_concurrent * _STEP_DOWN))
                if new_conc != current_concurrent:
                    log.info("SLO violated p99=%.0f ms → conc %d → %d",
                             p99, current_concurrent, new_conc)
                current_concurrent = new_conc
                healthy_streak = 0

            elif p99 < _SLO_P99_MS / 2.0:
                healthy_streak += 1
                if healthy_streak >= _HEALTHY_STREAK_NEEDED:
                    new_conc = min(_MAX_CONC, int(current_concurrent * _STEP_UP))
                    if new_conc != current_concurrent:
                        log.info("Recovery p99=%.0f ms streak=%d → conc %d → %d",
                                 p99, healthy_streak, current_concurrent, new_conc)
                    current_concurrent = new_conc
            # else: p99 in [SLO/2, SLO] -- hold current setting

            _push(service_a_url, _build_payload(current_concurrent))

        # --- wait observation window ----------------------------------------
        time.sleep(window_seconds)

        # --- fetch metrics after --------------------------------------------
        metrics_after = metrics_client.fetch()
        reward = compute_reward(metrics_after)

        # --- log (same schema as main.py) -----------------------------------
        action_logger.log_decision(
            window=window_idx,
            context=context,
            action=current_concurrent,
            policy_id=_build_payload(current_concurrent)["id"],
            metrics_before=metrics_before,
            metrics_after=metrics_after,
            reward=reward,
            blocked=in_shadow,
            block_reason=BlockReason.SHADOW_MODE if in_shadow else None,
            rollback=False,
            ucb_scores=None,
        )

        log.info(
            "Window %4d | p99=%5.0f ms | err=%.3f | raf=%.2f | "
            "reward=%+.3f | conc=%3d | shadow=%s",
            window_idx,
            metrics_after["p99_ms"],
            metrics_after["error_rate"],
            metrics_after["raf"],
            reward,
            current_concurrent,
            in_shadow,
        )

    log.info("Rajomon controller finished after %d windows", window_idx)
    log.info("Rajomon finished")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="Rajomon-inspired admission controller")
    p.add_argument("--prometheus",  default="http://localhost:9090")
    p.add_argument("--service-a",   default="http://localhost:8080")
    p.add_argument("--window",      type=int, default=60)
    p.add_argument("--max-windows", type=int, default=80)
    p.add_argument("--log-file",    default="logs/decisions-bl-S1-rajomon.jsonl")
    p.add_argument("--shadow",      type=int, default=5)
    args = p.parse_args()

    metrics_client = MetricsClient(prometheus_url=args.prometheus)

    with ActionLogger(path=args.log_file) as action_logger:
        run(
            metrics_client=metrics_client,
            action_logger=action_logger,
            service_a_url=args.service_a,
            window_seconds=args.window,
            max_windows=args.max_windows,
            shadow_windows=args.shadow,
        )


if __name__ == "__main__":
    main()
