package com.verabank.transfer.transfer;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.UUID;

/** Fila de core.transfers. */
public record TransferRow(
        UUID transferId,
        String clientId,
        UUID idempotencyKey,
        String requestHash,
        UUID sourceAccountId,
        UUID destAccountId,
        String destExternalRef,
        String destCountry,
        Rail rail,
        TransferType transferType,
        BigDecimal amount,
        String currency,
        TransferStatus status,
        String fraudDecision,
        String failureReason,
        Instant occurredAt,
        Instant createdAt,
        Instant completedAt) {
}
