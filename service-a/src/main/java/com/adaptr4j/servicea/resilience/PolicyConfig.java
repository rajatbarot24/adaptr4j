package com.adaptr4j.servicea.resilience;

import java.time.Duration;

/**
 * Immutable snapshot of a complete Resilience4j policy for one callsite.
 * Each PolicyConfig maps to one row in policy-menu.yml and one action in the bandit.
 */
public record PolicyConfig(
        String id,
        String name,

        // CircuitBreaker knobs
        int cbSlidingWindowSize,
        float cbFailureRateThreshold,
        float cbSlowCallRateThreshold,
        int cbSlowCallDurationMs,
        int cbWaitDurationInOpenStateMs,
        int cbPermittedCallsInHalfOpenState,
        int cbMinimumNumberOfCalls,

        // Retry knobs
        int retryMaxAttempts,
        int retryWaitDurationMs,
        boolean retryExponentialBackoff,
        double retryExponentialMultiplier,

        // Bulkhead knobs
        int bulkheadMaxConcurrentCalls,
        int bulkheadMaxWaitDurationMs
) {

    public Duration cbWaitDuration() {
        return Duration.ofMillis(cbWaitDurationInOpenStateMs);
    }

    public Duration retryWaitDuration() {
        return Duration.ofMillis(retryWaitDurationMs);
    }

    public Duration bulkheadMaxWaitDuration() {
        return Duration.ofMillis(bulkheadMaxWaitDurationMs);
    }

    public Duration cbSlowCallDuration() {
        return Duration.ofMillis(cbSlowCallDurationMs);
    }

    /** Balanced default used as the safe fallback when guardrails trigger rollback. */
    public static PolicyConfig defaultFallback() {
        return new PolicyConfig(
                "P10", "balanced-fallback",
                50, 50.0f, 100.0f, 1000, 60000, 5, 10,
                3, 500, true, 2.0,
                40, 0
        );
    }
}
