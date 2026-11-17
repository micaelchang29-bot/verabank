package com.verabank.transfer.scoring;

import java.math.BigDecimal;
import java.time.Duration;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.boot.context.properties.bind.DefaultValue;

/**
 * Scoring (ADR-04). {@code mode}: {@code remote} llama a risk-service con fallback por monto; {@code stub} aprueba
 * siempre (make up-core y las pruebas de las fases 1 y 2).
 */
@ConfigurationProperties(prefix = "verabank.scoring")
public record ScoringProperties(
        @DefaultValue("remote") String mode,
        @DefaultValue("500.00") BigDecimal fallbackLimit,
        @DefaultValue Remote remote,
        @DefaultValue CircuitBreaker circuitBreaker) {

    public record Remote(
            @DefaultValue("http://localhost:8082") String url,
            @DefaultValue("50ms") Duration connectTimeout,
            @DefaultValue("120ms") Duration timeout) {
    }

    /** Ventana por número de llamadas; abre con el porcentaje de fallos indicado. */
    public record CircuitBreaker(
            @DefaultValue("20") int windowSize,
            @DefaultValue("10") int minimumCalls,
            @DefaultValue("50") float failureRatePercent,
            @DefaultValue("10s") Duration openDuration,
            @DefaultValue("5") int halfOpenCalls) {
    }
}
