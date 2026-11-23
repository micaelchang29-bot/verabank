package com.verabank.risk.scoring;

import com.verabank.risk.shared.RiskConfig;
import io.quarkus.arc.lookup.LookupIfProperty;
import io.quarkus.reactive.datasource.ReactiveDataSource;
import io.smallrye.mutiny.Uni;
import io.vertx.core.json.JsonObject;
import io.vertx.mutiny.pgclient.PgPool;
import io.vertx.mutiny.sqlclient.Row;
import io.vertx.mutiny.sqlclient.RowSet;
import io.vertx.mutiny.sqlclient.SqlConnection;
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
        Tuple params = insertParams(in, features, scored, modelVersion);
        return pool.withTransaction(connection -> connection.preparedQuery(INSERT).execute(params)
                        .flatMap(rows -> Uni.createFrom().<Void>failure(new RollBack())))
                .onFailure(RollBack.class).recoverWithNull();
    }

    /**
     * Calienta TODAS las conexiones del pool: las retiene a la vez (así son {@code connections} distintas) y en cada
     * una ejecuta una consulta de cada tipo de la ruta de scoring (lectura de la bitácora, destino, origen) y el INSERT
     * dentro de una transacción que se revierte. Cada backend de PostgreSQL carga sus cachés de catálogo y planes; sin
     * esto, la primera solicitud real que cae en una conexión nueva los paga. No deja filas en la bitácora.
     *
     * <p>Si falla o se cancela (p. ej. por el tiempo máximo del calentamiento), toda conexión que el pool haya entregado
     * vuelve a él, también las que llegan después: ver {@link Borrowed}.
     */
    Uni<Integer> warmConnections(int connections, ScoreInput in, FeatureComputer.Windows w, Map<String, Double> features,
            DecisionPolicy.Scored scored, String modelVersion) {
        Tuple insert = insertParams(in, features, scored, modelVersion);
        return Uni.createFrom().deferred(() -> {
            Borrowed borrowed = new Borrowed();
            List<Uni<SqlConnection>> acquire = new ArrayList<>();
            for (int i = 0; i < connections; i++) {
                acquire.add(borrowed.acquire(pool));
            }
            return Uni.join().all(acquire).andFailFast().flatMap(held -> {
                List<Uni<Void>> runs = new ArrayList<>();
                for (SqlConnection c : held) {
                    runs.add(exercise(c, in, w, insert).eventually(() -> borrowed.release(c)));
                }
                return Uni.join().all(runs).andFailFast().map(done -> held.size());
            }).onTermination().invoke(borrowed::releaseAll);
        });
    }

    /**
     * Conexiones prestadas por un calentamiento. Con {@code andFailFast}, en cuanto una conexión no se podía abrir (la
     * base aún no estaba lista) el resto quedaba fuera del pool para siempre: las que ya se habían entregado no se
     * cerraban y las que llegaban después se descartaban sin devolverlas. Con el pool lleno de conexiones perdidas, cada
     * reintento esperaba hasta el tiempo máximo y el servicio no llegaba a estar listo (D16). Aquí cada conexión se
     * registra en cuanto el pool la entrega, fuera de la suscripción de Mutiny, y {@link #releaseAll()} las devuelve todas
     * al terminar, sea con éxito, fallo o cancelación; las que llegan después se devuelven en el acto.
     */
    private static final class Borrowed {

        private final List<io.vertx.sqlclient.SqlConnection> open = new ArrayList<>();
        private boolean finished;

        Uni<SqlConnection> acquire(PgPool pool) {
            return Uni.createFrom().emitter(emitter -> pool.getDelegate().getConnection().onComplete(result -> {
                if (result.failed()) {
                    emitter.fail(result.cause());
                } else if (keep(result.result())) {
                    emitter.complete(SqlConnection.newInstance(result.result()));
                }
            }));
        }

        private synchronized boolean keep(io.vertx.sqlclient.SqlConnection connection) {
            if (finished) {
                connection.close();   // llegó tarde: el calentamiento ya terminó
                return false;
            }
            open.add(connection);
            return true;
        }

        private synchronized boolean forget(io.vertx.sqlclient.SqlConnection connection) {
            return open.remove(connection);
        }

        Uni<Void> release(SqlConnection connection) {
            return forget(connection.getDelegate()) ? connection.close() : Uni.createFrom().voidItem();
        }

        void releaseAll() {
            List<io.vertx.sqlclient.SqlConnection> rest;
            synchronized (this) {
                finished = true;
                rest = new ArrayList<>(open);
                open.clear();
            }
            rest.forEach(io.vertx.sqlclient.SqlConnection::close);
        }
    }

    private static Uni<Void> exercise(SqlConnection c, ScoreInput in, FeatureComputer.Windows w, Tuple insert) {
        return c.preparedQuery(FIND).execute(Tuple.of(in.transferId()))
                .flatMap(r -> c.preparedQuery(FeatureQueries.DESTINATION_SQL)
                        .execute(Tuple.of(in.destRef(), utc(w.from24h()), utc(w.to()), in.sourceRef())))
                .flatMap(r -> c.preparedQuery(FeatureQueries.SOURCE_SQL)
                        .execute(Tuple.of(in.sourceRef(), utc(w.from24h()), utc(w.to()), utc(w.from1h()))))
                .flatMap(r -> c.begin())
                .flatMap(tx -> c.preparedQuery(INSERT).execute(insert).flatMap(r -> tx.rollback()));
    }

    private static Tuple insertParams(ScoreInput in, Map<String, Double> features, DecisionPolicy.Scored scored,
            String modelVersion) {
        return Tuple.tuple()
                .addValue(in.transferId()).addValue(in.sourceRef()).addValue(in.destRef())
                .addValue(Numeric.create(in.amount())).addValue(in.occurredAt().atOffset(ZoneOffset.UTC))
                .addValue(new JsonObject(Map.copyOf(features))).addValue(Numeric.create(scored.score()))
                .addValue(scored.decision().name()).addValue(modelVersion);
    }

    private static java.time.OffsetDateTime utc(java.time.Instant instant) {
        return instant.atOffset(ZoneOffset.UTC);
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
