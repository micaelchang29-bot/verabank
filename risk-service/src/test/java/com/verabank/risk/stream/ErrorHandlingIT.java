package com.verabank.risk.stream;

import static com.verabank.risk.stream.StreamSupport.T0;
import static com.verabank.risk.stream.StreamSupport.alerts;
import static com.verabank.risk.stream.StreamSupport.awaitProcessed;
import static com.verabank.risk.stream.StreamSupport.counter;
import static com.verabank.risk.stream.StreamSupport.deadLetters;
import static com.verabank.risk.stream.StreamSupport.sendAndWait;
import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.verabank.risk.stream.aml.AlertRepository;
import com.verabank.risk.support.Db;
import com.verabank.risk.support.KafkaInfra;
import io.micrometer.core.instrument.MeterRegistry;
import io.quarkus.test.junit.QuarkusTest;
import io.quarkus.test.junit.TestProfile;
import io.quarkus.test.junit.mockito.InjectSpy;
import io.smallrye.mutiny.Uni;
import jakarta.inject.Inject;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.awaitility.Awaitility;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.Mockito;

/** Política de errores: inválido → .dlt sin reintentos; fallo de base → 3 reintentos y .dlt; tipos ajenos sin efecto. */
@QuarkusTest
@TestProfile(StreamProfile.class)
class ErrorHandlingIT {

    private static final String CONSUMER = "risk-stream:transfer-events";
    private static final ObjectMapper JSON = new ObjectMapper();

    @Inject
    MeterRegistry registry;
    @InjectSpy
    AlertRepository alertRepository;

    @BeforeEach
    void reset() {
        StreamDb.reset();
        Mockito.reset(alertRepository);
    }

    private static String header(ConsumerRecord<String, String> record, String name) {
        var h = record.headers().lastHeader(name);
        return h == null ? null : new String(h.value(), StandardCharsets.UTF_8);
    }

    private static void awaitDeadLetter(String topic, String key) {
        Awaitility.await().atMost(30, TimeUnit.SECONDS).pollInterval(Duration.ofMillis(100))
                .until(() -> !deadLetters(topic, key).isEmpty());
    }

    @Test
    void invalidMessagesGoStraightToTheDltWithoutRetries() throws Exception {
        UUID source = UUID.randomUUID();
        Events.Transfer base = Events.transfer(source, "100.00", T0);
        var valid = JSON.readTree(base.json());

        var noDestination = (com.fasterxml.jackson.databind.node.ObjectNode) JSON.readTree(base.json());
        ((com.fasterxml.jackson.databind.node.ObjectNode) noDestination.get("payload")).remove("dest_account_id");
        var badAmount = (com.fasterxml.jackson.databind.node.ObjectNode) JSON.readTree(base.json());
        ((com.fasterxml.jackson.databind.node.ObjectNode) badAmount.get("payload")).put("amount", "12.345");
        var badVersion = (com.fasterxml.jackson.databind.node.ObjectNode) JSON.readTree(base.json());
        badVersion.put("schema_version", 2);
        var extraEnvelopeField = (com.fasterxml.jackson.databind.node.ObjectNode) JSON.readTree(base.json());
        extraEnvelopeField.put("extra", "x");
        var badEventId = (com.fasterxml.jackson.databind.node.ObjectNode) JSON.readTree(base.json());
        badEventId.put("event_id", "no-es-uuid");
        var noPayloadFields = (com.fasterxml.jackson.databind.node.ObjectNode) JSON.readTree(base.json());
        noPayloadFields.set("payload", JSON.createObjectNode());
        assertThat(valid.get("event_type").asText()).isEqualTo("TransferCompleted");

        List<String> invalid = List.of("esto no es json{", "[1,2,3]", "{}", noDestination.toString(),
                badAmount.toString(), badVersion.toString(), extraEnvelopeField.toString(), badEventId.toString(),
                noPayloadFields.toString());

        double attempts = counter(registry, "stream.processing.attempts", "consumer", CONSUMER);
        double dlt = counter(registry, "stream.dlt", "topic", KafkaInfra.TRANSFER_EVENTS);
        for (int i = 0; i < invalid.size(); i++) {
            KafkaInfra.send(KafkaInfra.TRANSFER_EVENTS, source + "-bad-" + i, invalid.get(i));
        }
        for (int i = 0; i < invalid.size(); i++) {
            awaitDeadLetter(KafkaInfra.TRANSFER_EVENTS, source + "-bad-" + i);
        }

        // Un intento por mensaje: ni un reintento.
        assertThat(counter(registry, "stream.processing.attempts", "consumer", CONSUMER) - attempts)
                .isEqualTo(invalid.size());
        assertThat(counter(registry, "stream.dlt", "topic", KafkaInfra.TRANSFER_EVENTS) - dlt).isEqualTo(invalid.size());
        ConsumerRecord<String, String> first = deadLetters(KafkaInfra.TRANSFER_EVENTS, source + "-bad-0").get(0);
        assertThat(first.partition()).isZero();   // el .dlt tiene una sola partición
        assertThat(first.value()).isEqualTo(invalid.get(0));
        assertThat(header(first, "x-original-topic")).isEqualTo(KafkaInfra.TRANSFER_EVENTS);
        assertThat(header(first, "x-exception-class")).endsWith("InvalidMessageException");
        assertThat(header(first, "x-original-offset")).isNotBlank();
        assertThat(Db.queryLong("SELECT count(*) FROM risk.processed_messages")).isZero();
    }

