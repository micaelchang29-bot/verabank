package com.verabank.transfer.transfer;

import java.time.Instant;
import java.util.UUID;

/** Transfer de contracts/openapi.yaml. Los montos van como cadena decimal. */
public record TransferView(
        UUID transferId,
        TransferStatus status,
        UUID sourceAccountId,
        UUID destAccountId,
        String destExternalRef,
        String destCountry,
        Rail rail,
        TransferType transferType,
        String amount,
        String currency,
        String fraudDecision,
        String failureReason,
        Instant occurredAt,
        Instant createdAt,
        Instant completedAt) {

    public static TransferView of(TransferRow t) {
        return new TransferView(t.transferId(), t.status(), t.sourceAccountId(), t.destAccountId(),
                t.destExternalRef(), t.destCountry(), t.rail(), t.transferType(), t.amount().toPlainString(),
                t.currency(), t.fraudDecision(), t.failureReason(), t.occurredAt(), t.createdAt(), t.completedAt());
    }
}
