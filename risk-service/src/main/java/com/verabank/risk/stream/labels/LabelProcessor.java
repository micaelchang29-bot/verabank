package com.verabank.risk.stream.labels;

import com.verabank.risk.stream.messaging.Envelope;
import com.verabank.risk.stream.messaging.Inbox;
import com.verabank.risk.stream.messaging.StreamMetrics;
import io.quarkus.reactive.datasource.ReactiveDataSource;
import io.smallrye.mutiny.Uni;
import io.vertx.mutiny.pgclient.PgPool;
import io.vertx.mutiny.sqlclient.Tuple;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.inject.Inject;
import java.util.UUID;

/**
 * {@code FraudLabelConfirmed}: inbox y upsert en {@code risk.fraud_labels} en una transacción. Una etiqueta repetida
 * con otro valor actualiza {@code is_fraud}, {@code label_source} y {@code labeled_at}; con el mismo valor y fuente no
 * toca la fila.
 */
@ApplicationScoped
public class LabelProcessor {

    private static final String UPSERT = """
            INSERT INTO risk.fraud_labels (transfer_id, is_fraud, label_source, labeled_at) VALUES ($1, $2, $3, now())
            ON CONFLICT (transfer_id) DO UPDATE
              SET is_fraud = EXCLUDED.is_fraud, label_source = EXCLUDED.label_source, labeled_at = now()
              WHERE (risk.fraud_labels.is_fraud, risk.fraud_labels.label_source)
                    IS DISTINCT FROM (EXCLUDED.is_fraud, EXCLUDED.label_source)
            """;

    @Inject
    @ReactiveDataSource("stream")
    PgPool pool;
    @Inject
    Inbox inbox;
    @Inject
    StreamMetrics metrics;

    public Uni<Void> process(Envelope envelope) {
        var p = envelope.payload();
        UUID transferId = UUID.fromString(p.get("transfer_id").asText());
        boolean isFraud = p.get("is_fraud").asBoolean();
        String source = p.get("label_source").asText();
        return pool.withTransaction(conn -> inbox.markProcessed(conn, Inbox.FRAUD_LABELS, envelope.eventId())
                .flatMap(isNew -> {
                    if (!isNew) {
                        metrics.duplicate(Inbox.FRAUD_LABELS);
                        return Uni.createFrom().voidItem();
                    }
                    return conn.preparedQuery(UPSERT).execute(Tuple.of(transferId, isFraud, source)).replaceWithVoid();
                }));
    }
}
