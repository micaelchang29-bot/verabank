package com.verabank.transfer.outbox;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.argThat;
import static org.mockito.Mockito.doReturn;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.verabank.transfer.scoring.Decision;
import com.verabank.transfer.scoring.DecisionSource;
import com.verabank.transfer.scoring.FraudScorer;
import com.verabank.transfer.scoring.ScoreResult;
import com.verabank.transfer.support.Bank;
import com.verabank.transfer.support.Contracts;
import com.verabank.transfer.support.Http;
import com.verabank.transfer.support.Infra;
import com.verabank.transfer.support.IntegrationTest;
import com.verabank.transfer.support.KafkaProbe;
import java.math.BigDecimal;
import java.time.Duration;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.stream.Collectors;
import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.test.annotation.DirtiesContext;
import org.springframework.test.context.bean.override.mockito.MockitoSpyBean;
import org.springframework.transaction.PlatformTransactionManager;

/**
 * Cada evento producido en transfer.events.v1 cumple envelope.v1.json y el esquema de su
 * event_type en contracts/events, lleva clave source_account_id y las cabeceras de §3.1.
 * El scorer se espía para producir también BLOCK y REVIEW, que el StubScorer nunca devuelve.
 */
@DirtiesContext(classMode = DirtiesContext.ClassMode.AFTER_CLASS)
class EventContractIT extends IntegrationTest {

    private static final BigDecimal BLOCK_AMOUNT = new BigDecimal("66.66");
    private static final BigDecimal REVIEW_AMOUNT = new BigDecimal("77.77");

    @MockitoSpyBean
    FraudScorer scorer;

    @Autowired
    PlatformTransactionManager txManager;

    @Autowired
    ObjectMapper mapper;

    @Test
    void cadaEventoProducidoCumpleSuEsquemaYCadaCompletadaTieneSuEvento() throws Exception {
        doReturn(new ScoreResult(new BigDecimal("0.99000"), Decision.BLOCK, DecisionSource.MODEL, "test", 3))
                .when(scorer).score(argThat(r -> r.amount().compareTo(BLOCK_AMOUNT) == 0));
        doReturn(new ScoreResult(new BigDecimal("0.50000"), Decision.REVIEW, DecisionSource.FALLBACK, "test", 3))
                .when(scorer).score(argThat(r -> r.amount().compareTo(REVIEW_AMOUNT) == 0));

        Bank bank = new Bank(jdbc, txManager);
        Http http = new Http(port);
        UUID source = bank.account("1000.00");
        UUID dest = bank.account("0");
        UUID blocked = bank.account("100.00", "BLOCKED");
        Map<String, List<String>> expected = new LinkedHashMap<>();

        expected.put(create(http, Bank.internal(source, dest, "12.34"), 201),
                List.of("TransferInitiated", "TransferScored", "TransferCompleted"));
        expected.put(create(http, Bank.internal(source, dest, "5000.00"), 422),
                List.of("TransferRejected"));
        expected.put(create(http, Bank.internal(blocked, dest, "1.00"), 422),
                List.of("TransferRejected"));
        expected.put(create(http, Bank.internal(source, dest, BLOCK_AMOUNT.toPlainString()), 422),
                List.of("TransferInitiated", "TransferScored", "TransferRejected"));
        expected.put(create(http, Bank.internal(source, dest, REVIEW_AMOUNT.toPlainString()), 201),
                List.of("TransferInitiated", "TransferScored", "TransferHeld"));
        expected.put(create(http, """
                {"source_account_id":"%s","dest_external_ref":"CCI-778899","dest_country":"CO","rail":"EXTERNAL",
                 "transfer_type":"PAYMENT","amount":"3.00","currency":"PEN","occurred_at":"2026-03-02T08:00:00Z"}"""
                .formatted(source), 201), List.of("TransferInitiated", "TransferScored"));

        List<ConsumerRecord<String, String>> records = KafkaProbe.readUntil(Infra.TRANSFER_EVENTS,
                rs -> typesByTransfer(rs).entrySet().containsAll(expected.entrySet()), Duration.ofSeconds(30));

        // Los eventos de estas transferencias son exactamente los esperados, en orden.
        Map<String, List<String>> ours = typesByTransfer(records);
        ours.keySet().retainAll(expected.keySet());
        assertThat(ours).isEqualTo(expected);

        // Todo lo que hay en el tópico (también lo de las demás pruebas) cumple los contratos.
        assertThat(records).isNotEmpty();
        Set<String> eventTypes = new HashSet<>();
        for (ConsumerRecord<String, String> r : records) {
            JsonNode envelope = mapper.readTree(r.value());
            String type = envelope.path("event_type").asText();
            eventTypes.add(type);
            assertThat(Contracts.envelopeErrors(envelope)).as("sobre de %s", r.value()).isEmpty();
            assertThat(Contracts.payloadErrors(type, envelope.path("payload"))).as("payload de %s", r.value()).isEmpty();
            assertThat(r.key()).isEqualTo(envelope.path("payload").path("source_account_id").asText(
                    sourceOf(envelope.path("aggregate_id").asText())));
            assertThat(KafkaProbe.header(r, "event_id")).isEqualTo(envelope.path("event_id").asText());
            assertThat(KafkaProbe.header(r, "event_type")).isEqualTo(type);
            assertThat(KafkaProbe.header(r, "correlation_id")).isEqualTo(envelope.path("correlation_id").asText());
        }
        assertThat(eventTypes).contains("TransferInitiated", "TransferScored", "TransferCompleted",
                "TransferRejected", "TransferHeld");

        // TransferScored lleva exactamente lo que se guardó en core.transfers.
        for (ConsumerRecord<String, String> r : records) {
            JsonNode envelope = mapper.readTree(r.value());
            if (!"TransferScored".equals(envelope.path("event_type").asText())) {
                continue;
            }
            JsonNode p = envelope.path("payload");
            Map<String, Object> row = jdbc.sql("""
                    SELECT fraud_score, fraud_decision, decision_source, model_version, scoring_latency_ms
                    FROM core.transfers WHERE transfer_id = CAST(:t AS uuid)""")
                    .param("t", p.path("transfer_id").asText()).query().singleRow();
            assertThat(p.path("score").decimalValue()).isEqualByComparingTo((BigDecimal) row.get("fraud_score"));
            assertThat(p.path("decision").asText()).isEqualTo(row.get("fraud_decision"));
            assertThat(p.path("decision_source").asText()).isEqualTo(row.get("decision_source"));
            assertThat(p.path("model_version").asText()).isEqualTo(row.get("model_version"));
            assertThat(p.path("latency_ms").asInt()).isEqualTo(row.get("scoring_latency_ms"));
        }

        // Cada transferencia completada por el servicio tiene su TransferCompleted en Kafka.
        List<String> completed = jdbc.sql("""
                SELECT transfer_id::text FROM core.transfers
                WHERE status = 'COMPLETED' AND client_id NOT IN ('test-seed', 'seed')""")
                .query(String.class).list();
        List<ConsumerRecord<String, String>> all = KafkaProbe.readUntil(Infra.TRANSFER_EVENTS,
                rs -> completedIds(rs).containsAll(completed), Duration.ofSeconds(30));
        assertThat(completedIds(all)).containsAll(completed);
    }

