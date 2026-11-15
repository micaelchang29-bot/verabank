package com.verabank.transfer.transfer;

import com.verabank.transfer.web.ApiException;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.UUID;

/** Solicitud validada y normalizada: lo que se guarda y lo que entra en el hash canónico. */
public record NewTransfer(
        UUID sourceAccountId,
        UUID destAccountId,
        String destExternalRef,
        String destCountry,
        Rail rail,
        TransferType transferType,
        BigDecimal amount,
        String currency,
        Instant occurredAt) {

    /** Reglas que el esquema no expresa por campo; la sintaxis ya la validó Bean Validation. */
    public static NewTransfer from(TransferRequest r) {
        if (r.rail() == Rail.INTERNAL && r.destAccountId() == null) {
            throw ApiException.validation("dest_account_id es obligatorio si rail es INTERNAL.");
        }
        if (r.rail() == Rail.EXTERNAL && r.destExternalRef() == null) {
            throw ApiException.validation("dest_external_ref es obligatorio si rail es EXTERNAL.");
        }
        if (r.sourceAccountId().equals(r.destAccountId())) {
            throw ApiException.validation("La cuenta de destino debe ser distinta de la de origen.");
        }
        BigDecimal amount = new BigDecimal(r.amount()).setScale(2, RoundingMode.UNNECESSARY);
        if (amount.signum() <= 0) {
            throw ApiException.validation("amount debe ser mayor que 0.");
        }
        // PostgreSQL guarda microsegundos: se trunca antes de guardar y de calcular el hash.
        Instant occurredAt = r.occurredAt().toInstant().truncatedTo(ChronoUnit.MICROS);
        return new NewTransfer(r.sourceAccountId(), r.destAccountId(), r.destExternalRef(), r.destCountry(),
                r.rail(), r.transferType(), amount, r.currency(), occurredAt);
    }
}
