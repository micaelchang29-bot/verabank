package com.verabank.transfer.scoring;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.UUID;

/** Lo que el scorer recibe; coincide con ScoreRequest de contracts/openapi.yaml. */
public record ScoreRequest(
        UUID transferId,
        String sourceRef,
        String destRef,
        BigDecimal amount,
        String currency,
        String transferType,
        String destCountry,
        Instant occurredAt) {
}
