package com.verabank.transfer.transfer;

import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;
import java.time.OffsetDateTime;
import java.util.UUID;

/** Cuerpo de {@code POST /api/v1/transfers} (TransferRequest en contracts/openapi.yaml). */
public record TransferRequest(
        @NotNull UUID sourceAccountId,
        UUID destAccountId,
        @Size(min = 1, max = 256) String destExternalRef,
        @NotNull @Pattern(regexp = "^[A-Z]{2}$") String destCountry,
        @NotNull Rail rail,
        @NotNull TransferType transferType,
        @NotNull @Pattern(regexp = "^[0-9]{1,16}(\\.[0-9]{1,2})?$", message = "debe ser una cadena decimal con hasta 2 decimales")
        String amount,
        @NotNull @Pattern(regexp = "^[A-Z]{3}$") String currency,
        @NotNull OffsetDateTime occurredAt) {
}
