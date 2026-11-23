package com.verabank.risk.scoring;

import static io.restassured.RestAssured.given;
import static org.assertj.core.api.Assertions.assertThat;
import static org.awaitility.Awaitility.await;

import com.verabank.risk.support.FlakyPgProxy;
import com.verabank.risk.support.PgInfra;
import io.quarkus.arc.Arc;
import io.quarkus.test.junit.QuarkusTestProfile;
import java.time.Duration;
import java.util.Map;
import org.eclipse.microprofile.config.ConfigProvider;

/**
 * D16: si al arrancar solo una parte del pool se abre a tiempo (la base aún no estaba lista), el primer calentamiento
 * falla, pero no puede quedarse con las conexiones que sí abrió ni con las que llegan tarde. Antes se quedaban fuera del
 * pool para siempre: cada reintento de la readiness esperaba conexiones que nadie devolvía y el pod no llegaba a Ready
 * (kind, 22 y 23 de noviembre de 2026). Cada escenario arranca su propia aplicación, con el pool detrás de un
 * {@link FlakyPgProxy}.
 */
final class WarmUpRecovery {

    private WarmUpRecovery() {
    }

    static Map<String, String> behind(FlakyPgProxy proxy) {
        return Map.of(
                "quarkus.datasource.\"scoring\".reactive.url", proxy.reactiveUrl(),
                "verabank.scoring.warmup-timeout", "2s",
                "verabank.scoring.warmup-rounds", "20");
    }

    static void assertReadinessRecovers() {
        String url = ConfigProvider.getConfig().getValue("quarkus.datasource.\"scoring\".reactive.url", String.class);
        assertThat(url).doesNotEndWith(":" + PgInfra.POSTGRES.getMappedPort(5432) + "/" + PgInfra.DB);   // va por el proxy

        // Cada reintento cabe en los 2 s del calentamiento; 30 s dan margen para varios.
        await().atMost(Duration.ofSeconds(30)).pollInterval(Duration.ofSeconds(1))
                .untilAsserted(() -> given().get("/q/health/ready").then().statusCode(200));
        assertThat(Arc.container().select(ScoringStartup.class).get().isWarmed()).isTrue();
    }

    /** 3 de las 10 conexiones del primer calentamiento fallan; las 7 restantes se abren y deben volver al pool. */
    public static class SomeConnectionsFail implements QuarkusTestProfile {
        @Override
        public Map<String, String> getConfigOverrides() {
            return behind(FlakyPgProxy.rejecting(3));
        }
    }

    /** 3 de las 10 conexiones tardan más que el calentamiento: se cancela por tiempo y las tardías también vuelven. */
    public static class SomeConnectionsArriveLate implements QuarkusTestProfile {
        @Override
        public Map<String, String> getConfigOverrides() {
            return behind(FlakyPgProxy.stalling(3, Duration.ofSeconds(4)));
        }
    }
}