    @Test
    void otherEventTypesAreAcknowledgedWithoutEffect() throws Exception {
        UUID source = UUID.randomUUID();
        UUID eventId = UUID.randomUUID();
        var payload = JSON.createObjectNode();
        payload.put("transfer_id", UUID.randomUUID().toString());
        payload.put("source_account_id", source.toString());
        String initiated = Events.envelope(eventId, "TransferInitiated", UUID.randomUUID(), T0, T0, UUID.randomUUID(), payload);
        double dlt = counter(registry, "stream.dlt");
        KafkaInfra.send(KafkaInfra.TRANSFER_EVENTS, source.toString(), initiated);
        // El marcador usa la misma clave (misma partición): cuando se procesa, el anterior ya se reconoció.
        sendAndWait(Events.transfer(source, "12000.00", T0));
        assertThat(Db.queryLong("SELECT count(*) FROM risk.processed_messages WHERE event_id = ?", eventId)).isZero();
        assertThat(Db.queryLong("SELECT count(*) FROM risk.transfer_facts")).isEqualTo(1);
        assertThat(counter(registry, "stream.dlt")).isEqualTo(dlt);
        assertThat(deadLetters(KafkaInfra.TRANSFER_EVENTS, source.toString())).isEmpty();
    }

    @Test
    void aPersistentDatabaseFailureRetriesThreeTimesAndThenGoesToTheDlt() {
        UUID source = UUID.randomUUID();
        Events.Transfer event = Events.transfer(source, "12000.00", T0);
        double attempts = counter(registry, "stream.processing.attempts", "consumer", CONSUMER);
        double dlt = counter(registry, "stream.dlt", "topic", KafkaInfra.TRANSFER_EVENTS);
        StreamDb.exec("REVOKE INSERT ON risk.processed_messages FROM risk_svc");
        try {
            StreamSupport.send(event);
            awaitDeadLetter(KafkaInfra.TRANSFER_EVENTS, source.toString());
        } finally {
            StreamDb.exec("GRANT INSERT ON risk.processed_messages TO risk_svc");
        }
        // Un intento inicial y tres reintentos.
        assertThat(counter(registry, "stream.processing.attempts", "consumer", CONSUMER) - attempts).isEqualTo(4);
        assertThat(counter(registry, "stream.dlt", "topic", KafkaInfra.TRANSFER_EVENTS) - dlt).isEqualTo(1);
        ConsumerRecord<String, String> record = deadLetters(KafkaInfra.TRANSFER_EVENTS, source.toString()).get(0);
        assertThat(record.partition()).isZero();
        assertThat(header(record, "x-exception-message")).containsIgnoringCase("permission denied");
        assertThat(alerts(source)).isZero();
        assertThat(Db.queryLong("SELECT count(*) FROM risk.transfer_facts WHERE source_account_id = ?", source)).isZero();

        // El consumidor sigue vivo y el mensaje fallido no se queda atascado: el siguiente se procesa.
        UUID next = UUID.randomUUID();
        sendAndWait(Events.transfer(next, "12000.00", T0));
        assertThat(alerts(next, "THRESHOLD")).isEqualTo(1);
    }

    @Test
    void aTransientFailureIsRetriedAndTheMessageIsProcessedWithoutReachingTheDlt() {
        UUID source = UUID.randomUUID();
        Events.Transfer event = Events.transfer(source, "12000.00", T0);
        // Falla dos veces en la escritura de la alerta (dentro de la transacción) y luego funciona.
        Mockito.doReturn(Uni.createFrom().failure(new IllegalStateException("conexión perdida")))
                .doReturn(Uni.createFrom().failure(new IllegalStateException("conexión perdida")))
                .doCallRealMethod()
                .when(alertRepository).upsert(any(), any(), any());
        double attempts = counter(registry, "stream.processing.attempts", "consumer", CONSUMER);
        sendAndWait(event);
        assertThat(counter(registry, "stream.processing.attempts", "consumer", CONSUMER) - attempts).isEqualTo(3);
        assertThat(alerts(source, "THRESHOLD")).isEqualTo(1);
        // Los intentos fallidos se revirtieron enteros: un solo hecho, una sola fila en el inbox.
        assertThat(Db.queryLong("SELECT count(*) FROM risk.transfer_facts WHERE transfer_id = ?", event.transferId)).isEqualTo(1);
        assertThat(Db.queryLong("SELECT count(*) FROM risk.processed_messages WHERE event_id = ?", event.eventId)).isEqualTo(1);
        assertThat(deadLetters(KafkaInfra.TRANSFER_EVENTS, source.toString())).isEmpty();
        awaitProcessed(event.eventId);
    }
}
