package com.adaptr4j.servicea.resilience;

import io.github.resilience4j.bulkhead.Bulkhead;
import io.github.resilience4j.bulkhead.BulkheadConfig;
import io.github.resilience4j.bulkhead.BulkheadFullException;
import io.github.resilience4j.bulkhead.BulkheadRegistry;
import io.github.resilience4j.circuitbreaker.CallNotPermittedException;
import io.github.resilience4j.circuitbreaker.CircuitBreaker;
import io.github.resilience4j.circuitbreaker.CircuitBreakerConfig;
import io.github.resilience4j.circuitbreaker.CircuitBreakerRegistry;
import io.github.resilience4j.retry.Retry;
import io.github.resilience4j.retry.RetryConfig;
import io.github.resilience4j.retry.RetryRegistry;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Instant;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Supplier;

/**
 * Engineering contribution: safe atomic runtime policy swap for Resilience4j in Spring Boot 3.
 *
 * Wraps a single downstream callsite with a Retry → CircuitBreaker → Bulkhead chain.
 * The policy engine calls swap() to change the policy without disrupting in-flight requests.
 *
 * Why this is needed: Spring Cloud refresh does not support Resilience4j dynamic reload
 * in Spring Boot 3 out of the box (GitHub resilience4j/resilience4j#2180). This class
 * provides the missing actuation mechanism as a first-class contribution.
 *
 * Not a @Component: callsites needing multiple independent downstream dependencies
 * (e.g. fan-out to Service B and Service C) need multiple named instances with
 * non-colliding CB/Retry/Bulkhead registry entries. Instantiated via @Bean factory
 * methods in ResilienceConfig instead of component-scanning.
 */
public class PolicyReferenceHolder {

    private static final Logger log = LoggerFactory.getLogger(PolicyReferenceHolder.class);

    private final String instancePrefix;
    private final CircuitBreakerRegistry cbRegistry;
    private final RetryRegistry retryRegistry;
    private final BulkheadRegistry bulkheadRegistry;
    private final Runnable onRetryCallback;
    private final AtomicReference<ActivePolicy> activeRef;
    private final AtomicLong versionCounter = new AtomicLong(0);

    /**
     * Snapshot of the currently-active policy with its live R4J instances.
     */
    public record ActivePolicy(
            PolicyConfig config,
            CircuitBreaker circuitBreaker,
            Retry retry,
            Bulkhead bulkhead,
            long version,
            Instant appliedAt
    ) {}

    public PolicyReferenceHolder(String holderName,
                                 CircuitBreakerRegistry cbRegistry,
                                 RetryRegistry retryRegistry,
                                 BulkheadRegistry bulkheadRegistry,
                                 Runnable onRetryCallback) {
        this.instancePrefix = "adaptr4j-" + holderName + "-";
        this.cbRegistry = cbRegistry;
        this.retryRegistry = retryRegistry;
        this.bulkheadRegistry = bulkheadRegistry;
        this.onRetryCallback = onRetryCallback != null ? onRetryCallback : () -> { };

        PolicyConfig initial = PolicyConfig.defaultFallback();
        this.activeRef = new AtomicReference<>(buildActivePolicy(initial, 0));
        log.info("PolicyReferenceHolder[{}] initialised with policy={}", holderName, initial.id());
    }

    /**
     * Execute a downstream call through the current policy, in Resilience4j's standard
     * order: Retry( CircuitBreaker( Bulkhead( call ) ) ).
     *
     * Earlier versions used Bulkhead( Retry( CircuitBreaker( call ) ) ). With the bulkhead
     * outermost, a request held its bulkhead slot through every retry back-off, so a few
     * failing calls could fill the bulkhead and every other request was rejected.
     */
    public <T> T execute(Supplier<T> downstreamCall) {
        ActivePolicy active = activeRef.get();
        Supplier<T> decorated = Retry.decorateSupplier(active.retry(),
                CircuitBreaker.decorateSupplier(active.circuitBreaker(),
                        Bulkhead.decorateSupplier(active.bulkhead(), downstreamCall)));
        return decorated.get();
    }

