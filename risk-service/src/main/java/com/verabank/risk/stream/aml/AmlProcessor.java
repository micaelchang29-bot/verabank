package com.verabank.risk.stream.aml;

import com.fasterxml.jackson.databind.JsonNode;
import com.verabank.risk.stream.messaging.Envelope;
import com.verabank.risk.stream.messaging.EventSchemas;
import com.verabank.risk.stream.messaging.Inbox;
import com.verabank.risk.stream.messaging.StreamMetrics;
import io.quarkus.reactive.datasource.ReactiveDataSource;
import io.smallrye.mutiny.Uni;
import io.vertx.mutiny.pgclient.PgPool;
import io.vertx.mutiny.sqlclient.SqlClient;
import io.vertx.mutiny.sqlclient.Tuple;
import io.vertx.sqlclient.data.Numeric;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.inject.Inject;
import java.math.BigDecimal;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import org.jboss.logging.Logger;

/**
 * {@code TransferCompleted}: en UNA transacción, inbox + proyección idempotente en {@code risk.transfer_facts} +
 * cadena de reglas + upsert de alertas. Devuelve las alertas que hay que publicar (nuevas o con otro conjunto de
 * transferencias, D6) ya con la transacción confirmada.
 */
@ApplicationScoped
public class AmlProcessor {

    private static final Logger LOG = Logger.getLogger(AmlProcessor.class);

    /** Una alerta por publicar y el evento que la disparó. */
    public record ToPublish(AlertRepository.Persisted alert, Envelope trigger) {
    }

    private static final String INSERT_FACT = """
            INSERT INTO risk.transfer_facts
              (transfer_id, source_account_id, dest_ref, dest_country, transfer_type, amount, occurred_at,
               emitted_at, completed_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (transfer_id) DO NOTHING
            """;

    @Inject
    @ReactiveDataSource("stream")
    PgPool pool;
    @Inject
    Inbox inbox;
    @Inject
    RuleCatalog catalog;
    @Inject
    AmlChain chain;
    @Inject
    AlertRepository alerts;
    @Inject
    StreamMetrics metrics;

    public Uni<List<ToPublish>> process(Envelope envelope) {
        TransferFact fact = toFact(envelope);
        return catalog.snapshot().flatMap(rules -> pool.withTransaction(conn -> inbox
                .markProcessed(conn, Inbox.TRANSFER_EVENTS, envelope.eventId()).flatMap(isNew -> {
                    if (!isNew) {
                        metrics.duplicate(Inbox.TRANSFER_EVENTS);
                        LOG.debugf("TransferCompleted %s ya procesado; sin efectos", envelope.eventId());
                        return Uni.createFrom().item(List.<AlertRepository.Persisted>of());
                    }
                    return insertFact(conn, fact).flatMap(inserted -> inserted
                            ? chain.run(new AmlContext(conn, fact, rules)).flatMap(c -> persist(conn, c, fact))
                            : Uni.createFrom().item(List.<AlertRepository.Persisted>of()));
                })))
                .map(list -> {
                    list.forEach(a -> record(a));
                    return list.stream().map(a -> new ToPublish(a, envelope)).toList();
                });
    }

    private Uni<Boolean> insertFact(SqlClient conn, TransferFact f) {
        Tuple args = Tuple.tuple().addValue(f.transferId()).addValue(f.sourceAccountId()).addValue(f.destRef())
                .addValue(f.destCountry()).addValue(f.transferType()).addValue(Numeric.create(f.amount()))
                .addValue(f.occurredAt().atOffset(ZoneOffset.UTC)).addValue(f.emittedAt().atOffset(ZoneOffset.UTC))
                .addValue(f.completedAt().atOffset(ZoneOffset.UTC));
        return conn.preparedQuery(INSERT_FACT).execute(args).map(rows -> rows.rowCount() == 1);
    }

    private Uni<List<AlertRepository.Persisted>> persist(SqlClient conn, List<AlertCandidate> candidates,
            TransferFact fact) {
        Uni<List<AlertRepository.Persisted>> chainOfUpserts = Uni.createFrom().item(new ArrayList<>());
        for (AlertCandidate candidate : candidates) {
            chainOfUpserts = chainOfUpserts.flatMap(done -> alerts.upsert(conn, candidate, fact.emittedAt())
                    .map(saved -> {
                        if (saved != null) {
                            done.add(saved);
                        }
                        return done;
                    }));
        }
        return chainOfUpserts.map(List::copyOf);
    }

    private void record(AlertRepository.Persisted alert) {
        if (alert.created()) {
            metrics.alertRaised(alert.ruleCode());
            metrics.persistLatency(alert.persistLatency());
        } else {
            metrics.alertUpdated(alert.ruleCode());
        }
    }

    /** El payload ya se validó contra el esquema; las fechas del evento son las de negocio ({@code occurred_at}). */
    static TransferFact toFact(Envelope envelope) {
        JsonNode p = envelope.payload();
        String destRef = p.hasNonNull("dest_account_id") ? p.get("dest_account_id").asText()
                : p.get("dest_external_ref").asText();
        return new TransferFact(UUID.fromString(p.get("transfer_id").asText()),
                UUID.fromString(p.get("source_account_id").asText()), destRef, p.get("dest_country").asText(),
                p.get("transfer_type").asText(), new BigDecimal(p.get("amount").asText()),
                EventSchemas.micros(envelope.occurredAt()), EventSchemas.micros(envelope.emittedAt()),
                EventSchemas.micros(OffsetDateTime.parse(p.get("completed_at").asText()).toInstant()));
    }
}
