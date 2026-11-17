package com.verabank.risk.support;

import java.util.Map;
import java.util.UUID;
import java.util.function.Consumer;

/** Cuerpos de POST /internal/v1/scores para las pruebas. */
public final class Requests {

    private Requests() {
    }

    public static Map<String, Object> valid(UUID id) {
        Map<String, Object> body = new java.util.LinkedHashMap<>();
        body.put("transfer_id", id.toString());
        body.put("source_ref", "src-01");
        body.put("dest_ref", "dst-01");
        body.put("amount", "1250.00");
        body.put("currency", "PEN");
        body.put("transfer_type", "TRANSFER");
        body.put("dest_country", "PE");
        body.put("occurred_at", "2026-01-10T10:00:00Z");
        return body;
    }

    public static Map<String, Object> valid(UUID id, Consumer<Map<String, Object>> change) {
        Map<String, Object> body = valid(id);
        change.accept(body);
        return body;
    }
}
