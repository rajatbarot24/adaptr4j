"""
AdaptR4j load generation — four workload profiles (W1–W4).

Run a specific workload:
    locust -f locust/workloads.py WorkloadW2 \
           --host http://localhost:8080 \
           --users 50 --spawn-rate 5 --run-time 10m --headless

Workload definitions:
  W1 — Steady low load     (10 users, 1 req/s each)
  W2 — Moderate sustained  (50 users, 0.5-2 s wait)
  W3 — Spike               (200 users, 0.1-0.5 s wait, burst)
  W4 — Diurnal sine wave   (50-250 users, 2-min period, 15-min run)
       Run W4 WITHOUT --users/--spawn-rate; DiurnalShape controls user count.
       Command: locust -f locust/workloads.py WorkloadW4 \
                       --host http://localhost:8080 --run-time 15m --headless \
                       --csv logs/locust-s1-w4
"""

import math
import os
import random
import time

from locust import HttpUser, LoadTestShape, between, constant, events, task
from locust.contrib.fasthttp import FastHttpUser


# ---------------------------------------------------------------------------
# Base user — single endpoint target
# ---------------------------------------------------------------------------

class _BaseUser(HttpUser):
    abstract = True

    @task
    def checkout(self):
        with self.client.get("/api/checkout", catch_response=True) as resp:
            if resp.status_code >= 500:
                resp.failure(f"HTTP {resp.status_code}")
            elif resp.elapsed.total_seconds() > 1.0:
                resp.failure(f"Slow: {resp.elapsed.total_seconds():.2f}s")
            else:
                resp.success()


# ---------------------------------------------------------------------------
# W1 — Steady low load
# ---------------------------------------------------------------------------
class WorkloadW1(_BaseUser):
    """10 users, 1 req/s each — baseline / shadow-mode warm-up."""
    wait_time = constant(1)


# ---------------------------------------------------------------------------
# W2 — Moderate sustained load
# ---------------------------------------------------------------------------
class WorkloadW2(_BaseUser):
    """50 users, random 0.5–2 s between requests."""
    wait_time = between(0.5, 2.0)


# ---------------------------------------------------------------------------
# W3 — Traffic spike (simulate flash crowd)
# ---------------------------------------------------------------------------
class WorkloadW3(_BaseUser):
    """200 users, minimal wait — spike scenario."""
    wait_time = between(0.1, 0.5)

    @task(3)
    def checkout_burst(self):
        """Fire checkout 3× more frequently during spike."""
        self.checkout()

    @task(1)
    def health_poll(self):
        self.client.get("/api/health")


# ---------------------------------------------------------------------------
# W4 — Diurnal (sine-wave load, controlled by DiurnalShape)
# ---------------------------------------------------------------------------
class WorkloadW4(_BaseUser):
    """User behaviour for W4 diurnal runs. DiurnalShape controls the user count."""
    wait_time = between(0.3, 1.0)


# ---------------------------------------------------------------------------
# S7 fan-out generality — hits /api/checkout-fanout instead of /api/checkout
# ---------------------------------------------------------------------------
class WorkloadFanoutW2(HttpUser):
    """50 users, random 0.5-2s wait, hitting the fan-out endpoint
    (Service A calls Service B and Service C concurrently per request)."""
    wait_time = between(0.5, 2.0)

    @task
    def checkout_fanout(self):
        with self.client.get("/api/checkout-fanout", catch_response=True) as resp:
            if resp.status_code >= 500:
                resp.failure(f"HTTP {resp.status_code}")
            elif resp.elapsed.total_seconds() > 1.0:
                resp.failure(f"Slow: {resp.elapsed.total_seconds():.2f}s")
            else:
                resp.success()


class _DiurnalShape(LoadTestShape):
    """Sinusoidal load: 50-250 users, 2-minute period.

    Locust applies ANY LoadTestShape defined at module level to EVERY run of this
    file and silently ignores --users/--spawn-rate.  Until Nov 2026 this class was
    always defined, so every run after it was added (13 Jun 2026) used the diurnal
    wave instead of the requested workload.  It is now only exported when
    ADAPTR4J_SHAPE=diurnal is set (used by W4 runs only).
    """
    abstract = True
    total_time = 18000  # 5 hours — always outlasts any policy engine run
    min_users  = 50
    max_users  = 250
    period     = 120    # 2-minute sine period

    def tick(self):
        t = self.get_run_time()
        if t >= self.total_time:
            return None
        amplitude  = (self.max_users - self.min_users) / 2
        center     = (self.max_users + self.min_users) / 2
        users      = max(1, int(center + amplitude * math.sin(2 * math.pi * t / self.period)))
        spawn_rate = min(users, 50)
        return users, spawn_rate


if os.environ.get("ADAPTR4J_SHAPE") == "diurnal":
    class DiurnalShape(_DiurnalShape):
        abstract = False


# ---------------------------------------------------------------------------
# Event hooks — log workload metadata for experiment tagging
# ---------------------------------------------------------------------------

@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    print(f"[AdaptR4j] Locust test started — host={environment.host}")


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    stats = environment.stats.total
    print(
        f"[AdaptR4j] Test finished — "
        f"requests={stats.num_requests} "
        f"failures={stats.num_failures} "
        f"p99={stats.get_response_time_percentile(0.99):.0f}ms "
        f"rps={stats.current_rps:.1f}"
    )
