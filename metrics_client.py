"""
Prometheus HTTP API client for the AdaptR4j policy engine.

Reads the current observation window metrics from Prometheus:
  - p99 latency (adaptr4j.downstream.call.duration)
  - error rate (adaptr4j.downstream.error.total / total calls)
  - RAF — Retry Amplification Factor (adaptr4j.downstream.retry.total / incoming requests)
  - circuit-breaker state (resilience4j_circuitbreaker_state)

Reference for instant-query API: https://prometheus.io/docs/prometheus/latest/querying/api/
"""

import logging
import time

import numpy as np
import requests

log = logging.getLogger(__name__)

# PromQL expressions used by the controller.
# Rate windows match the observation window (60 s default).
#
# All cross-metric divisions and the throughput query wrap each side in sum(...)
# before combining. This is required for two independent reasons:
#  1. Spring's auto-instrumented http_server_requests_seconds_count carries extra
#     labels (status, outcome, exception, error) that split it into multiple time
#     series per uri. Without sum(), Prometheus's default one-to-one vector
#     matching cannot pair a single-series numerator against these multiple
#     differently-labelled denominator series and silently returns an empty
#     result (which previously caused RAF to always fall back to its default).
#  2. For the fan-out scenario (S7/S8), our own custom metrics carry a "target"
#     tag (b/c) per downstream leg. Without sum(), queries return one row per
#     target instead of a combined cross-leg signal, and _query()'s "take the
#     first row" extraction would silently pick only one leg's reading.
# histogram_quantile aggregates by "le" via `sum(...) by (le)`, which preserves
# the bucket dimension needed for the quantile while collapsing target/status/etc.
def _build_queries(target_uri: str) -> dict:
    return {
        # Latency is measured at the EDGE (the request the user made), not on the
        # downstream calls: edge latency includes any queueing inside Service A, which
        # downstream timers cannot see (this hid multi-second fan-out queueing).
        "p99_ms": (
            'histogram_quantile(0.99, sum(rate('
            f'http_server_requests_seconds_bucket{{uri="{target_uri}"}}[60s])) by (le)) * 1000'
        ),
        "p50_ms": (
            'histogram_quantile(0.50, sum(rate('
            f'http_server_requests_seconds_bucket{{uri="{target_uri}"}}[60s])) by (le)) * 1000'
        ),
        "error_rate": (
            'sum(rate(adaptr4j_downstream_error_total[60s])) / '
            '(sum(rate(adaptr4j_downstream_call_duration_seconds_count[60s])) + 1e-9)'
        ),
        "raf": (
            'sum(rate(adaptr4j_downstream_retry_total[60s])) / '
            f'(sum(rate(http_server_requests_seconds_count{{'
            f'uri="{target_uri}"}}[60s])) + 1e-9)'
        ),
        "cb_open": (
            'max(resilience4j_circuitbreaker_state{state="open"})'
        ),
        "throughput_rps": (
            f'sum(rate(http_server_requests_seconds_count{{uri="{target_uri}"}}[60s]))'
        ),
    }


class MetricsClient:
    """Thin wrapper around the Prometheus instant-query API."""

    def __init__(self, prometheus_url: str = "http://localhost:9090", timeout: float = 5.0,
                 target_uri: str = "/api/checkout"):
        self.base_url = prometheus_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._queries = _build_queries(target_uri)

    def _query(self, promql: str) -> float | None:
        """Execute a Prometheus instant query and return the first scalar value."""
        try:
            resp = self._session.get(
                f"{self.base_url}/api/v1/query",
                params={"query": promql, "time": time.time()},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("data", {}).get("result", [])
            if not results:
                return None
            return float(results[0]["value"][1])
        except Exception as exc:
            log.warning("Prometheus query failed (%s): %s", promql[:60], exc)
            return None

    def fetch(self) -> dict:
        """
        Fetch all metrics for the current window.
        Missing / NaN values are replaced with safe defaults so the caller
        never has to guard against None.
        """
        raw = {key: self._query(q) for key, q in self._queries.items()}

        return {
            "p99_ms":         _coerce(raw["p99_ms"],         default=0.0),
            "p50_ms":         _coerce(raw["p50_ms"],         default=0.0),
            "error_rate":     _coerce(raw["error_rate"],     default=0.0),
            "raf":            _coerce(raw["raf"],            default=1.0),
            "cb_open":        _coerce(raw["cb_open"],        default=0.0),
            "throughput_rps": _coerce(raw["throughput_rps"], default=0.0),
        }

    def to_context_vector(self, metrics: dict, slo_p99_ms: float = 500.0) -> np.ndarray:
        """
        Build the 6-dimensional context vector fed to the LinUCB bandit.

        Features (all normalised to roughly [0, 1]):
          0  p99_ms / slo_p99_ms
          1  p50_ms / slo_p99_ms
          2  error_rate            (already 0-1)
          3  min(raf, 5) / 5       (cap at 5× amplification)
          4  cb_open               (binary 0/1)
          5  min(throughput_rps, 200) / 200
        """
        return np.array([
            metrics["p99_ms"]         / slo_p99_ms,
            metrics["p50_ms"]         / slo_p99_ms,
            metrics["error_rate"],
            min(metrics["raf"], 5.0)  / 5.0,
            float(metrics["cb_open"] > 0.5),
            min(metrics["throughput_rps"], 200.0) / 200.0,
        ], dtype=np.float64)


def _coerce(value: float | None, default: float) -> float:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return default
    return float(value)
