package com.verabank.transfer.transfer;

import static com.verabank.transfer.config.Sql.instant;
import static com.verabank.transfer.config.Sql.ts;
import static com.verabank.transfer.config.Sql.uuid;

import com.verabank.transfer.scoring.ScoreResult;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.Instant;
import java.util.Optional;
import java.util.UUID;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

@Repository
public class TransferRepository {

    private static final String COLUMNS = """
            transfer_id, client_id, idempotency_key, request_hash, source_account_id, dest_account_id,
            dest_external_ref, dest_country, rail, transfer_type, amount, currency, status,
            fraud_decision, failure_reason, occurred_at, created_at, completed_at""";

    private final JdbcClient jdbc;

    public TransferRepository(JdbcClient jdbc) {
        this.jdbc = jdbc;
    }

    public Optional<TransferRow> find(UUID transferId) {
        return jdbc.sql("SELECT " + COLUMNS + " FROM core.transfers WHERE transfer_id = :id")
                .param("id", transferId)
                .query(TransferRepository::map)
                .optional();
    }

    public Optional<TransferRow> findForUpdate(UUID transferId) {
        return jdbc.sql("SELECT " + COLUMNS + " FROM core.transfers WHERE transfer_id = :id FOR UPDATE")
                .param("id", transferId)
                .query(TransferRepository::map)
                .optional();
    }

    public Optional<TransferRow> findByKey(String clientId, UUID idempotencyKey) {
        return jdbc.sql("SELECT " + COLUMNS + " FROM core.transfers"
                        + " WHERE client_id = :client AND idempotency_key = :key")
                .param("client", clientId)
                .param("key", idempotencyKey)
                .query(TransferRepository::map)
                .optional();
    }

    /**
     * Inserta la transferencia en PENDING. Si otra solicitud con la misma clave ya la insertó
     * (o la está insertando: PostgreSQL espera a que esa transacción termine), devuelve
     * {@code false} sin error: la restricción uq_transfer_idem resuelve la carrera (ADR-03).
     */
    public boolean insertPending(UUID transferId, String clientId, UUID idempotencyKey, String requestHash,
            NewTransfer t) {
        return jdbc.sql("""
                INSERT INTO core.transfers (transfer_id, client_id, idempotency_key, request_hash,
                    source_account_id, dest_account_id, dest_external_ref, dest_country, rail, transfer_type,
                    amount, currency, status, occurred_at)
                VALUES (:id, :client, :key, :hash, :source, :dest, :destRef, :country, :rail, :type,
                    :amount, :currency, 'PENDING', :occurredAt)
                ON CONFLICT ON CONSTRAINT uq_transfer_idem DO NOTHING""")
                .param("id", transferId)
                .param("client", clientId)
                .param("key", idempotencyKey)
                .param("hash", requestHash)
                .param("source", t.sourceAccountId())
                .param("dest", t.destAccountId())
                .param("destRef", t.destExternalRef())
                .param("country", t.destCountry())
                .param("rail", t.rail().name())
                .param("type", t.transferType().name())
                .param("amount", t.amount())
                .param("currency", t.currency())
                .param("occurredAt", ts(t.occurredAt()))
                .update() == 1;
    }

    public void recordScore(UUID transferId, ScoreResult score) {
        jdbc.sql("""
                UPDATE core.transfers SET fraud_score = :score, fraud_decision = :decision,
                    decision_source = :source, model_version = :model, scoring_latency_ms = :latency
                WHERE transfer_id = :id""")
                .param("id", transferId)
                .param("score", score.score())
                .param("decision", score.decision().name())
                .param("source", score.source().name())
                .param("model", score.modelVersion())
                .param("latency", score.latencyMs())
                .update();
    }

    /** Cambia el estado solo si sigue en {@code from}; devuelve la fila resultante. */
    public TransferRow transition(UUID transferId, TransferStatus from, TransferStatus to, String failureReason) {
        return jdbc.sql("UPDATE core.transfers SET status = :to, failure_reason = COALESCE(:reason, failure_reason),"
                        + " completed_at = CASE WHEN :to = 'COMPLETED' THEN now() ELSE completed_at END"
                        + " WHERE transfer_id = :id AND status = :from RETURNING " + COLUMNS)
                .param("id", transferId)
                .param("from", from.name())
                .param("to", to.name())
                .param("reason", failureReason)
                .query(TransferRepository::map)
                .optional()
                .orElseThrow(() -> new IllegalStateException(
                        "la transferencia " + transferId + " no está en " + from));
    }

    private static TransferRow map(ResultSet rs, int row) throws SQLException {
        Instant completed = instant(rs, "completed_at");
        return new TransferRow(uuid(rs, "transfer_id"), rs.getString("client_id"), uuid(rs, "idempotency_key"),
                rs.getString("request_hash"), uuid(rs, "source_account_id"), uuid(rs, "dest_account_id"),
                rs.getString("dest_external_ref"), rs.getString("dest_country"), Rail.valueOf(rs.getString("rail")),
                TransferType.valueOf(rs.getString("transfer_type")), rs.getBigDecimal("amount"),
                rs.getString("currency"), TransferStatus.valueOf(rs.getString("status")),
                rs.getString("fraud_decision"), rs.getString("failure_reason"), instant(rs, "occurred_at"),
                instant(rs, "created_at"), completed);
    }
}
