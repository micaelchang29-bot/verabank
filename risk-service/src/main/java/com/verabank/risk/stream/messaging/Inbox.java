package com.verabank.risk.stream.messaging;

import io.smallrye.mutiny.Uni;
import io.vertx.mutiny.sqlclient.SqlClient;
import io.vertx.mutiny.sqlclient.Tuple;
import jakarta.enterprise.context.ApplicationScoped;
import java.util.UUID;

/** Inbox de {@code risk.processed_messages}: se inserta en la misma transacción que el efecto del mensaje. */
@ApplicationScoped
public class Inbox {

    public static final String TRANSFER_EVENTS = "risk-stream:transfer-events";
    public static final String FRAUD_LABELS = "risk-stream:fraud-labels";

    /** {@code true} si el mensaje es nuevo; {@code false} si ya se procesó (sin efectos). */
    public Uni<Boolean> markProcessed(SqlClient db, String consumer, UUID eventId) {
        return db.preparedQuery("""
                INSERT INTO risk.processed_messages (consumer, event_id) VALUES ($1, $2)
                ON CONFLICT (consumer, event_id) DO NOTHING""")
                .execute(Tuple.of(consumer, eventId)).map(rows -> rows.rowCount() == 1);
    }
}
