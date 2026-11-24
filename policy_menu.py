"""
Loads the 30-policy menu from policy-menu.yml.
Each policy is a complete CB + Retry + Bulkhead configuration for the bandit's action space.
"""

import yaml
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PolicyConfig:
    id: str
    name: str
    # CircuitBreaker
    cb_sliding_window_size: int
    cb_failure_rate_threshold: float
    cb_slow_call_rate_threshold: float
    cb_slow_call_duration_ms: int
    cb_wait_duration_in_open_state_ms: int
    cb_permitted_calls_in_half_open_state: int
    cb_minimum_number_of_calls: int
    # Retry
    retry_max_attempts: int
    retry_wait_duration_ms: int
    retry_exponential_backoff: bool
    retry_exponential_multiplier: float
    # Bulkhead
    bulkhead_max_concurrent_calls: int
    bulkhead_max_wait_duration_ms: int

    def to_api_payload(self) -> dict:
        """Serialize to the JSON body expected by Service A's POST /policy endpoint."""
        return {
            "id": self.id,
            "name": self.name,
            "cbSlidingWindowSize": self.cb_sliding_window_size,
            "cbFailureRateThreshold": self.cb_failure_rate_threshold,
            "cbSlowCallRateThreshold": self.cb_slow_call_rate_threshold,
            "cbSlowCallDurationMs": self.cb_slow_call_duration_ms,
            "cbWaitDurationInOpenStateMs": self.cb_wait_duration_in_open_state_ms,
            "cbPermittedCallsInHalfOpenState": self.cb_permitted_calls_in_half_open_state,
            "cbMinimumNumberOfCalls": self.cb_minimum_number_of_calls,
            "retryMaxAttempts": self.retry_max_attempts,
            "retryWaitDurationMs": self.retry_wait_duration_ms,
            "retryExponentialBackoff": self.retry_exponential_backoff,
            "retryExponentialMultiplier": self.retry_exponential_multiplier,
            "bulkheadMaxConcurrentCalls": self.bulkhead_max_concurrent_calls,
            "bulkheadMaxWaitDurationMs": self.bulkhead_max_wait_duration_ms,
        }


def load_menu(path: str = "policy-menu.yml") -> list[PolicyConfig]:
    with open(Path(path)) as f:
        raw = yaml.safe_load(f)
    policies = []
    for p in raw["policies"]:
        policies.append(PolicyConfig(
            id=p["id"],
            name=p["name"],
            cb_sliding_window_size=p["cb"]["sliding_window_size"],
            cb_failure_rate_threshold=p["cb"]["failure_rate_threshold"],
            cb_slow_call_rate_threshold=p["cb"].get("slow_call_rate_threshold", 100.0),
            cb_slow_call_duration_ms=p["cb"].get("slow_call_duration_ms", 1000),
            cb_wait_duration_in_open_state_ms=p["cb"]["wait_duration_in_open_state_ms"],
            cb_permitted_calls_in_half_open_state=p["cb"].get("permitted_calls_in_half_open_state", 5),
            cb_minimum_number_of_calls=p["cb"].get("minimum_number_of_calls", 10),
            retry_max_attempts=p["retry"]["max_attempts"],
            retry_wait_duration_ms=p["retry"]["wait_duration_ms"],
            retry_exponential_backoff=p["retry"].get("exponential_backoff", False),
            retry_exponential_multiplier=p["retry"].get("exponential_multiplier", 2.0),
            bulkhead_max_concurrent_calls=p["bulkhead"]["max_concurrent_calls"],
            bulkhead_max_wait_duration_ms=p["bulkhead"].get("max_wait_duration_ms", 0),
        ))
    return policies
