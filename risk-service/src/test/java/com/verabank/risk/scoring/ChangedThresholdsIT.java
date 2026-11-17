package com.verabank.risk.scoring;

import static io.restassured.RestAssured.given;
import static org.hamcrest.Matchers.equalTo;

import com.verabank.risk.support.ArtifactDirs;
import com.verabank.risk.support.Requests;
import io.quarkus.test.junit.QuarkusTest;
import io.quarkus.test.junit.QuarkusTestProfile;
import io.quarkus.test.junit.TestProfile;
import io.restassured.http.ContentType;
import java.util.Map;
import java.util.UUID;
import org.junit.jupiter.api.Test;

/** Un valor cambiado en thresholds.json (el modelo intacto) tampoco arranca: su SHA-256 es el del manifest. */
@QuarkusTest
@TestProfile(ChangedThresholdsIT.ChangedThreshold.class)
class ChangedThresholdsIT {

    public static class ChangedThreshold implements QuarkusTestProfile {
        @Override
        public Map<String, String> getConfigOverrides() {
            return Map.of("verabank.artifacts.dir", ArtifactDirs.withChangedThreshold().toString());
        }
    }

    @Test
    void aTamperedThresholdFileLeavesTheServiceWithoutModel() {
        given().contentType(ContentType.JSON).body(Requests.valid(UUID.randomUUID())).post("/internal/v1/scores")
                .then().statusCode(503).body("type", equalTo("model-not-loaded"));
        given().get("/q/health/ready").then().statusCode(503);
    }
}
