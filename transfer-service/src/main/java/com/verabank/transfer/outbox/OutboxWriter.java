package com.verabank.transfer.outbox;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.time.Instant;
import java.util.UUID;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Component;

/**
 * Escribe eventos en core.outbox dentro de la transacción del cambio de estado (ADR-02).
 *
 * <p>La columna payload guarda el sobre completo (envelope.v1.json) salvo {@code emitted_at},
 * que el relay añade con el reloj real al publicar (§3.2).
 */
@Component
public class OutboxWriter {

    private final JdbcClient jdbc;
    private final ObjectMapper mapper;

    public OutboxWriter(JdbcClient jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    public UUID append(String topic, String aggregateType, UUID aggregateId, String messageKey, String eventType,
            Instant occurredAt, UUID correlationId, ObjectNode payload) {
        UUID eventId = UUID.randomUUID();
        ObjectNode envelope = mapper.createObjectNode();
        envelope.put("event_id", eventId.toString());
        envelope.put("event_type", eventType);
        envelope.put("schema_version", 1);
        envelope.put("aggregate_id", aggregateId.toString());
        envelope.put("occurred_at", occurredAt.toString());
        envelope.put("correlation_id", correlationId.toString());
        envelope.set("payload", payload);
        jdbc.sql("""
                INSERT INTO core.outbox (event_id, aggregate_type, aggregate_id, topic, message_key, event_type, payload)
                VALUES (:eventId, :aggregateType, :aggregateId, :topic, :key, :eventType, CAST(:payload AS jsonb))""")
                .param("eventId", eventId)
                .param("aggregateType", aggregateType)
                .param("aggregateId", aggregateId)
                .param("topic", topic)
                .param("key", messageKey)
                .param("eventType", eventType)
                .param("payload", write(envelope))
                .update();
        return eventId;
    }

    private String write(ObjectNode node) {
        try {
            return mapper.writeValueAsString(node);
        } catch (JsonProcessingException e) {
            throw new IllegalStateException(e);
        }
    }
}
