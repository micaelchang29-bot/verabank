package com.verabank.risk.scoring;

import static io.restassured.RestAssured.given;
import static org.assertj.core.api.Assertions.assertThat;
import static org.hamcrest.Matchers.containsString;

import io.quarkus.arc.Arc;
import io.quarkus.test.junit.QuarkusTest;
import io.quarkus.test.junit.QuarkusTestProfile;
import io.quarkus.test.junit.TestProfile;
import java.util.Map;
import org.junit.jupiter.api.Test;

/** Con un tiempo máximo imposible el calentamiento nunca termina: la readiness no sube aunque el modelo esté cargado. */
@QuarkusTest
@TestProfile(WarmUpPendingIT.NoTimeToWarmUp.class)
class WarmUpPendingIT {

    public static class NoTimeToWarmUp implements QuarkusTestProfile {
        @Override
        public Map<String, String> getConfigOverrides() {
            return Map.of("verabank.scoring.warmup-timeout", "1ms");
        }
    }

    @Test
    void readinessIsDownWhileTheWarmUpHasNotFinished() {
        assertThat(Arc.container().select(ModelRegistry.class).get().state().loaded()).isTrue();
        given().get("/q/health/ready").then().statusCode(503).body(containsString("pendiente"));
        assertThat(Arc.container().select(ScoringStartup.class).get().isWarmed()).isFalse();
        given().get("/q/health/live").then().statusCode(200);
    }
}
