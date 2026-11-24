package com.adaptr4j.servicea.config;

import com.adaptr4j.servicea.client.DownstreamClient;
import com.adaptr4j.servicea.resilience.PolicyReferenceHolder;
import io.github.resilience4j.bulkhead.BulkheadRegistry;
import io.github.resilience4j.circuitbreaker.CircuitBreakerRegistry;
import io.github.resilience4j.retry.RetryRegistry;
import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.reactive.function.client.WebClient;

/**
 * Wires the PolicyReferenceHolder / DownstreamClient pairs for each downstream
 * dependency. Service B is the primary single-hop dependency used by all
 * S1-S6/W1-W4 scenarios. Service C is a second, independent dependency added
 * for the fan-out generality experiment (S7): /api/checkout-fanout calls both
 * B and C concurrently, each governed by its own CB/Retry/Bulkhead chain, while
 * the policy engine applies one joint policy to both (same action space as the
 * single-hop case — no bandit/menu changes required).
 *
 * The retry counter (RAF numerator) is created here and passed into each
 * PolicyReferenceHolder, which increments it via Resilience4j's onRetry event
 * publisher every time a call is retried (registered fresh on each policy
 * swap, since swap() creates a new Retry instance).
 */
@Configuration
public class ResilienceConfig {

    @Bean
    public PolicyReferenceHolder policyHolderB(CircuitBreakerRegistry cbRegistry,
                                               RetryRegistry retryRegistry,
                                               BulkheadRegistry bulkheadRegistry,
                                               MeterRegistry meterRegistry) {
        Counter retryCounter = Counter.builder("adaptr4j.downstream.retry.total")
                .description("Total retry attempts (RAF numerator)")
                .tag("target", "b")
                .register(meterRegistry);
        return new PolicyReferenceHolder("b", cbRegistry, retryRegistry, bulkheadRegistry,
                retryCounter::increment);
    }

    @Bean
    public PolicyReferenceHolder policyHolderC(CircuitBreakerRegistry cbRegistry,
                                               RetryRegistry retryRegistry,
                                               BulkheadRegistry bulkheadRegistry,
                                               MeterRegistry meterRegistry) {
        Counter retryCounter = Counter.builder("adaptr4j.downstream.retry.total")
                .description("Total retry attempts (RAF numerator)")
                .tag("target", "c")
                .register(meterRegistry);
        return new PolicyReferenceHolder("c", cbRegistry, retryRegistry, bulkheadRegistry,
                retryCounter::increment);
    }

    @Bean
    public DownstreamClient downstreamClientB(PolicyReferenceHolder policyHolderB,
                                              WebClient.Builder webClientBuilder,
                                              MeterRegistry meterRegistry,
                                              @Value("${downstream.base-url:http://service-b:8081}") String baseUrl) {
        return new DownstreamClient("b", policyHolderB, webClientBuilder, meterRegistry, baseUrl);
    }

    @Bean
    public DownstreamClient downstreamClientC(PolicyReferenceHolder policyHolderC,
                                              WebClient.Builder webClientBuilder,
                                              MeterRegistry meterRegistry,
                                              @Value("${downstream-c.base-url:http://service-c:8083}") String baseUrlC) {
        return new DownstreamClient("c", policyHolderC, webClientBuilder, meterRegistry, baseUrlC);
    }
}