    /**
     * Atomically swap to a new policy. Thread-safe; does not drop in-flight requests.
     * Old instances are left alive for 30 seconds so in-flight calls can drain.
     */
    public void swap(PolicyConfig newConfig) {
        long newVersion = versionCounter.incrementAndGet();
        ActivePolicy oldPolicy = activeRef.get();
        ActivePolicy newPolicy = buildActivePolicy(newConfig, newVersion);

        activeRef.set(newPolicy);

        log.info("Policy swap v{} → v{}: {} → {} appliedAt={}",
                oldPolicy.version(), newVersion,
                oldPolicy.config().id(), newConfig.id(),
                newPolicy.appliedAt());
    }

    public ActivePolicy current() {
        return activeRef.get();
    }

    /**
     * Resilience4j rate thresholds are percentages (0-100].  The policy menu and the BO
     * tooling express them as fractions (0.20-1.00); before Nov 2026 those fractions were
     * passed through unchanged, so every menu policy got a 0.2-1 % threshold and its
     * circuit breaker opened on the first error.  Values <= 1.0 are treated as fractions.
     */
    static float asPercent(float threshold) {
        return threshold <= 1.0f ? threshold * 100.0f : threshold;
    }

    // -------------------------------------------------------------------------

    private ActivePolicy buildActivePolicy(PolicyConfig cfg, long version) {
        String instanceName = instancePrefix + version;

        CircuitBreakerConfig cbConfig = CircuitBreakerConfig.custom()
                .slidingWindowSize(cfg.cbSlidingWindowSize())
                .failureRateThreshold(asPercent(cfg.cbFailureRateThreshold()))
                .slowCallRateThreshold(asPercent(cfg.cbSlowCallRateThreshold()))
                .slowCallDurationThreshold(cfg.cbSlowCallDuration())
                .waitDurationInOpenState(cfg.cbWaitDuration())
                .permittedNumberOfCallsInHalfOpenState(cfg.cbPermittedCallsInHalfOpenState())
                .minimumNumberOfCalls(cfg.cbMinimumNumberOfCalls())
                .build();

        // Rejections by our own CB or bulkhead are not retried: retrying them only waits
        // and re-rejects, and adds load exactly when the system is protecting itself.
        RetryConfig.Builder<Object> retryBuilder = RetryConfig.custom()
                .maxAttempts(cfg.retryMaxAttempts())
                .ignoreExceptions(CallNotPermittedException.class, BulkheadFullException.class);
        if (cfg.retryExponentialBackoff()) {
            retryBuilder.intervalFunction(attempt ->
                    (long) (cfg.retryWaitDurationMs() * Math.pow(cfg.retryExponentialMultiplier(), attempt - 1)));
        } else {
            retryBuilder.waitDuration(cfg.retryWaitDuration());
        }
        RetryConfig retryConfig = retryBuilder.build();

        BulkheadConfig bulkheadConfig = BulkheadConfig.custom()
                .maxConcurrentCalls(cfg.bulkheadMaxConcurrentCalls())
                .maxWaitDuration(cfg.bulkheadMaxWaitDuration())
                .build();

        CircuitBreaker cb = cbRegistry.circuitBreaker(instanceName, cbConfig);
        Retry retry = retryRegistry.retry(instanceName, retryConfig);
        Bulkhead bulkhead = bulkheadRegistry.bulkhead(instanceName, bulkheadConfig);

        // RAF numerator: increment the caller-supplied counter on every retry attempt.
        // Each policy swap creates a fresh Retry instance, so this must be (re-)registered
        // here rather than once at construction time.
        retry.getEventPublisher().onRetry(event -> onRetryCallback.run());

        return new ActivePolicy(cfg, cb, retry, bulkhead, version, Instant.now());
    }
}
