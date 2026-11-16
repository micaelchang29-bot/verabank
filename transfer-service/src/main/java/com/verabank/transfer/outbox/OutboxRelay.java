package com.verabank.transfer.outbox;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.verabank.transfer.config.Sql;
import com.verabank.transfer.config.VerabankProperties;
import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Timer;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Queue;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicBoolean;
import org.apache.kafka.clients.producer.ProducerRecord;
import org.apache.kafka.common.InvalidRecordException;
import org.apache.kafka.common.errors.RecordTooLargeException;
import org.apache.kafka.common.errors.SerializationException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
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
 *   <li>Orden por outbox_id: el lote se lee en ese orden y los eventos de una misma clave se
 *       envían de uno en uno, esperando la confirmación de cada uno antes del siguiente.</li>
 *   <li>Sin pérdidas ni duplicados: una fila solo se marca publicada cuando Kafka confirmó el
 *       envío ({@code acks=all}). Ante el primer fallo de una clave no se envía ningún posterior
 *       de esa clave: quedan pendientes y se reintentan en orden en el próximo lote.</li>
 *   <li>Errores no reintentables (mensaje demasiado grande…): el evento va a {@code <tópico>.dlt}
 *       (§3.1) y se marca publicado, para no bloquear la clave.</li>
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
    private final Timer lag;
    private final Counter dlt;

    public OutboxRelay(JdbcClient jdbc, KafkaTemplate<String, String> kafka, ObjectMapper mapper,
            PlatformTransactionManager txManager, VerabankProperties properties) {
        this(jdbc, kafka, mapper, txManager, properties, new SimpleMeterRegistry());
    }

    @Autowired
    public OutboxRelay(JdbcClient jdbc, KafkaTemplate<String, String> kafka, ObjectMapper mapper,
            PlatformTransactionManager txManager, VerabankProperties properties, MeterRegistry meters) {
        this.lag = Timer.builder("outbox.lag")
                .description("published_at menos created_at, al confirmar la publicación")
                .publishPercentileHistogram()
                .serviceLevelObjectives(Duration.ofSeconds(1))
                .register(meters);
        this.dlt = Counter.builder("outbox.dlt").description("Eventos del outbox enviados al DLT").register(meters);
        this.jdbc = jdbc;
        this.kafka = kafka;
        this.mapper = mapper;
        this.config = properties.outbox();
        this.tx = new TransactionTemplate(txManager);
        this.tx.setIsolationLevel(TransactionDefinition.ISOLATION_READ_COMMITTED);
    }

    private record Pending(long outboxId, UUID eventId, String topic, String key, String eventType, String envelope,
            Instant createdAt) {
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
                    SELECT outbox_id, event_id, topic, message_key, event_type, payload::text AS envelope, created_at
                    FROM core.outbox WHERE published_at IS NULL
                    ORDER BY outbox_id LIMIT :limit
                    FOR UPDATE SKIP LOCKED""")
                    .param("limit", config.batchSize())
                    .query((rs, i) -> new Pending(rs.getLong("outbox_id"), Sql.uuid(rs, "event_id"),
                            rs.getString("topic"), rs.getString("message_key"), rs.getString("event_type"),
                            rs.getString("envelope"), Sql.instant(rs, "created_at")))
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

    /**
     * Envía el lote respetando el orden por clave: los eventos de una misma clave van de uno
     * en uno, esperando la confirmación de cada uno antes de enviar el siguiente; las claves
     * distintas avanzan en paralelo. Ante el primer fallo de una clave no se envía ningún
     * posterior de esa clave, y solo se devuelven como publicados los confirmados.
     */
    private List<Long> send(List<Pending> batch) {
        String emittedAt = Instant.now().truncatedTo(ChronoUnit.MICROS).toString();
        Map<String, List<Pending>> byKey = new LinkedHashMap<>();
        for (Pending p : batch) {
            byKey.computeIfAbsent(p.topic() + '\u0000' + p.key(), k -> new ArrayList<>()).add(p);
        }
        Queue<Long> done = new ConcurrentLinkedQueue<>();
        AtomicBoolean cancelled = new AtomicBoolean(false);
        List<CompletableFuture<Void>> chains = new ArrayList<>(byKey.size());
        for (List<Pending> group : byKey.values()) {
            chains.add(sendInOrder(group, 0, emittedAt, done, cancelled));
        }
        try {
            CompletableFuture.allOf(chains.toArray(CompletableFuture[]::new))
                    .get(config.sendTimeout().toMillis(), TimeUnit.MILLISECONDS);
        } catch (TimeoutException e) {
            cancelled.set(true);   // ninguna cadena envía más; lo no confirmado se reintenta
            log.warn("el lote del outbox no terminó en {}; lo no confirmado se reintentará", config.sendTimeout());
        } catch (InterruptedException e) {
            cancelled.set(true);
            Thread.currentThread().interrupt();
        } catch (ExecutionException e) {
            cancelled.set(true);
            log.error("error inesperado enviando el lote del outbox", e);
        }
        List<Long> confirmed = new ArrayList<>(done);
        confirmed.sort(Long::compare);
        return confirmed;
    }

    private CompletableFuture<Void> sendInOrder(List<Pending> group, int index, String emittedAt,
            Queue<Long> done, AtomicBoolean cancelled) {
        if (index >= group.size() || cancelled.get()) {
            return CompletableFuture.completedFuture(null);
        }
        Pending p = group.get(index);
        return sendOne(p, emittedAt).handle((result, error) -> {
            if (error == null) {
                lag.record(Duration.between(p.createdAt(), Instant.now()));   // al confirmar la publicación
                done.add(p.outboxId());
                return sendInOrder(group, index + 1, emittedAt, done, cancelled);
            }
            if (isNonRetriable(error)) {
                // Reintentar no sirve: al <tópico>.dlt (§3.1) y se marca publicado, para no bloquear la clave.
                return deadLetter(p, emittedAt, error).thenCompose(sent -> {
                    if (!sent) {
                        return CompletableFuture.<Void>completedFuture(null);   // se reintenta todo, en orden
                    }
                    dlt.increment();
                    done.add(p.outboxId());
                    return sendInOrder(group, index + 1, emittedAt, done, cancelled);
                });
            }
            log.warn("no se pudo publicar outbox_id={} event_id={} ({}): {}; se reintentará y no se envía "
                    + "ningún posterior de la misma clave", p.outboxId(), p.eventId(), p.eventType(),
                    rootMessage(error));
            return CompletableFuture.<Void>completedFuture(null);
        }).thenCompose(next -> next);
    }

    /** Errores que un reintento no arregla: mensaje demasiado grande, registro inválido o no serializable. */
    static boolean isNonRetriable(Throwable error) {
        for (Throwable t = error; t != null; t = t.getCause() == t ? null : t.getCause()) {
            if (t instanceof RecordTooLargeException || t instanceof InvalidRecordException
                    || t instanceof SerializationException || t instanceof JsonProcessingException) {
                return true;
            }
        }
        return false;
    }

    /** Publica el evento en {@code <tópico>.dlt} (misma clave); {@code true} si Kafka lo confirmó. */
    private CompletableFuture<Boolean> deadLetter(Pending p, String emittedAt, Throwable error) {
        String reason = rootMessage(error);
        log.error("outbox_id={} event_id={} ({}) no es publicable ({}); se envía a {}.dlt y se marca publicado",
                p.outboxId(), p.eventId(), p.eventType(), reason, p.topic());
        try {
            ProducerRecord<String, String> record = new ProducerRecord<>(p.topic() + ".dlt", p.key(),
                    dltValue(p, emittedAt, error));
            record.headers()
                    .add("event_id", bytes(p.eventId().toString()))
                    .add("event_type", bytes(p.eventType()))
                    .add("original_topic", bytes(p.topic()))
                    .add("dlt_reason", bytes(reason.length() > 500 ? reason.substring(0, 500) : reason));
            return kafka.send(record).handle((r, e) -> {
                if (e != null) {
                    log.warn("no se pudo enviar outbox_id={} al DLT ({}); se reintentará", p.outboxId(),
                            rootMessage(e));
                }
                return e == null;
            });
        } catch (RuntimeException e) {
            log.warn("no se pudo preparar el DLT de outbox_id={}: {}", p.outboxId(), rootMessage(e));
            return CompletableFuture.completedFuture(false);
        }
    }

    /**
     * Valor del DLT: el sobre con su payload, salvo cuando el problema es el tamaño o el JSON
     * (el DLT tampoco admitiría el mensaje): entonces solo los metadatos. El evento completo
     * sigue en core.outbox, identificado por event_id.
     */
    private String dltValue(Pending p, String emittedAt, Throwable error) {
        try {
            ObjectNode envelope = (ObjectNode) mapper.readTree(p.envelope());
            envelope.put("emitted_at", emittedAt);
            boolean tooLarge = false;
            for (Throwable t = error; t != null; t = t.getCause() == t ? null : t.getCause()) {
                tooLarge |= t instanceof RecordTooLargeException;
            }
            if (tooLarge) {
                envelope.set("payload", mapper.createObjectNode()
                        .put("omitted", "payload demasiado grande; ver core.outbox")
                        .put("outbox_id", p.outboxId())
                        .put("payload_chars", p.envelope().length()));
            }
            return mapper.writeValueAsString(envelope);
        } catch (Exception e) {
            return mapper.createObjectNode()
                    .put("omitted", "sobre no legible; ver core.outbox")
                    .put("outbox_id", p.outboxId())
                    .put("event_id", p.eventId().toString())
                    .toString();
        }
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
