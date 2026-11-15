package com.verabank.transfer.outbox;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.verabank.transfer.config.Sql;
import com.verabank.transfer.config.VerabankProperties;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import org.apache.kafka.clients.producer.ProducerRecord;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.kafka.support.SendResult;
import org.springframework.stereotype.Component;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * Relay del outbox por sondeo (ADR-02).
 *
 * <ul>
 *   <li>Un solo relay activo: cada lote corre en una transacción que primero toma
 *       {@code pg_try_advisory_xact_lock}; si otra instancia lo tiene, el lote no hace nada.
 *       {@code FOR UPDATE SKIP LOCKED} es una segunda barrera.</li>
 *   <li>Orden por outbox_id: el lote se lee en ese orden y se envía en ese orden; el
 *       productor idempotente conserva el orden dentro de cada partición.</li>
 *   <li>Sin pérdidas: una fila solo se marca publicada cuando Kafka confirmó el envío
 *       ({@code acks=all}). Si un envío falla, esa fila y las siguientes de la misma clave
 *       quedan pendientes para el próximo lote, así el orden por clave no se rompe.</li>
 *   <li>Kafka caído: los envíos esperan dentro del productor hasta {@code delivery.timeout.ms},
 *       reintentando la misma secuencia idempotente, de modo que una caída más corta que ese
 *       plazo se recupera sin reenviar ni duplicar. La API sigue aceptando: los eventos esperan
 *       en el outbox.</li>
 * </ul>
 */
@Component
public class OutboxRelay {

    private static final Logger log = LoggerFactory.getLogger(OutboxRelay.class);

    private final JdbcClient jdbc;
    private final KafkaTemplate<String, String> kafka;
    private final ObjectMapper mapper;
    private final TransactionTemplate tx;
    private final VerabankProperties.Outbox config;

    public OutboxRelay(JdbcClient jdbc, KafkaTemplate<String, String> kafka, ObjectMapper mapper,
            PlatformTransactionManager txManager, VerabankProperties properties) {
        this.jdbc = jdbc;
        this.kafka = kafka;
        this.mapper = mapper;
        this.config = properties.outbox();
        this.tx = new TransactionTemplate(txManager);
        this.tx.setIsolationLevel(TransactionDefinition.ISOLATION_READ_COMMITTED);
    }

    private record Pending(long outboxId, UUID eventId, String topic, String key, String eventType, String envelope) {
    }

    /** Publica un lote; devuelve cuántas filas quedaron marcadas como publicadas. */
    public int publishBatch() {
        Integer published = tx.execute(status -> {
            Boolean locked = jdbc.sql("SELECT pg_try_advisory_xact_lock(:key)")
                    .param("key", config.lockKey())
                    .query(Boolean.class)
                    .single();
            if (!Boolean.TRUE.equals(locked)) {
                return 0;
            }
            List<Pending> batch = jdbc.sql("""
                    SELECT outbox_id, event_id, topic, message_key, event_type, payload::text AS envelope
                    FROM core.outbox WHERE published_at IS NULL
                    ORDER BY outbox_id LIMIT :limit
                    FOR UPDATE SKIP LOCKED""")
                    .param("limit", config.batchSize())
                    .query((rs, i) -> new Pending(rs.getLong("outbox_id"), Sql.uuid(rs, "event_id"),
                            rs.getString("topic"), rs.getString("message_key"), rs.getString("event_type"),
                            rs.getString("envelope")))
                    .list();
            if (batch.isEmpty()) {
                return 0;
            }
            List<Long> done = send(batch);
            if (!done.isEmpty()) {
                jdbc.sql("UPDATE core.outbox SET published_at = now() WHERE outbox_id IN (:ids)")
                        .param("ids", done)
                        .update();
            }
            return done.size();
        });
        return published == null ? 0 : published;
    }

    /** Vacía lo pendiente mientras haya lotes llenos. */
    public void drain() {
        int n;
        do {
            n = publishBatch();
        } while (n >= config.batchSize());
    }

    private List<Long> send(List<Pending> batch) {
        String emittedAt = Instant.now().truncatedTo(ChronoUnit.MICROS).toString();
        List<CompletableFuture<SendResult<String, String>>> futures = new ArrayList<>(batch.size());
        for (Pending p : batch) {
            futures.add(sendOne(p, emittedAt));
        }
        List<Long> done = new ArrayList<>(batch.size());
        Set<String> blockedKeys = new HashSet<>();
        long timeoutMs = config.sendTimeout().toMillis();
        for (int i = 0; i < batch.size(); i++) {
            Pending p = batch.get(i);
            String partitionKey = p.topic() + '\u0000' + p.key();
            try {
                futures.get(i).get(timeoutMs, TimeUnit.MILLISECONDS);
                if (!blockedKeys.contains(partitionKey)) {
                    done.add(p.outboxId());
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                blockedKeys.add(partitionKey);
            } catch (Exception e) {
                blockedKeys.add(partitionKey);
                log.warn("no se pudo publicar outbox_id={} event_id={} ({}): {}; se reintentará",
                        p.outboxId(), p.eventId(), p.eventType(), rootMessage(e));
            }
        }
        return done;
    }

    private CompletableFuture<SendResult<String, String>> sendOne(Pending p, String emittedAt) {
        try {
            ObjectNode envelope = (ObjectNode) mapper.readTree(p.envelope());
            envelope.put("emitted_at", emittedAt);
            ProducerRecord<String, String> record = new ProducerRecord<>(p.topic(), p.key(),
                    mapper.writeValueAsString(envelope));
            record.headers()
                    .add("event_id", bytes(p.eventId().toString()))
                    .add("event_type", bytes(p.eventType()))
                    .add("correlation_id", bytes(envelope.path("correlation_id").asText()));
            return kafka.send(record);
        } catch (Exception e) {
            return CompletableFuture.failedFuture(e);
        }
    }

    private static byte[] bytes(String s) {
        return s.getBytes(StandardCharsets.UTF_8);
    }

    private static String rootMessage(Throwable e) {
        Throwable t = e;
        while (t.getCause() != null && t.getCause() != t) {
            t = t.getCause();
        }
        return t.getClass().getSimpleName() + ": " + t.getMessage();
    }
}
