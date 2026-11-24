package com.adaptr4j.servicea.controller;

import com.adaptr4j.servicea.resilience.PolicyConfig;
import com.adaptr4j.servicea.resilience.PolicyReferenceHolder;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.Map;

/**
 * REST endpoint called by the Python policy engine to push new Resilience4j configs.
 * Accepts the full PolicyConfig as JSON and applies it to BOTH the B and C
 * PolicyReferenceHolders. For single-hop scenarios (S1-S6/W1-W4) only the
 * B-holder is ever exercised by traffic, so this is harmless there; for the
 * fan-out scenario (S7) this is what lets a single bandit action represent a
 * joint policy applied uniformly across both downstream dependencies, with
 * no changes needed to the bandit's action space or main.py.
 */
@RestController
@RequestMapping("/policy")
public class PolicyController {

    private static final Logger log = LoggerFactory.getLogger(PolicyController.class);

    private final PolicyReferenceHolder policyHolderB;
    private final PolicyReferenceHolder policyHolderC;

    public PolicyController(@Qualifier("policyHolderB") PolicyReferenceHolder policyHolderB,
                            @Qualifier("policyHolderC") PolicyReferenceHolder policyHolderC) {
        this.policyHolderB = policyHolderB;
        this.policyHolderC = policyHolderC;
    }

    @PostMapping
    public ResponseEntity<Map<String, Object>> applyPolicy(@RequestBody PolicyConfig newConfig) {
        log.info("Received policy update request: id={} name={}", newConfig.id(), newConfig.name());
        policyHolderB.swap(newConfig);
        policyHolderC.swap(newConfig);
        PolicyReferenceHolder.ActivePolicy active = policyHolderB.current();
        return ResponseEntity.ok(Map.of(
                "applied", true,
                "policy_id", active.config().id(),
                "version", active.version(),
                "applied_at", active.appliedAt().toString()
        ));
    }

    @GetMapping
    public ResponseEntity<Map<String, Object>> currentPolicy() {
        PolicyReferenceHolder.ActivePolicy active = policyHolderB.current();
        return ResponseEntity.ok(Map.of(
                "policy_id", active.config().id(),
                "policy_name", active.config().name(),
                "version", active.version(),
                "applied_at", active.appliedAt().toString(),
                "cb_failure_rate_threshold", active.config().cbFailureRateThreshold(),
                "retry_max_attempts", active.config().retryMaxAttempts(),
                "bulkhead_max_concurrent", active.config().bulkheadMaxConcurrentCalls()
        ));
    }
}
