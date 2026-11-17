package com.verabank.risk.scoring;

import static org.assertj.core.api.Assertions.assertThat;

import com.verabank.risk.support.PgInfra;
import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

/**
 * ADR-07: ambas consultas de features son index-only (ix_slog_dest e ix_slog_source). Con 120.000 filas, VACUUM (para el
 * mapa de visibilidad) y EXPLAIN ANALYZE, con el plan de la primera ejecución y con el genérico de las sentencias
 * preparadas del pool.
 */
class IndexOnlyScanIT {

    private static final String DEST_ARGS = "'dst-7', timestamptz '2026-01-19 00:00:00+00', timestamptz '2026-01-20 00:00:00+00', 'src-7'";
    private static final String SOURCE_ARGS = "'src-7', timestamptz '2026-01-19 00:00:00+00', timestamptz '2026-01-20 00:00:00+00', timestamptz '2026-01-19 23:00:00+00'";

    @BeforeAll
    static void data() throws SQLException {
        PgInfra.truncateLog();
        try (Connection c = PgInfra.admin(); Statement s = c.createStatement()) {
            s.execute("""
                    INSERT INTO risk.scoring_log (transfer_id, source_ref, dest_ref, amount, occurred_at, features, score, decision, model_version)
                    SELECT gen_random_uuid(), 'src-' || (i % 3000), 'dst-' || (i % 2000), (i % 997) + 0.5,
                           timestamptz '2026-01-01 00:00:00+00' + (i % 720) * interval '1 hour', '{}'::jsonb, 0.1, 'APPROVE', 'warmup'
                    FROM generate_series(1, 120000) AS g(i)""");
            s.execute("VACUUM (ANALYZE) risk.scoring_log");
        }
    }

    private static List<String> explain(String mode, String prepare, String name, String args) throws SQLException {
        try (Connection c = PgInfra.admin(); Statement s = c.createStatement()) {
            s.execute("SET plan_cache_mode = " + mode);
            s.execute("PREPARE " + name + " AS " + prepare);
            List<String> plan = new ArrayList<>();
            try (ResultSet rs = s.executeQuery("EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) EXECUTE " + name + "(" + args + ")")) {
                while (rs.next()) {
                    plan.add(rs.getString(1));
                }
            }
            return plan;
        }
    }

    private static void assertIndexOnly(List<String> plan, String index) {
        List<String> scans = plan.stream().filter(l -> l.contains("scoring_log") && l.contains("Scan")).toList();
        assertThat(scans).as("nodos que leen scoring_log:\n" + String.join("\n", plan)).isNotEmpty();
        assertThat(scans).allSatisfy(l -> assertThat(l).contains("Index Only Scan").contains(index));
        assertThat(String.join("\n", plan)).doesNotContain("Seq Scan").doesNotContain("Bitmap").doesNotContain("Rows Removed by Index Recheck");
        // Con el mapa de visibilidad al día, ningún acceso toca el heap.
        assertThat(plan.stream().filter(l -> l.contains("Heap Fetches")).toList())
                .allSatisfy(l -> assertThat(l).endsWith("Heap Fetches: 0"));
    }

    @Test
    void destinationQueryIsIndexOnlyOnIxSlogDest() throws SQLException {
        for (String mode : List.of("force_custom_plan", "force_generic_plan")) {
            assertIndexOnly(explain(mode, FeatureQueries.DESTINATION_SQL, "dest_q_" + mode.substring(6, 9),
                    DEST_ARGS), "ix_slog_dest");
        }
    }

    @Test
    void sourceQueryIsIndexOnlyOnIxSlogSource() throws SQLException {
        for (String mode : List.of("force_custom_plan", "force_generic_plan")) {
            assertIndexOnly(explain(mode, FeatureQueries.SOURCE_SQL, "src_q_" + mode.substring(6, 9), SOURCE_ARGS), "ix_slog_source");
        }
    }
}
