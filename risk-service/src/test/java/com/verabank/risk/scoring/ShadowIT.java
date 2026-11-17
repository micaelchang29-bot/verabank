package com.verabank.risk.scoring;

import static io.restassured.RestAssured.given;
import static org.assertj.core.api.Assertions.assertThat;
import static org.awaitility.Awaitility.await;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.doThrow;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.verabank.risk.support.Db;
import com.verabank.risk.support.Dirs;
import com.verabank.risk.support.PgInfra;
import com.verabank.risk.support.Requests;
import io.micrometer.core.instrument.MeterRegistry;
import io.quarkus.test.junit.QuarkusTest;
import io.quarkus.test.junit.QuarkusTestProfile;
import io.quarkus.test.junit.TestProfile;
import io.quarkus.test.junit.mockito.InjectSpy;
import io.restassured.http.ContentType;
import io.restassured.response.Response;
import jakarta.inject.Inject;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.nio.file.Files;
import java.time.Duration;
import java.util.Map;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

/** D8: con la sombra activa se guarda shadow_score; si el challenger falla, la respuesta no cambia. */
@QuarkusTest
@TestProfile(ShadowIT.ShadowOn.class)
class ShadowIT {

    public static class ShadowOn implements QuarkusTestProfile {
        @Override
        public Map<String, String> getConfigOverrides() {
            return Map.of("verabank.shadow.enabled", "true");
        }
    }

    @Inject
    MeterRegistry registry;
    @InjectSpy
    ShadowScorer shadowScorer;

    @BeforeEach
    void clean() {
        PgInfra.truncateLog();
    }

    private static Response post(UUID id) {
        return given().contentType(ContentType.JSON).body(Requests.valid(id)).post("/internal/v1/scores");
    }

    private double failures() {
        var counter = registry.find("scoring.shadow.failures").counter();
        return counter == null ? 0 : counter.count();
    }

    @Test
    void theChallengerScoreIsStoredAfterTheResponseAndMatchesItsOwnInference() throws Exception {
        UUID id = UUID.randomUUID();
        Response response = post(id).then().statusCode(200).extract().response();
        await().atMost(Duration.ofSeconds(10)).until(
                () -> Db.queryString("SELECT shadow_score FROM risk.scoring_log WHERE transfer_id = ?", id) != null);

        JsonNode thresholds = new ObjectMapper().readTree(Files.readAllBytes(Dirs.artifacts().resolve("thresholds.json")));
        float[] row = new float[17];
        for (int i = 0; i < 17; i++) {
            String name = thresholds.get("feature_order").get(i).asText();
            row[i] = (float) Db.queryDecimal("SELECT (features->>?)::float8 FROM risk.scoring_log WHERE transfer_id = ?", name, id).doubleValue();
        }
        try (OnnxModel challenger = OnnxModel.load(Files.readAllBytes(Dirs.artifacts().resolve("challenger/model.onnx")),
                "features", "probabilities", 1, 17)) {
            BigDecimal expected = new BigDecimal(challenger.score(row)).setScale(5, RoundingMode.HALF_EVEN);
            assertThat(Db.queryDecimal("SELECT shadow_score FROM risk.scoring_log WHERE transfer_id = ?", id)).isEqualByComparingTo(expected);
        }
        // La respuesta es la del champion (no la de la sombra) y la sombra no se sobrescribe en un reintento.
        BigDecimal shadow = Db.queryDecimal("SELECT shadow_score FROM risk.scoring_log WHERE transfer_id = ?", id);
        Response again = post(id).then().statusCode(200).extract().response();
        assertThat(again.jsonPath().getString("score")).isEqualTo(response.jsonPath().getString("score"));
        assertThat(Db.queryDecimal("SELECT shadow_score FROM risk.scoring_log WHERE transfer_id = ?", id)).isEqualByComparingTo(shadow);
        assertThat(failures()).isZero();
    }

    @Test
    void aFailingChallengerNeverChangesTheResponseAndIsCounted() {
        doThrow(new IllegalStateException("el challenger falló a propósito")).when(shadowScorer).run(any(), any());
        double before = failures();
        UUID id = UUID.randomUUID();
        Response response = post(id).then().statusCode(200).extract().response();
        assertThat(response.jsonPath().getString("decision")).isIn("APPROVE", "REVIEW", "BLOCK");
        assertThat(response.jsonPath().getString("model_version")).startsWith("fraud-");
        await().atMost(Duration.ofSeconds(10)).until(() -> failures() > before);
        assertThat(Db.count(id)).isEqualTo(1);
        assertThat(Db.queryString("SELECT shadow_score FROM risk.scoring_log WHERE transfer_id = ?", id)).isNull();
        // El reintento devuelve exactamente lo mismo.
        Response again = post(id).then().statusCode(200).extract().response();
        assertThat(again.jsonPath().getString("score")).isEqualTo(response.jsonPath().getString("score"));
        assertThat(again.jsonPath().getString("decision")).isEqualTo(response.jsonPath().getString("decision"));
    }
}
