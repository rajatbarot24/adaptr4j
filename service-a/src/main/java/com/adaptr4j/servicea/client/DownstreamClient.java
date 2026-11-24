package com.adaptr4j.servicea.client;

import com.adaptr4j.servicea.resilience.PolicyReferenceHolder;
import io.github.resilience4j.bulkhead.BulkheadFullException;
import io.github.resilience4j.circuitbreaker.CallNotPermittedException;
import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Timer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.web.reactive.function.client.WebClient;

import java.util.Map;

/**
 * Calls a downstream service through its PolicyReferenceHolder (CB → Retry → Bulkhead chain).
 * Records end-to-end latency and retry counts for Prometheus.
 *
 * Not a @Service: callsites needing multiple independent downstream dependencies
 * (fan-out to Service B and Service C) need multiple named instances, each wired
 * to a different PolicyReferenceHolder and base URL. Instantiated via @Bean factory
 * methods in ResilienceConfig.
 *
 * Metrics use the SAME metric names regardless of target (with a "target" tag for
 * per-leg breakdown), so existing PromQL queries that aggregate by metric name
 * (see metrics_client.py) automatically produce a combined signal across all
 * downstream legs without any policy-engine changes.
 */
public class DownstreamClient {

    private static final Logger log = LoggerFactory.getLogger(DownstreamClient.class);

    private final PolicyReferenceHolder policyHolder;
    private final WebClient webClient;
    private final Timer callTimer;
    private final Counter errorCounter;
    private final String target;
    private final MeterRegistry meterRegistry;

    public DownstreamClient(String target,
                            PolicyReferenceHolder policyHolder,
                            WebClient.Builder webClientBuilder,
                            MeterRegistry meterRegistry,
                            String baseUrl) {
        this.target = target;
        this.meterRegistry = meterRegistry;
        this.policyHolder = policyHolder;
        this.webClient = webClientBuilder.baseUrl(baseUrl).build();
        this.callTimer = Timer.builder("adaptr4j.downstream.call.duration")
                .description("End-to-end downstream call latency")
                .tag("target", target)
                .register(meterRegistry);
        this.errorCounter = Counter.builder("adaptr4j.downstream.error.total")
                .description("Calls that failed as seen by the caller (incl. CB/bulkhead rejections)")
                .tag("target", target)
                .register(meterRegistry);
    }

    /**
     * Error accounting is done at the OUTERMOST layer, i.e. on what the caller actually
     * receives.  adaptr4j.downstream.error.total counts every call that ends in an
     * exception -- a downstream failure that survived all retries, a circuit-breaker
     * rejection, or a bulkhead rejection.  Earlier versions counted only failed
     * downstream attempts, so CB/bulkhead rejections were invisible to the reward while
     * their ~0 ms durations pulled p99 down.  The "reason" breakdown is exported
     * separately (adaptr4j.downstream.rejected.total) for analysis.
     */
    @SuppressWarnings("unchecked")
    public Map<String, Object> callDownstream() {
        return callTimer.record(() -> {
            try {
                return policyHolder.execute(() -> webClient.get()
                        .uri("/api/data")
                        .retrieve()
                        .bodyToMono(Map.class)
                        .block());
            } catch (RuntimeException e) {
                errorCounter.increment();
                String reason = e instanceof CallNotPermittedException ? "cb_open"
                        : e instanceof BulkheadFullException ? "bulkhead_full"
                        : "downstream";
                Counter.builder("adaptr4j.downstream.rejected.total")
                        .description("Failed calls by reason")
                        .tag("target", target)
                        .tag("reason", reason)
                        .register(meterRegistry)
                        .increment();
                throw e;
            }
        });
    }
}
