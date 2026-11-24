package com.adaptr4j.servicea.controller;

import com.adaptr4j.servicea.client.DownstreamClient;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Edge endpoint that load generators (Locust/hey) hit to drive the experiment.
 *
 * /checkout       — single-hop A->B, used by all S1-S6/W1-W4 scenarios (unchanged).
 * /checkout-fanout — A fans out to B and C concurrently (S7 fan-out generality scenario).
 *                    Mirrors a realistic checkout flow needing both an inventory
 *                    check (B) and a payment authorization (C) before responding.
 */
@RestController
@RequestMapping("/api")
public class EdgeController {

    private static final ExecutorService LEG_EXECUTOR = Executors.newVirtualThreadPerTaskExecutor();

    private final DownstreamClient downstreamClientB;
    private final DownstreamClient downstreamClientC;

    public EdgeController(@Qualifier("downstreamClientB") DownstreamClient downstreamClientB,
                          @Qualifier("downstreamClientC") DownstreamClient downstreamClientC) {
        this.downstreamClientB = downstreamClientB;
        this.downstreamClientC = downstreamClientC;
    }

    @GetMapping("/checkout")
    public ResponseEntity<Map<String, Object>> checkout() {
        Map<String, Object> result = downstreamClientB.callDownstream();
        return ResponseEntity.ok(result);
    }

    @GetMapping("/checkout-fanout")
    public ResponseEntity<Map<String, Object>> checkoutFanout() {
        // Legs run on virtual threads. The previous version used the common ForkJoinPool
        // (cores-1 = 7 threads here); each leg blocks for the downstream latency, so at
        // W2 load requests queued for seconds before a leg could even start.
        CompletableFuture<Map<String, Object>> bFuture =
                CompletableFuture.supplyAsync(downstreamClientB::callDownstream, LEG_EXECUTOR);
        CompletableFuture<Map<String, Object>> cFuture =
                CompletableFuture.supplyAsync(downstreamClientC::callDownstream, LEG_EXECUTOR);

        // Wait for both legs to complete (success or failure) before inspecting
        // results, so the edge latency reflects max(B, C) as in a real fan-out,
        // and both legs' metrics are always recorded regardless of which fails first.
        CompletableFuture.allOf(bFuture, cFuture).exceptionally(ex -> null).join();

        Map<String, Object> bResult = bFuture.join();
        Map<String, Object> cResult = cFuture.join();

        return ResponseEntity.ok(Map.of(
                "service_b", bResult,
                "service_c", cResult
        ));
    }

    @GetMapping("/health")
    public ResponseEntity<Map<String, String>> health() {
        return ResponseEntity.ok(Map.of("status", "UP"));
    }
}
