package com.verabank.risk.stream.messaging;

import io.smallrye.mutiny.Uni;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.inject.Inject;
import java.nio.charset.StandardCharsets;
import org.apache.kafka.clients.producer.ProducerRecord;
import org.apache.kafka.common.header.Header;
import org.jboss.logging.Logger;

/**
 * Publica en {@code <tópico>.dlt}. Esos tópicos tienen una sola partición (§3.1), así que el destino es siempre la
 * partición 0 y no la del mensaje original. Conserva clave, valor y cabeceras y añade el origen y la causa.
 */
@ApplicationScoped
public class DeadLetters {

    private static final Logger LOG = Logger.getLogger(DeadLetters.class);
    private static final int MAX_MESSAGE = 500;

    @Inject
    KafkaGateway kafka;
    @Inject
    StreamMetrics metrics;

    public Uni<Void> send(SourceRecord source, Throwable error) {
        Throwable cause = rootCause(error);
        String reason = error instanceof InvalidMessageException ? "invalid" : "failed";
        ProducerRecord<String, String> record = new ProducerRecord<>(source.topic() + ".dlt", 0, source.key(),
                source.value());
        for (Header h : source.headers()) {
            record.headers().add(h.key(), h.value());
        }
        record.headers()
                .add("x-original-topic", bytes(source.topic()))
                .add("x-original-partition", bytes(Integer.toString(source.partition())))
                .add("x-original-offset", bytes(Long.toString(source.offset())))
                .add("x-exception-class", bytes(cause.getClass().getName()))
                .add("x-exception-message", bytes(truncate(String.valueOf(cause.getMessage()))));
        LOG.errorf("mensaje de %s (partición %d, offset %d) al DLT: %s: %s", source.topic(), source.partition(),
                source.offset(), cause.getClass().getSimpleName(), cause.getMessage());
        return kafka.send(record).invoke(() -> metrics.deadLettered(source.topic(), reason));
    }

    private static Throwable rootCause(Throwable error) {
        Throwable t = error;
        while (t.getCause() != null && t.getCause() != t) {
            t = t.getCause();
        }
        return t;
    }

    private static String truncate(String s) {
        return s.length() > MAX_MESSAGE ? s.substring(0, MAX_MESSAGE) : s;
    }

    private static byte[] bytes(String s) {
        return s.getBytes(StandardCharsets.UTF_8);
    }
}
