package com.verabank.risk.scoring;

import static io.restassured.RestAssured.given;
import static org.assertj.core.api.Assertions.assertThat;

import com.verabank.risk.support.PgInfra;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Timer;
import io.quarkus.arc.Arc;
import io.quarkus.test.junit.QuarkusTest;
import jakarta.inject.Inject;
import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.time.Duration;
import org.junit.jupiter.api.Test;

/**
 * El calentamiento de arranque recorre la ruta completa (lectura, features, inferencia, INSERT) sin dejar filas en
 * {@code risk.scoring_log} (el INSERT va en una transacción que se revierte) ni tocar las métricas de decisiones y de
 * etapas, y la readiness solo sube después de él.
 */
@QuarkusTest
class WarmUpIT {

    @Inject
    MeterRegistry meters;

    private static long rows() throws SQLException {
        try (Connection c = PgInfra.admin(); Statement s = c.createStatement();
                ResultSet rs = s.executeQuery("SELECT count(*) FROM risk.scoring_log")) {
            rs.next();
            return rs.getLong(1);
        }
    }

    private double decisions() {
        return meters.find("scoring.decisions").counters().stream().mapToDouble(c -> c.count()).sum();
    }

    private long stageSamples() {
        return meters.find("scoring.stage.duration").timers().stream().mapToLong(Timer::count).sum();
    }

    @Test
    void warmUpLeavesNoRowsAndNoTraceInTheMetrics() throws SQLException {
        long rowsBefore = rows();
        double decisionsBefore = decisions();
        long stagesBefore = stageSamples();

        Arc.container().select(ScoringService.class).get().warmUp(25).await().atMost(Duration.ofSeconds(30));

        assertThat(rows()).isEqualTo(rowsBefore);
        assertThat(decisions()).isEqualTo(decisionsBefore);
        assertThat(stageSamples()).isEqualTo(stagesBefore);
    }

    @Test
    void warmUpRunsOnTheRealInsertAndIsRolledBack() throws SQLException {
        // Si el INSERT fallara (columna, permiso, restricción) el calentamiento lanzaría: prueba que la ruta es la real.
        Arc.container().select(ScoringService.class).get().warmUp(3).await().atMost(Duration.ofSeconds(30));
        try (Connection c = PgInfra.admin(); Statement s = c.createStatement();
                ResultSet rs = s.executeQuery("SELECT count(*) FROM risk.scoring_log WHERE source_ref = 'warmup-source'")) {
            rs.next();
            assertThat(rs.getLong(1)).isZero();
        }
    }

    @Test
    void theServiceIsReadyOnlyAfterWarmingUp() {
        assertThat(Arc.container().select(ScoringStartup.class).get().isWarmed()).isTrue();
        given().get("/q/health/ready").then().statusCode(200);
    }
}
