package com.verabank.risk.stream.messaging;

import com.verabank.risk.stream.aml.AlertPublisher;
import com.verabank.risk.stream.aml.AmlProcessor;
import io.smallrye.mutiny.Uni;
import io.smallrye.reactive.messaging.kafka.api.IncomingKafkaRecordMetadata;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.inject.Inject;
import java.time.Duration;
import java.time.Instant;
import java.util.concurrent.CompletionStage;
import org.eclipse.microprofile.reactive.messaging.Incoming;
import org.eclipse.microprofile.reactive.messaging.Message;
import org.jboss.logging.Logger;
import org.jboss.logging.MDC;

/**
 * Consumidor de {@code transfer.events.v1} (grupo {@code risk-stream}). El mensaje solo se reconoce (y su offset se
 * confirma) después de confirmar la transacción local y de publicar las alertas. Otros {@code event_type} del
 * tópico se reconocen sin efecto. Solo existe con el rol stream: el canal se deshabilita con cualquier otro rol.
 */
@ApplicationScoped
public class TransferEventsConsumer {

    private static final Logger LOG = Logger.getLogger(TransferEventsConsumer.class);
    static final String TRANSFER_COMPLETED = "TransferCompleted";

    @Inject
    StreamPipeline pipeline;
    @Inject
    EventSchemas schemas;
    @Inject
    AmlProcessor processor;
    @Inject
    AlertPublisher publisher;
    @Inject
    StreamMetrics metrics;

    @Incoming("transfer-events")
    public CompletionStage<Void> onMessage(Message<String> message) {
        IncomingKafkaRecordMetadata<String, String> metadata = message.getMetadata(IncomingKafkaRecordMetadata.class)
                .orElseThrow(() -> new IllegalStateException("el mensaje no trae metadatos de Kafka"));
        SourceRecord record = SourceRecord.of(metadata, message.getPayload());
        return pipeline.run(Inbox.TRANSFER_EVENTS, record, this::handle)
                .subscribeAsCompletionStage()
                .thenCompose(v -> message.ack());
    }

    Uni<Void> handle(SourceRecord record) {
        Envelope envelope = schemas.parseEnvelope(record.value());
        MDC.put("correlation_id", envelope.correlationId().toString());
        metrics.consumerDelay(record.topic(), Duration.between(envelope.emittedAt(), Instant.now()));
        if (!TRANSFER_COMPLETED.equals(envelope.eventType())) {
            LOG.debugf("evento %s sin efecto en risk-stream", envelope.eventType());
            return Uni.createFrom().voidItem();
        }
        schemas.validatePayload(envelope);
        return processor.process(envelope).flatMap(publisher::publish);
    }
}
