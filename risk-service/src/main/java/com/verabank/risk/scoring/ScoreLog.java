package com.verabank.risk.scoring;

import com.verabank.risk.shared.RiskConfig;
import io.quarkus.arc.lookup.LookupIfProperty;
import io.quarkus.reactive.datasource.ReactiveDataSource;
import io.smallrye.mutiny.Uni;
import io.vertx.core.json.JsonObject;
import io.vertx.mutiny.pgclient.PgPool;
import io.vertx.mutiny.sqlclient.Row;
import io.vertx.mutiny.sqlclient.RowSet;
import io.vertx.mutiny.sqlclient.Tuple;
import io.vertx.sqlclient.data.Numeric;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.inject.Inject;
import java.math.BigDecimal;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;

/** Bitácora {@code risk.scoring_log}: idempotencia por {@code transfer_id} y actualización de {@code shadow_score}. */
@ApplicationScoped
@LookupIfProperty(name = "verabank.role", stringValue = "scoring")
class ScoreLog {

    record Stored(BigDecimal score, Decision decision, String modelVersion) {
    }

    private static final String FIND = "SELECT score, decision, model_version FROM risk.scoring_log WHERE transfer_id = $1";
    private static final String INSERT = """
            INSERT INTO risk.scoring_log
              (transfer_id, source_ref, dest_ref, amount, occurred_at, features, score, decision, model_version)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (transfer_id) DO NOTHING
            RETURNING score, decision, model_version
            """;
    private static final String UPDATE_SHADOW = """
            UPDATE risk.scoring_log SET shadow_score = $2 WHERE transfer_id = $1 AND shadow_score IS NULL
            """;

    @Inject
    @ReactiveDataSource("scoring")
    PgPool pool;
    @Inject
    RiskConfig config;

    Uni<Optional<Stored>> find(UUID transferId) {
        return DbCall.limited(pool.preparedQuery(FIND).execute(Tuple.of(transferId)).map(ScoreLog::first),
                config.scoring().queryTimeout(), "bitácora (lectura)");
    }

    /** Inserta; vacío si el {@code transfer_id} ya existía (otra solicitud ganó la carrera). */
    Uni<Optional<Stored>> insert(ScoreInput in, Map<String, Double> features, DecisionPolicy.Scored scored,
            String modelVersion) {
        Tuple params = Tuple.tuple()
                .addValue(in.transferId()).addValue(in.sourceRef()).addValue(in.destRef())
                .addValue(Numeric.create(in.amount())).addValue(in.occurredAt().atOffset(ZoneOffset.UTC))
                .addValue(new JsonObject(Map.copyOf(features))).addValue(Numeric.create(scored.score()))
                .addValue(scored.decision().name()).addValue(modelVersion);
        return DbCall.limited(pool.preparedQuery(INSERT).execute(params).map(ScoreLog::first),
                config.scoring().queryTimeout(), "bitácora (escritura)");
    }

    /**
     * Ejecuta el INSERT real dentro de una transacción que siempre se revierte: calienta la sentencia preparada y la
     * ruta de escritura sin dejar ninguna fila en la bitácora.
     */
    Uni<Void> insertAndRollBack(ScoreInput in, Map<String, Double> features, DecisionPolicy.Scored scored,
            String modelVersion) {
        Tuple params = Tuple.tuple()
                .addValue(in.transferId()).addValue(in.sourceRef()).addValue(in.destRef())
                .addValue(Numeric.create(in.amount())).addValue(in.occurredAt().atOffset(ZoneOffset.UTC))
                .addValue(new JsonObject(Map.copyOf(features))).addValue(Numeric.create(scored.score()))
                .addValue(scored.decision().name()).addValue(modelVersion);
        return pool.withTransaction(connection -> connection.preparedQuery(INSERT).execute(params)
                        .flatMap(rows -> Uni.createFrom().<Void>failure(new RollBack())))
                .onFailure(RollBack.class).recoverWithNull();
    }

    private static final class RollBack extends RuntimeException {
        RollBack() {
            super("calentamiento: la transacción se revierte a propósito", null, false, false);
        }
    }

    /** Escribe {@code shadow_score} solo si está vacío; devuelve si cambió una fila. */
    Uni<Boolean> updateShadow(UUID transferId, BigDecimal shadowScore) {
        return DbCall.limited(pool.preparedQuery(UPDATE_SHADOW).execute(Tuple.of(transferId, Numeric.create(shadowScore)))
                .map(rows -> rows.rowCount() == 1), config.scoring().queryTimeout(), "bitácora (sombra)");
    }

    /** Abre {@code connections} conexiones a la vez para que la primera solicitud no pague el alta de la conexión. */
    Uni<Void> warmUp(int connections) {
        List<Uni<Void>> pings = new ArrayList<>();
        for (int i = 0; i < connections; i++) {
            pings.add(pool.query("SELECT 1").execute().replaceWithVoid());
        }
        return Uni.join().all(pings).andFailFast().replaceWithVoid();
    }

    Uni<Void> ping() {
        return pool.query("SELECT 1").execute().replaceWithVoid();
    }

    private static Optional<Stored> first(RowSet<Row> rows) {
        if (rows.rowCount() == 0) {
            return Optional.empty();
        }
        Row row = rows.iterator().next();
        return Optional.of(new Stored(row.getNumeric("score").bigDecimalValue(), Decision.valueOf(row.getString("decision")),
                row.getString("model_version")));
    }
}
