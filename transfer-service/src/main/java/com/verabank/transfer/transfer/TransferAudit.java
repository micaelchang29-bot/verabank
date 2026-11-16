package com.verabank.transfer.transfer;

import java.util.LinkedHashMap;
import java.util.Map;

/** Detalle común de las filas de auditoría de una transferencia. */
public final class TransferAudit {

    private TransferAudit() {
    }

    public static Map<String, Object> detail(TransferStatus from, TransferStatus to, TransferRow t, String reason) {
        Map<String, Object> d = new LinkedHashMap<>();
        d.put("from_status", from == null ? null : from.name());
        d.put("to_status", to.name());
        d.put("client_id", t.clientId());
        d.put("idempotency_key", t.idempotencyKey().toString());
        d.put("source_account_id", t.sourceAccountId().toString());
        if (t.destAccountId() != null) {
            d.put("dest_account_id", t.destAccountId().toString());
        }
        d.put("rail", t.rail().name());
        d.put("amount", t.amount().toPlainString());
        d.put("currency", t.currency());
        if (reason != null) {
            d.put("reason", reason);
        }
        return d;
    }
}
