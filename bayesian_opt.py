"""
Component 1: Bayesian Optimisation — offline policy menu generation.

Runs *before* the live experiment to discover the 30-policy menu that covers
the Pareto front of (latency, throughput, reliability) across the expected
fault-profile space.

Algorithm: Gaussian Process surrogate + Expected Improvement acquisition,
using scikit-optimize (skopt).

Reference: Brochu et al., "A Tutorial on Bayesian Optimization of Expensive
Cost Functions," arXiv 2010.09876.

Usage (standalone):
    python bayesian_opt.py --n-calls 120 --out policy-menu.yml
"""

import argparse
import logging
import math
import time
from typing import Callable

import numpy as np
import yaml
from skopt import gp_minimize
from skopt.space import Integer, Real

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Search space — mirrors PolicyConfig fields in policy_menu.py
# ---------------------------------------------------------------------------
SPACE = [
    Integer(10,  100,  name="cb_sliding_window_size"),
    Real(  0.20, 0.80, name="cb_failure_rate_threshold"),
    Real(  0.50, 1.00, name="cb_slow_call_rate_threshold"),
    Integer(500, 3000, name="cb_slow_call_duration_ms"),
    Integer(500, 10000,name="cb_wait_duration_in_open_state_ms"),
    Integer(2,   10,   name="cb_permitted_calls_in_half_open_state"),
    Integer(5,   30,   name="cb_minimum_number_of_calls"),
    Integer(1,   5,    name="retry_max_attempts"),
    Integer(100, 2000, name="retry_wait_duration_ms"),
    Integer(10,  100,  name="bulkhead_max_concurrent_calls"),
]

DIMENSION_NAMES = [d.name for d in SPACE]


# ---------------------------------------------------------------------------
# Objective
# ---------------------------------------------------------------------------

def _simulate_objective(params: list, simulator: Callable | None = None) -> float:
    """
    Black-box objective evaluated during BO.

    In real use `simulator` calls Service A → Service B via the REST API and
    reads back p99 + error_rate from Prometheus.  For offline/unit use it
    falls back to a cheap analytical proxy.

    Returns a *cost* (BO minimises), defined as:
        cost = w_lat * (p99_ms / SLO_P99) + w_err * error_rate + w_raf * raf
    """
    if simulator is not None:
        return simulator(dict(zip(DIMENSION_NAMES, params)))

    # Analytical proxy (only for unit tests / CI)
    cb_window   = params[0]
    failure_thr = params[1]
    retry_att   = params[7]
    bulkhead    = params[9]

    # Heuristic: tighter CB → less latency bleed; more retries → more RAF
    latency_cost  = 1.0 / math.log1p(cb_window)
    raf_cost      = (retry_att - 1) * 0.15
    throughput_ok = min(bulkhead, 60) / 60.0
    error_penalty = max(0.0, 0.5 - failure_thr) * 2.0

    return latency_cost + raf_cost - throughput_ok + error_penalty


# ---------------------------------------------------------------------------
# Main optimisation loop
# ---------------------------------------------------------------------------

class BayesianPolicyOptimiser:
    """
    Wraps skopt.gp_minimize to discover the policy menu offline.

    After `run()` the `results_` attribute holds the raw skopt result, and
    `top_k_policies()` returns the best-K unique configurations ready to be
    written to policy-menu.yml.
    """

    def __init__(
        self,
        n_calls: int = 120,
        n_initial_points: int = 20,
        random_state: int = 42,
        simulator: Callable | None = None,
    ):
        self.n_calls = n_calls
        self.n_initial_points = n_initial_points
        self.random_state = random_state
        self.simulator = simulator
        self.results_ = None

    def run(self) -> None:
        log.info("Starting BO with %d calls (%d random)", self.n_calls, self.n_initial_points)
        t0 = time.perf_counter()
        self.results_ = gp_minimize(
            func=lambda p: _simulate_objective(p, self.simulator),
            dimensions=SPACE,
            n_calls=self.n_calls,
            n_initial_points=self.n_initial_points,
            acq_func="EI",
            random_state=self.random_state,
            verbose=False,
        )
        elapsed = time.perf_counter() - t0
        log.info("BO finished in %.1f s — best cost=%.4f", elapsed, self.results_.fun)

    def top_k_policies(self, k: int = 30) -> list[dict]:
        """
        Return the k lowest-cost parameter sets, deduplicated by config hash.
        """
        if self.results_ is None:
            raise RuntimeError("Call run() before top_k_policies()")

        # Sort all evaluated points by cost
        pairs = sorted(zip(self.results_.func_vals, self.results_.x_iters))
        seen, configs = set(), []
        for _cost, params in pairs:
            key = tuple(round(p, 3) if isinstance(p, float) else p for p in params)
            if key in seen:
                continue
            seen.add(key)
            configs.append(dict(zip(DIMENSION_NAMES, params)))
            if len(configs) == k:
                break
        return configs


# ---------------------------------------------------------------------------
# YAML serialisation
# ---------------------------------------------------------------------------

def configs_to_yaml(configs: list[dict], path: str = "policy-menu.yml") -> None:
    """Write discovered policies to policy-menu.yml."""
    policies = []
    for idx, cfg in enumerate(configs):
        policies.append({
            "id":   f"p{idx+1:02d}",
            "name": f"bo-policy-{idx+1:02d}",
            "cb": {
                "sliding_window_size":           int(cfg["cb_sliding_window_size"]),
                "failure_rate_threshold":        round(float(cfg["cb_failure_rate_threshold"]), 2),
                "slow_call_rate_threshold":      round(float(cfg["cb_slow_call_rate_threshold"]), 2),
                "slow_call_duration_ms":         int(cfg["cb_slow_call_duration_ms"]),
                "wait_duration_in_open_state_ms":int(cfg["cb_wait_duration_in_open_state_ms"]),
                "permitted_calls_in_half_open_state": int(cfg["cb_permitted_calls_in_half_open_state"]),
                "minimum_number_of_calls":       int(cfg["cb_minimum_number_of_calls"]),
            },
            "retry": {
                "max_attempts":         int(cfg["retry_max_attempts"]),
                "wait_duration_ms":     int(cfg["retry_wait_duration_ms"]),
                "exponential_backoff":  False,
                "exponential_multiplier": 2.0,
            },
            "bulkhead": {
                "max_concurrent_calls": int(cfg["bulkhead_max_concurrent_calls"]),
                "max_wait_duration_ms": 0,
            },
        })

    with open(path, "w", encoding="utf-8") as fh:
        yaml.dump({"policies": policies}, fh, default_flow_style=False, sort_keys=False)
    log.info("Wrote %d policies to %s", len(policies), path)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Offline BO policy menu generation")
    parser.add_argument("--n-calls", type=int, default=120)
    parser.add_argument("--n-init",  type=int, default=20)
    parser.add_argument("--top-k",   type=int, default=30)
    parser.add_argument("--seed",    type=int, default=42)
    parser.add_argument("--out",     default="policy-menu.yml")
    args = parser.parse_args()

    opt = BayesianPolicyOptimiser(
        n_calls=args.n_calls,
        n_initial_points=args.n_init,
        random_state=args.seed,
    )
    opt.run()
    configs = opt.top_k_policies(k=args.top_k)
    configs_to_yaml(configs, path=args.out)


if __name__ == "__main__":
    _main()
