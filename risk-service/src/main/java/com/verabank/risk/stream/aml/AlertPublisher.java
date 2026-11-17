package com.verabank.risk.stream.aml;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.verabank.risk.shared.RiskConfig;
import com.verabank.risk.stream.messaging.KafkaGateway;
import com.verabank.risk.stream.messaging.PublishFailedException;
import com.verabank.risk.stream.messaging.StreamMetrics;
import io.smallrye.mutiny.Uni;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.inject.Inject;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.List;
import java.util.UUID;
import org.apache.kafka.clients.producer.ProducerRecord;
import org.jboss.logging.Logger;

/**
 * Publica {@code AmlAlertRaised} en {@code aml.alerts.v1} con clave {@code subject_account_id}, solo después de
 * confirmar la transacción y solo para alertas nuevas o con otro conjunto de transferencias (D6). Un fallo de
 * publicación se reintenta con la misma política del consumidor; si persiste, el mensaje de origen va al DLT.
 */
@ApplicationScoped
public class AlertPublisher {

    private static final Logger LOG = Logger.getLogger(AlertPublisher.class);

    @Inject
    KafkaGateway kafka;
    @Inject
    ObjectMapper mapper;
    @Inject
    RiskConfig config;
    @Inject
    StreamMetrics metrics;

    public Uni<Void> publish(List<AmlProcessor.ToPublish> alerts) {
        Uni<Void> all = Uni.createFrom().voidItem();
        for (AmlProcessor.ToPublish item : alerts) {
            all = all.flatMap(v -> publishOne(item));
        }
        return all;
    }

    private Uni<Void> publishOne(AmlProcessor.ToPublish item) {
        var stream = config.stream();
        return Uni.createFrom().deferred(() -> kafka.send(record(item)))
                .onFailure().retry().withBackOff(stream.retryBackoff(), stream.retryBackoff()).withJitter(0.0)
                .atMost(stream.retries())
                .onFailure().transform(e -> {
                    metrics.publishFailure();
                    LOG.errorf(e, "no se pudo publicar AmlAlertRaised de la alerta %s", item.alert().alertId());
                    return new PublishFailedException("AmlAlertRaised de " + item.alert().alertId() + " sin publicar", e);
                });
    }

    ProducerRecord<String, String> record(AmlProcessor.ToPublish item) {
        AlertRepository.Persisted a = item.alert();
        ObjectNode payload = mapper.createObjectNode();
        payload.put("alert_id", a.alertId().toString());
        payload.put("rule_code", a.ruleCode());
        payload.put("rule_version", a.ruleVersion());
        payload.put("severity", a.severity());
        payload.put("subject_account_id", a.subjectAccountId().toString());
        ArrayNode ids = payload.putArray("transfer_ids");
        a.transferIds().forEach(id -> ids.add(id.toString()));
        payload.put("total_amount", a.totalAmount().toPlainString());
        payload.put("window_start", a.windowStart().toString());
        payload.put("window_end", a.windowEnd().toString());

        UUID eventId = UUID.randomUUID();
        ObjectNode envelope = mapper.createObjectNode();
        envelope.put("event_id", eventId.toString());
        envelope.put("event_type", "AmlAlertRaised");
        envelope.put("schema_version", 1);
        envelope.put("aggregate_id", a.alertId().toString());
        envelope.put("occurred_at", item.trigger().occurredAt().toString());   // tiempo de negocio del disparador
        envelope.put("emitted_at", Instant.now().toString());
        envelope.put("correlation_id", item.trigger().correlationId().toString());
        envelope.set("payload", payload);

        ProducerRecord<String, String> record = new ProducerRecord<>(config.stream().amlAlertsTopic(),
                a.subjectAccountId().toString(), envelope.toString());
        record.headers()
                .add("event_id", eventId.toString().getBytes(StandardCharsets.UTF_8))
                .add("event_type", "AmlAlertRaised".getBytes(StandardCharsets.UTF_8))
                .add("correlation_id", item.trigger().correlationId().toString().getBytes(StandardCharsets.UTF_8));
        return record;
    }
}
