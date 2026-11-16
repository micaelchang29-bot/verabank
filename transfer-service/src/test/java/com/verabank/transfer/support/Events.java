package com.verabank.transfer.support;

import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.springframework.jdbc.core.simple.JdbcClient;

/** Lectura y validación del outbox de una transferencia contra contracts/events. */
public final class Events {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private Events() {
    }

    public record Row(String eventType, String topic, String key, JsonNode envelope, boolean published) {

        public JsonNode payload() {
            return envelope.path("payload");
        }
    }

    public static List<Row> outbox(JdbcClient jdbc, UUID transferId) {
        return jdbc.sql("""
                SELECT event_type, topic, message_key, payload::text AS envelope, published_at IS NOT NULL AS published
                FROM core.outbox WHERE aggregate_id = :t ORDER BY outbox_id""")
                .param("t", transferId)
                .query((rs, i) -> {
                    try {
                        return new Row(rs.getString("event_type"), rs.getString("topic"), rs.getString("message_key"),
                                MAPPER.readTree(rs.getString("envelope")), rs.getBoolean("published"));
                    } catch (Exception e) {
                        throw new IllegalStateException(e);
                    }
                })
                .list();
    }

    public static List<String> types(JdbcClient jdbc, UUID transferId) {
        return outbox(jdbc, transferId).stream().map(Row::eventType).toList();
    }

    /** Todo evento del outbox de la transferencia cumple el sobre y el esquema de su event_type. */
    public static void assertAllValid(JdbcClient jdbc, UUID transferId) {
        for (Row row : outbox(jdbc, transferId)) {
            assertValid(row.envelope());
            assertThat(row.envelope().path("event_type").asText()).isEqualTo(row.eventType());
        }
    }

    /** El relay añade emitted_at al publicar; aquí se simula para validar el sobre completo. */
    public static void assertValid(JsonNode stored) {
        ObjectNode envelope = stored.deepCopy();
        if (!envelope.has("emitted_at")) {
            envelope.put("emitted_at", Instant.now().toString());
        }
        assertThat(Contracts.envelopeErrors(envelope)).as("sobre de %s", stored.path("event_type").asText()).isEmpty();
        assertThat(Contracts.payloadErrors(envelope.path("event_type").asText(), envelope.path("payload")))
                .as("payload de %s", envelope.path("event_type").asText()).isEmpty();
    }

    public static Map<String, Long> countByType(JdbcClient jdbc, UUID transferId) {
        return outbox(jdbc, transferId).stream().collect(
                java.util.stream.Collectors.groupingBy(Row::eventType, java.util.stream.Collectors.counting()));
    }
}
