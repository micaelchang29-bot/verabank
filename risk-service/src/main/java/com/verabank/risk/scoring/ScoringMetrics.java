package com.verabank.risk.scoring;

import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Timer;
import io.quarkus.arc.lookup.LookupIfProperty;
import jakarta.annotation.PostConstruct;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.inject.Inject;
import java.time.Duration;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Métricas del rol scoring. {@code scoring.stage.duration{stage}} es un histograma con el presupuesto de
 * docs/diseno.md §1.2 como cubeta (features 40 ms, inferencia 10 ms, bitácora 10 ms; total 150 ms de p95 en el cliente).
 */
@ApplicationScoped
@LookupIfProperty(name = "verabank.role", stringValue = "scoring")
class ScoringMetrics {

    static final Map<String, Duration> BUDGET = Map.of(
            "features", Duration.ofMillis(40),
            "inference", Duration.ofMillis(10),
            "log", Duration.ofMillis(10),
            "total", Duration.ofMillis(150));

    @Inject
    MeterRegistry registry;

    private final Map<String, Timer> timers = new ConcurrentHashMap<>();

    @PostConstruct
    void register() {
        BUDGET.keySet().forEach(this::timer);
        for (Decision decision : Decision.values()) {
            decisionCounter(decision);
        }
        shadowFailure(0);
    }

    /** Fuerza la creación del bean (y así su {@link #register()}) antes de la primera solicitud real. */
    void ensureRegistered() {
        // El cuerpo vacío basta: llamar a un método del proxy de ApplicationScoped instancia el bean.
    }

    private Timer timer(String stage) {
        return timers.computeIfAbsent(stage, s -> Timer.builder("scoring.stage.duration")
                .description("Duración por etapa del scoring")
                .tag("stage", s)
                .publishPercentileHistogram()
                .publishPercentiles(0.5, 0.95, 0.99)
                .serviceLevelObjectives(BUDGET.get(s))
                .minimumExpectedValue(Duration.ofMillis(1))
                .maximumExpectedValue(Duration.ofSeconds(2))
                .register(registry));
    }

    void stage(String stage, long startNanos) {
        timer(stage).record(Duration.ofNanos(System.nanoTime() - startNanos));
    }

    private Counter decisionCounter(Decision decision) {
        return Counter.builder("scoring.decisions").tag("decision", decision.name()).register(registry);
    }

    void decision(Decision decision) {
        decisionCounter(decision).increment();
    }

    void error(String type) {
        Counter.builder("scoring.errors").tag("type", type).register(registry).increment();
    }

    void shadowFailure() {
        shadowFailure(1);
    }

    private void shadowFailure(double amount) {
        Counter.builder("scoring.shadow.failures").register(registry).increment(amount);
    }
}
