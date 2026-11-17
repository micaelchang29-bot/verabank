package com.verabank.risk.stream.aml;

import io.smallrye.mutiny.Uni;
import io.vertx.core.json.JsonObject;
import io.vertx.mutiny.sqlclient.SqlClient;
import io.vertx.mutiny.sqlclient.Tuple;
import io.vertx.sqlclient.data.Numeric;
import jakarta.enterprise.context.ApplicationScoped;
import java.math.BigDecimal;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.List;
import java.util.UUID;

/**
 * Upsert de alertas por {@code dedupe_key}. Si la clave ya existe solo se reemplazan {@code transfer_ids},
 * {@code total_amount} y {@code window_end}, y solo cuando el conjunto de transferencias cambió; {@code created_at},
 * {@code status} y {@code trigger_emitted_at} se conservan. Sin fila de retorno = la alerta ya existía sin cambios.
 */
@ApplicationScoped
public class AlertRepository {

    /** Alerta nueva o ampliada, tal como quedó guardada: es lo que se publica. */
    public record Persisted(UUID alertId, boolean created, String ruleCode, int ruleVersion, String severity,
            UUID subjectAccountId, List<UUID> transferIds, BigDecimal totalAmount, Instant windowStart,
            Instant windowEnd, Duration persistLatency) {
    }

    private static final String UPSERT = """
            INSERT INTO risk.aml_alerts
              (alert_id, dedupe_key, rule_code, rule_version, rule_params, subject_account_id, severity,
               window_start, window_end, total_amount, transfer_ids, trigger_emitted_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            ON CONFLICT (dedupe_key) DO UPDATE
              SET transfer_ids = EXCLUDED.transfer_ids,
                  total_amount = EXCLUDED.total_amount,
                  window_end   = EXCLUDED.window_end
              WHERE risk.aml_alerts.transfer_ids IS DISTINCT FROM EXCLUDED.transfer_ids
            RETURNING alert_id, (xmax = 0) AS created, rule_code, rule_version, severity, subject_account_id,
                      transfer_ids, total_amount, window_start, window_end,
                      EXTRACT(EPOCH FROM (created_at - trigger_emitted_at))::float8 AS latency_s
            """;

    public Uni<Persisted> upsert(SqlClient db, AlertCandidate c, Instant triggerEmittedAt) {
        Tuple args = Tuple.tuple()
                .addValue(UUID.randomUUID()).addValue(c.dedupeKey()).addValue(c.ruleCode()).addValue(c.ruleVersion())
                .addValue(new JsonObject(c.ruleParams())).addValue(c.subjectAccountId()).addValue(c.severity())
                .addValue(c.windowStart().atOffset(ZoneOffset.UTC)).addValue(c.windowEnd().atOffset(ZoneOffset.UTC))
                .addValue(Numeric.create(c.totalAmount())).addValue(c.transferIds().toArray(UUID[]::new))
                .addValue(triggerEmittedAt.atOffset(ZoneOffset.UTC));
        return db.preparedQuery(UPSERT).execute(args).map(rows -> {
            if (rows.size() == 0) {
                return null;
            }
            var row = rows.iterator().next();
            return new Persisted(row.getUUID("alert_id"), row.getBoolean("created"), row.getString("rule_code"),
                    row.getInteger("rule_version"), row.getString("severity"), row.getUUID("subject_account_id"),
                    List.of(row.getArrayOfUUIDs("transfer_ids")), row.getNumeric("total_amount").bigDecimalValue(),
                    row.getOffsetDateTime("window_start").toInstant(), row.getOffsetDateTime("window_end").toInstant(),
                    Duration.ofNanos((long) (row.getDouble("latency_s") * 1_000_000_000L)));
        });
    }
}
