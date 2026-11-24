package com.adaptr4j.serviceb.controller;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;
import java.util.concurrent.ThreadLocalRandom;

/**
 * Downstream service that simulates configurable latency and error injection.
 * Fault behaviour is controlled via application properties so Toxiproxy or
 * environment variables can override values per experiment scenario.
 */
@RestController
@RequestMapping("/api")
public class DownstreamController {

    private static final Logger log = LoggerFactory.getLogger(DownstreamController.class);

    @Value("${fault.latency-ms:0}")
    private volatile int baseLatencyMs;

    @Value("${fault.latency-jitter-ms:0}")
    private volatile int jitterMs;

    @Value("${fault.error-rate:0.0}")
    private volatile double errorRate;

    /**
     * Change the fault profile at runtime (used by the S5 spike and drift experiments).
     * Earlier experiments restarted Service B to switch faults, which produced
     * connection errors at every switch and could leave Service A's circuit breaker open.
     */
    @PostMapping("/fault")
    public ResponseEntity<Map<String, Object>> setFault(
            @RequestParam(required = false) Integer latencyMs,
            @RequestParam(required = false) Integer jitter,
            @RequestParam(required = false) Double errorRate) {
        if (latencyMs != null) this.baseLatencyMs = latencyMs;
        if (jitter != null) this.jitterMs = jitter;
        if (errorRate != null) this.errorRate = errorRate;
        log.info("Fault profile set: latency={}ms jitter={}ms errorRate={}",
                baseLatencyMs, jitterMs, this.errorRate);
        return ResponseEntity.ok(Map.of("latencyMs", baseLatencyMs,
                "jitterMs", jitterMs, "errorRate", this.errorRate));
    }

    @GetMapping("/data")
    public ResponseEntity<Map<String, Object>> getData() throws InterruptedException {
        applyFaultProfile();
        return ResponseEntity.ok(Map.of(
                "service", "B",
                "status", "ok",
                "timestamp", System.currentTimeMillis()
        ));
    }

    @GetMapping("/health")
    public ResponseEntity<Map<String, String>> health() {
        return ResponseEntity.ok(Map.of("status", "UP"));
    }

    private void applyFaultProfile() throws InterruptedException {
        // Inject latency
        int latency = baseLatencyMs;
        if (jitterMs > 0) {
            latency += ThreadLocalRandom.current().nextInt(-jitterMs, jitterMs + 1);
            latency = Math.max(0, latency);
        }
        if (latency > 0) {
            Thread.sleep(latency);
        }

        // Inject errors
        if (errorRate > 0 && ThreadLocalRandom.current().nextDouble() < errorRate) {
            log.debug("Injecting error (configured error-rate={})", errorRate);
            throw new RuntimeException("Injected fault: simulated downstream error");
        }
    }
}
