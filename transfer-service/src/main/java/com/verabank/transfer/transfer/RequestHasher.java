package com.verabank.transfer.transfer;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HexFormat;
import java.util.Map;
import java.util.TreeMap;

/**
 * SHA-256 del cuerpo canónico (ADR-03): campos normalizados ("100" y "100.00" son el mismo
 * monto; la fecha va en UTC), claves en orden y sin nulos. Dos cuerpos con el mismo
 * significado dan el mismo hash aunque cambie el orden o el formato del JSON.
 */
public final class RequestHasher {

    private static final ObjectMapper CANONICAL = new ObjectMapper();

    private RequestHasher() {
    }

    public static String hash(NewTransfer t) {
        Map<String, String> fields = new TreeMap<>();
        fields.put("amount", t.amount().toPlainString());
        fields.put("currency", t.currency());
        fields.put("dest_country", t.destCountry());
        fields.put("occurred_at", t.occurredAt().toString());
        fields.put("rail", t.rail().name());
        fields.put("source_account_id", t.sourceAccountId().toString());
        fields.put("transfer_type", t.transferType().name());
        if (t.destAccountId() != null) {
            fields.put("dest_account_id", t.destAccountId().toString());
        }
        if (t.destExternalRef() != null) {
            fields.put("dest_external_ref", t.destExternalRef());
        }
        try {
            byte[] canonical = CANONICAL.writeValueAsString(fields).getBytes(StandardCharsets.UTF_8);
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(canonical));
        } catch (JsonProcessingException | NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }
}
