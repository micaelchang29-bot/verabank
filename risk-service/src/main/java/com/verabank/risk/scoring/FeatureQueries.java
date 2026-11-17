package com.verabank.risk.scoring;

import com.verabank.risk.scoring.FeatureComputer.DestinationHistory;
import com.verabank.risk.scoring.FeatureComputer.SourceHistory;
import com.verabank.risk.scoring.FeatureComputer.Windows;
import com.verabank.risk.shared.RiskConfig;
import io.quarkus.arc.lookup.LookupIfProperty;
import io.quarkus.reactive.datasource.ReactiveDataSource;
import io.smallrye.mutiny.Uni;
import io.vertx.mutiny.pgclient.PgPool;
import io.vertx.mutiny.sqlclient.Row;
import io.vertx.mutiny.sqlclient.Tuple;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.inject.Inject;
import java.math.BigDecimal;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;

/**
 * Las dos consultas de features sobre {@code risk.scoring_log}, que se ejecutan a la vez (ADR-07). Ambas son
 * index-only: la de destino usa {@code ix_slog_dest (dest_ref, occurred_at DESC) INCLUDE (amount, source_ref)} y la de
 * origen {@code ix_slog_source (source_ref, occurred_at DESC) INCLUDE (amount)}. Ventanas {@code [t - w, t)}: la cota
 * superior es estricta, así que ninguna fila con {@code occurred_at >= t} cuenta. Las filas
 * {@code model_version = 'warmup'} cuentan como historia (D1).
 */
@ApplicationScoped
@LookupIfProperty(name = "verabank.role", stringValue = "scoring")
class FeatureQueries {

    /** $1 destino, $2 inicio de 24 h, $3 t, $4 origen. */
    static final String DESTINATION_SQL = """
            SELECT w.cnt, w.total, w.distinct_src, f.first_seen, p.pair_seen
            FROM (SELECT count(*) AS cnt, coalesce(sum(amount), 0) AS total, count(DISTINCT source_ref) AS distinct_src
                  FROM risk.scoring_log
                  WHERE dest_ref = $1 AND occurred_at >= $2 AND occurred_at < $3) w,
                 (SELECT min(occurred_at) AS first_seen
                  FROM risk.scoring_log
                  WHERE dest_ref = $1 AND occurred_at < $3) f,
                 (SELECT EXISTS (SELECT 1 FROM risk.scoring_log
                                 WHERE dest_ref = $1 AND source_ref = $4 AND occurred_at < $3)) AS p(pair_seen)
            """;

    /** $1 origen, $2 inicio de 24 h, $3 t, $4 inicio de 1 h. */
    static final String SOURCE_SQL = """
            SELECT count(*) FILTER (WHERE occurred_at >= $4) AS cnt_1h, count(*) AS cnt_24h,
                   coalesce(sum(amount), 0) AS sum_24h
            FROM risk.scoring_log
            WHERE source_ref = $1 AND occurred_at >= $2 AND occurred_at < $3
            """;

    @Inject
    @ReactiveDataSource("scoring")
    PgPool pool;
    @Inject
    RiskConfig config;

    Uni<DestinationHistory> destination(ScoreInput in, Windows w) {
        Uni<DestinationHistory> query = pool.preparedQuery(DESTINATION_SQL)
                .execute(Tuple.of(in.destRef(), utc(w.from24h()), utc(w.to()), in.sourceRef()))
                .map(rows -> {
                    Row row = rows.iterator().next();
                    OffsetDateTime first = row.getOffsetDateTime("first_seen");
                    return new DestinationHistory(row.getLong("cnt"), decimal(row, "total"), row.getLong("distinct_src"),
                            first == null ? null : first.toInstant(), row.getBoolean("pair_seen"));
                });
        return DbCall.limited(query, config.scoring().queryTimeout(), "destino");
    }

    Uni<SourceHistory> source(ScoreInput in, Windows w) {
        Uni<SourceHistory> query = pool.preparedQuery(SOURCE_SQL)
                .execute(Tuple.of(in.sourceRef(), utc(w.from24h()), utc(w.to()), utc(w.from1h())))
                .map(rows -> {
                    Row row = rows.iterator().next();
                    return new SourceHistory(row.getLong("cnt_1h"), row.getLong("cnt_24h"), decimal(row, "sum_24h"));
                });
        return DbCall.limited(query, config.scoring().queryTimeout(), "origen");
    }

    private static OffsetDateTime utc(java.time.Instant instant) {
        return instant.atOffset(ZoneOffset.UTC);
    }

    private static BigDecimal decimal(Row row, String column) {
        return row.getNumeric(column).bigDecimalValue();
    }
}