    @Test
    void elValidadorRechazaEventosQueNoCumplen() throws Exception {
        JsonNode bad = mapper.readTree("""
                {"transfer_id":"no-uuid","source_account_id":"%s","dest_country":"PE","rail":"INTERNAL",
                 "transfer_type":"TRANSFER","amount":12.5,"currency":"PEN"}""".formatted(UUID.randomUUID()));
        JsonNode badEnvelope = mapper.readTree("""
                {"event_id":"%s","event_type":"TransferInitiated","schema_version":1,"aggregate_id":"%s",
                 "occurred_at":"ayer","emitted_at":"2026-01-01T00:00:00Z","correlation_id":"%s","payload":{},
                 "extra":true}""".formatted(UUID.randomUUID(), UUID.randomUUID(), UUID.randomUUID()));

        assertThat(Contracts.payloadErrors("TransferInitiated", bad)).hasSizeGreaterThanOrEqualTo(3);
        assertThat(Contracts.envelopeErrors(badEnvelope)).hasSizeGreaterThanOrEqualTo(2);
    }

    private String create(Http http, String body, int expectedStatus) {
        UUID key = UUID.randomUUID();
        Http.Response r = http.postTransfer(KEY_WEB, key, body);
        assertThat(r.status()).as(r.body()).isEqualTo(expectedStatus);
        return jdbc.sql("SELECT transfer_id::text FROM core.transfers WHERE client_id = :c AND idempotency_key = :k")
                .param("c", CLIENT_WEB).param("k", key).query(String.class).single();
    }

    private String sourceOf(String transferId) {
        return jdbc.sql("SELECT source_account_id::text FROM core.transfers WHERE transfer_id = CAST(:t AS uuid)")
                .param("t", transferId).query(String.class).single();
    }

    private Map<String, List<String>> typesByTransfer(List<ConsumerRecord<String, String>> records) {
        Map<String, List<String>> result = new LinkedHashMap<>();
        for (ConsumerRecord<String, String> r : records) {
            try {
                JsonNode e = mapper.readTree(r.value());
                result.computeIfAbsent(e.path("aggregate_id").asText(), k -> new ArrayList<>())
                        .add(e.path("event_type").asText());
            } catch (Exception ex) {
                throw new IllegalStateException(ex);
            }
        }
        return result;
    }

    private Set<String> completedIds(List<ConsumerRecord<String, String>> records) {
        return records.stream()
                .filter(r -> "TransferCompleted".equals(KafkaProbe.header(r, "event_type")))
                .map(r -> {
                    try {
                        return mapper.readTree(r.value()).path("aggregate_id").asText();
                    } catch (Exception e) {
                        throw new IllegalStateException(e);
                    }
                })
                .collect(Collectors.toSet());
    }
}
