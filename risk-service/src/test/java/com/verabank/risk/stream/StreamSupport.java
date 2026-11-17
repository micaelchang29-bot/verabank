package com.verabank.risk.stream;

import com.verabank.risk.support.Db;
import com.verabank.risk.support.KafkaInfra;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Counter;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.awaitility.Awaitility;

/** Atajos comunes de las pruebas del rol stream. */
public final class StreamSupport {

    public static final Instant T0 = Instant.parse("2026-03-01T10:00:00Z");

    private StreamSupport() {
    }

    /** Publica un TransferCompleted (clave: cuenta de origen, como transfer-service) y espera a que se procese. */
    public static void sendAndWait(Events.Transfer t) {
        send(t);
        awaitProcessed(t.eventId);
    }

    public static void send(Events.Transfer t) {
        t.emittedAt = Instant.now();
        KafkaInfra.send(KafkaInfra.TRANSFER_EVENTS, t.source.toString(), t.json());
    }

    public static void awaitProcessed(UUID eventId) {
        Awaitility.await().atMost(30, TimeUnit.SECONDS).pollInterval(Duration.ofMillis(25))
                .until(() -> Db.queryLong("SELECT count(*) FROM risk.processed_messages WHERE event_id = ?", eventId) == 1);
    }

    public static long alerts(UUID subject, String ruleCode) {
        return Db.queryLong("SELECT count(*) FROM risk.aml_alerts WHERE subject_account_id = ? AND rule_code = ?",
                subject, ruleCode);
    }

    public static long alerts(UUID subject) {
        return Db.queryLong("SELECT count(*) FROM risk.aml_alerts WHERE subject_account_id = ?", subject);
    }

    /** Mensajes de aml.alerts.v1 con esa clave (cuenta). */
    public static List<ConsumerRecord<String, String>> published(UUID subject) {
        return KafkaInfra.readAll(KafkaInfra.AML_ALERTS).stream().filter(r -> subject.toString().equals(r.key())).toList();
    }

    public static List<ConsumerRecord<String, String>> deadLetters(String topic, String key) {
        return KafkaInfra.readAll(topic + ".dlt").stream().filter(r -> key.equals(r.key())).toList();
    }

    public static double counter(MeterRegistry registry, String name, String tagKey, String tagValue) {
        double sum = 0;
        for (Counter c : registry.find(name).tag(tagKey, tagValue).counters()) {
            sum += c.count();
        }
        return sum;
    }

    public static double counter(MeterRegistry registry, String name) {
        double sum = 0;
        for (Counter c : registry.find(name).counters()) {
            sum += c.count();
        }
        return sum;
    }
}
