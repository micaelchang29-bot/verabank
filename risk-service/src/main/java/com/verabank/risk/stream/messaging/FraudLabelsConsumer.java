package com.verabank.risk.stream.messaging;

import com.verabank.risk.stream.labels.LabelProcessor;
import io.smallrye.mutiny.Uni;
import io.smallrye.reactive.messaging.kafka.api.IncomingKafkaRecordMetadata;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.inject.Inject;
import java.time.Duration;
import java.time.Instant;
import java.util.concurrent.CompletionStage;
import org.eclipse.microprofile.reactive.messaging.Incoming;
import org.eclipse.microprofile.reactive.messaging.Message;

/** Consumidor de {@code fraud.labels.v1}: etiquetas confirmadas para {@code risk.fraud_labels}, con inbox. */
@ApplicationScoped
public class FraudLabelsConsumer {

    static final String FRAUD_LABEL_CONFIRMED = "FraudLabelConfirmed";

    @Inject
    StreamPipeline pipeline;
    @Inject
    EventSchemas schemas;
    @Inject
    LabelProcessor processor;
    @Inject
    StreamMetrics metrics;

    @Incoming("fraud-labels")
    public CompletionStage<Void> onMessage(Message<String> message) {
        IncomingKafkaRecordMetadata<String, String> metadata = message.getMetadata(IncomingKafkaRecordMetadata.class)
                .orElseThrow(() -> new IllegalStateException("el mensaje no trae metadatos de Kafka"));
        SourceRecord record = SourceRecord.of(metadata, message.getPayload());
        return pipeline.run(Inbox.FRAUD_LABELS, record, this::handle)
                .subscribeAsCompletionStage()
                .thenCompose(v -> message.ack());
    }

    Uni<Void> handle(SourceRecord record) {
        Envelope envelope = schemas.parseEnvelope(record.value());
        metrics.consumerDelay(record.topic(), Duration.between(envelope.emittedAt(), Instant.now()));
        if (!FRAUD_LABEL_CONFIRMED.equals(envelope.eventType())) {
            throw new InvalidMessageException("event_type no admitido en fraud.labels.v1: " + envelope.eventType());
        }
        schemas.validatePayload(envelope);
        return processor.process(envelope);
    }
}
