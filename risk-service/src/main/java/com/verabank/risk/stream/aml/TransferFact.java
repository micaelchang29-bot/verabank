package com.verabank.risk.stream.aml;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.UUID;

/** Proyección de un {@code TransferCompleted}; los instantes ya vienen truncados a microsegundos, como los guarda PostgreSQL. */
public record TransferFact(UUID transferId, UUID sourceAccountId, String destRef, String destCountry,
        String transferType, BigDecimal amount, Instant occurredAt, Instant emittedAt, Instant completedAt) {
}
