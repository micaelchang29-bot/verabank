package com.verabank.transfer.saga;

import static org.assertj.core.api.Assertions.assertThat;
import static org.awaitility.Awaitility.await;

import com.verabank.transfer.support.Bank;
import com.verabank.transfer.support.Http;
import com.verabank.transfer.support.Infra;
import com.verabank.transfer.support.IntegrationTest;
import com.verabank.transfer.support.KafkaProbe;
import com.verabank.transfer.support.SagaEnabled;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Timer;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
import java.util.stream.Collectors;
import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.transaction.PlatformTransactionManager;

/**
 * T-10: Kafka caído. La API sigue aceptando, el outbox acumula (outbox.pending y outbox.oldest.age crecen,
 * outbox.lag mide la espera) y, al volver, se vacía en orden por clave. Por defecto el corte dura 10 s;
 * {@code -Dt10.outage=60} ejecuta el caso completo de 60 s.
 */
@SagaEnabled
class T10KafkaOutageIT extends IntegrationTest {

    private static final long OUTAGE_SECONDS = Long.getLong("t10.outage", 10);

    @Autowired
    PlatformTransactionManager txManager;

    @Autowired
    MeterRegistry meters;

    @Autowired
    SagaGauges gauges;

    private void pauseKafka() {
        Infra.KAFKA.getDockerClient().pauseContainerCmd(Infra.KAFKA.getContainerId()).exec();
    }

    private void resumeKafka() {
        try {
            Infra.KAFKA.getDockerClient().unpauseContainerCmd(Infra.KAFKA.getContainerId()).exec();
        } catch (RuntimeException e) {
            // ya estaba en marcha
        }
    }

    private long published(Set<UUID> transfers) {
        return jdbc.sql("SELECT count(*) FROM core.outbox WHERE aggregate_id IN (:ids) AND published_at IS NOT NULL")
                .param("ids", transfers).query(Long.class).single();
    }

    @Test
    void t10ConKafkaCaidoLaApiAceptaYElOutboxSeVaciaEnOrdenAlVolver() {
        Http http = new Http(port);
        Bank bank = new Bank(jdbc, txManager);
        List<UUID> sources = List.of(bank.account("5000.00"), bank.account("5000.00"), bank.account("5000.00"));
        UUID dest = bank.account("0");
        Timer lag = meters.timer("outbox.lag");

        // Con Kafka sano el retraso es corto: el relay publica en el siguiente sondeo.
        Http.Response warm = http.postTransfer(KEY_WEB, UUID.randomUUID(), Bank.internal(sources.get(0), dest, "1.00"));
        UUID warmId = UUID.fromString(warm.json().path("transfer_id").asText());
        await().atMost(Duration.ofSeconds(20)).until(() -> published(Set.of(warmId)) == 3);
        assertThat(jdbc.sql("SELECT max(published_at - created_at) < interval '1 second' FROM core.outbox "
                + "WHERE aggregate_id = :t").param("t", warmId).query(Boolean.class).single()).isTrue();

        Set<UUID> during = new HashSet<>();
        pauseKafka();
        try {
            Instant paused = Instant.now();
            // La API acepta mientras Kafka está caído: transferencias internas y externas de tres cuentas.
            for (int i = 0; i < 4; i++) {
                for (UUID source : sources) {
                    long t0 = System.nanoTime();
                    String body = i % 2 == 0 ? Bank.internal(source, dest, "2.00") : Bank.external(source, "2.00");
                    Http.Response r = http.postTransfer(KEY_WEB, UUID.randomUUID(), body);
                    assertThat(r.status()).as("la API sigue aceptando con Kafka caído").isEqualTo(201);
                    assertThat(TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - t0)).isLessThan(3000);
                    during.add(UUID.fromString(r.json().path("transfer_id").asText()));
                }
            }
            // Pasa el corte entero: nada se publica y el outbox acumula.
            long remaining = OUTAGE_SECONDS * 1000 - Duration.between(paused, Instant.now()).toMillis();
            if (remaining > 0) {
                Thread.sleep(remaining);
            }
            assertThat(published(during)).as("nada se publica con Kafka caído").isZero();
            gauges.refresh();
            assertThat(meters.get("outbox.pending").gauge().value()).isGreaterThanOrEqualTo(during.size() * 2.0);
            assertThat(meters.get("outbox.oldest.age").gauge().value()).as("la espera crece con el corte")
                    .isGreaterThanOrEqualTo(OUTAGE_SECONDS - 2.0);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        } finally {
            resumeKafka();
        }

        // Al volver, el outbox se vacía solo.
        await().atMost(Duration.ofSeconds(90)).pollInterval(Duration.ofMillis(250)).until(
                () -> jdbc.sql("SELECT count(*) FROM core.outbox WHERE aggregate_id IN (:ids) AND published_at IS NULL")
                        .param("ids", during).query(Long.class).single() == 0);

        // outbox.lag registró la espera del corte; los eventos nuevos vuelven a publicarse en menos de 1 s.
        assertThat(lag.max(TimeUnit.SECONDS)).as("lag máximo observado").isGreaterThanOrEqualTo(OUTAGE_SECONDS - 2.0);
        Http.Response after = http.postTransfer(KEY_WEB, UUID.randomUUID(), Bank.internal(sources.get(1), dest, "1.00"));
        UUID afterId = UUID.fromString(after.json().path("transfer_id").asText());
        await().atMost(Duration.ofSeconds(20)).until(() -> published(Set.of(afterId)) == 3);
        assertThat(jdbc.sql("SELECT max(published_at - created_at) < interval '1 second' FROM core.outbox "
                + "WHERE aggregate_id = :t").param("t", afterId).query(Boolean.class).single())
                .as("el lag se recupera").isTrue();
        gauges.refresh();
        assertThat(meters.get("outbox.pending").gauge().value()).isZero();
        assertThat(meters.get("outbox.oldest.age").gauge().value()).isZero();

        // En orden: para cada clave, los eventos llegan a Kafka en el orden de outbox_id.
        Set<String> keys = sources.stream().map(UUID::toString).collect(Collectors.toSet());
        Map<String, List<String>> expected = new java.util.LinkedHashMap<>();
        for (Map<String, Object> row : jdbc.sql("""
                SELECT message_key, event_id FROM core.outbox
                WHERE topic = 'transfer.events.v1' AND message_key IN (:keys) AND aggregate_id IN (:ids)
                ORDER BY outbox_id""").param("keys", keys).param("ids", during).query().listOfRows()) {
            expected.computeIfAbsent((String) row.get("message_key"), k -> new ArrayList<>())
                    .add(row.get("event_id").toString());
        }
        long total = expected.values().stream().mapToLong(List::size).sum();
        Set<String> expectedIds = expected.values().stream().flatMap(List::stream).collect(Collectors.toSet());
        List<ConsumerRecord<String, String>> records = KafkaProbe.readUntil(Infra.TRANSFER_EVENTS,
                recs -> recs.stream().filter(r -> expectedIds.contains(KafkaProbe.header(r, "event_id"))).count() >= total,
                Duration.ofSeconds(60));
        Map<String, List<String>> actual = new java.util.LinkedHashMap<>();
        for (ConsumerRecord<String, String> r : records) {
            String eventId = KafkaProbe.header(r, "event_id");
            if (expectedIds.contains(eventId)) {
                actual.computeIfAbsent(r.key(), k -> new ArrayList<>()).add(eventId);
            }
        }
        assertThat(actual).as("orden por clave en Kafka").isEqualTo(expected);
        assertThat(bank.reconcile()).containsExactly(0, 0);
    }
}
