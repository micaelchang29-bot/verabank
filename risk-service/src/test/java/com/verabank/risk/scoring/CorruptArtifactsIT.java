package com.verabank.risk.scoring;

import static io.restassured.RestAssured.given;
import static org.hamcrest.Matchers.equalTo;
import static org.hamcrest.Matchers.matchesPattern;

import com.verabank.risk.support.ArtifactDirs;
import com.verabank.risk.support.Requests;
import io.quarkus.test.junit.QuarkusTest;
import io.quarkus.test.junit.QuarkusTestProfile;
import io.quarkus.test.junit.TestProfile;
import io.restassured.http.ContentType;
import java.util.Map;
import java.util.UUID;
import org.junit.jupiter.api.Test;

/** Un model.onnx alterado (su SHA-256 ya no coincide) deja el servicio sin modelo: 503 y readiness caída. */
@QuarkusTest
@TestProfile(CorruptArtifactsIT.CorruptModel.class)
class CorruptArtifactsIT {

    public static class CorruptModel implements QuarkusTestProfile {
        @Override
        public Map<String, String> getConfigOverrides() {
            return Map.of("verabank.artifacts.dir", ArtifactDirs.withCorruptModel().toString());
        }
    }

    @Test
    void scoringIsModelNotLoadedAndReadinessIsDown() {
        given().contentType(ContentType.JSON).body(Requests.valid(UUID.randomUUID())).post("/internal/v1/scores")
                .then().statusCode(503).contentType("application/problem+json")
                .body("type", equalTo("model-not-loaded")).body("status", equalTo(503))
                .body("correlation_id", matchesPattern("[0-9a-f-]{36}"));
        given().get("/q/health/ready").then().statusCode(503).body("status", equalTo("DOWN"));
        given().get("/q/health/live").then().statusCode(200);
    }

    @Test
    void validationStillComesFirstSoABadBodyIsStillA400() {
        given().contentType(ContentType.JSON).body("{}").post("/internal/v1/scores").then().statusCode(400)
                .body("type", equalTo("validation-error"));
    }
}
