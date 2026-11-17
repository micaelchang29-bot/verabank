package com.verabank.risk.stream;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.time.Instant;
import java.util.UUID;

/** Construye eventos de prueba con el envoltorio de contracts/events/envelope.v1.json. */
public final class Events {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private Events() {
    }

    /** Una transferencia completada; {@code emittedAt} por defecto es ahora (reloj real). */
    public static final class Transfer {
        public UUID eventId = UUID.randomUUID();
        public UUID transferId = UUID.randomUUID();
        public UUID source;
        public UUID dest = UUID.randomUUID();
        public String country = "ES";
        public String amount;
        public Instant occurredAt;
        public Instant emittedAt = Instant.now();
        public UUID correlationId = UUID.randomUUID();

        public Transfer(UUID source, String amount, Instant occurredAt) {
            this.source = source;
            this.amount = amount;
            this.occurredAt = occurredAt;
        }

        public Transfer country(String c) {
            this.country = c;
            return this;
        }

        public Transfer eventId(UUID id) {
            this.eventId = id;
            return this;
        }

        public Transfer transferId(UUID id) {
            this.transferId = id;
            return this;
        }

        public String json() {
            ObjectNode payload = MAPPER.createObjectNode();
            payload.put("transfer_id", transferId.toString());
            payload.put("source_account_id", source.toString());
            payload.put("dest_account_id", dest.toString());
            payload.put("dest_country", country);
            payload.put("rail", "INTERNAL");
            payload.put("transfer_type", "TRANSFER");
            payload.put("amount", amount);
            payload.put("currency", "EUR");
            payload.put("completed_at", occurredAt.toString());
            return envelope(eventId, "TransferCompleted", transferId, occurredAt, emittedAt, correlationId, payload);
        }
    }

    public static Transfer transfer(UUID source, String amount, Instant occurredAt) {
        return new Transfer(source, amount, occurredAt);
    }

    public static String fraudLabel(UUID eventId, UUID transferId, boolean isFraud, String source) {
        ObjectNode payload = MAPPER.createObjectNode();
        payload.put("transfer_id", transferId.toString());
        payload.put("is_fraud", isFraud);
        payload.put("label_source", source);
        return envelope(eventId, "FraudLabelConfirmed", transferId, Instant.now(), Instant.now(), UUID.randomUUID(),
                payload);
    }

    public static String envelope(UUID eventId, String type, UUID aggregateId, Instant occurredAt, Instant emittedAt,
            UUID correlationId, ObjectNode payload) {
        ObjectNode root = MAPPER.createObjectNode();
        root.put("event_id", eventId.toString());
        root.put("event_type", type);
        root.put("schema_version", 1);
        root.put("aggregate_id", aggregateId.toString());
        root.put("occurred_at", occurredAt.toString());
        root.put("emitted_at", emittedAt.toString());
        root.put("correlation_id", correlationId.toString());
        root.set("payload", payload);
        return root.toString();
    }
}
