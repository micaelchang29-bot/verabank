package com.verabank.risk.scoring;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.UUID;

/** Solicitud validada de POST /internal/v1/scores (ScoreRequest de contracts/openapi.yaml). */
public record ScoreInput(
        UUID transferId,
        String sourceRef,
        String destRef,
        BigDecimal amount,
        String currency,
        String transferType,
        String destCountry,
        Instant occurredAt) {
}
