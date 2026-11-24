package com.adaptr4j.servicec.controller;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;
import java.util.concurrent.ThreadLocalRandom;

/**
 * Second downstream service (parallel to Service B) for fan-out topology experiments.
 * Simulates configurable latency and error injection, independently of Service B,
 * so asymmetric fault scenarios (e.g. B under S1, C under S2) can be tested.
 */
@RestController
@RequestMapping("/api")
public class DownstreamController {

    private static final Logger log = LoggerFactory.getLogger(DownstreamController.class);

    @Value("${fault.latency-ms:0}")
    private int baseLatencyMs;

    @Value("${fault.latency-jitter-ms:0}")
    private int jitterMs;

    @Value("${fault.error-rate:0.0}")
    private double errorRate;

    @GetMapping("/data")
    public ResponseEntity<Map<String, Object>> getData() throws InterruptedException {
        applyFaultProfile();
        return ResponseEntity.ok(Map.of(
                "service", "C",
                "status", "ok",
                "timestamp", System.currentTimeMillis()
        ));
    }

    @GetMapping("/health")
    public ResponseEntity<Map<String, String>> health() {
        return ResponseEntity.ok(Map.of("status", "UP"));
    }

    private void applyFaultProfile() throws InterruptedException {
        int latency = baseLatencyMs;
        if (jitterMs > 0) {
            latency += ThreadLocalRandom.current().nextInt(-jitterMs, jitterMs + 1);
            latency = Math.max(0, latency);
        }
        if (latency > 0) {
            Thread.sleep(latency);
        }

        if (errorRate > 0 && ThreadLocalRandom.current().nextDouble() < errorRate) {
            log.debug("Injecting error (configured error-rate={})", errorRate);
            throw new RuntimeException("Injected fault: simulated downstream error");
        }
    }
}
