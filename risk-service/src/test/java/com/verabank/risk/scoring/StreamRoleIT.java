package com.verabank.risk.scoring;

import static io.restassured.RestAssured.given;
import static org.assertj.core.api.Assertions.assertThat;
import static org.hamcrest.Matchers.equalTo;

import com.verabank.risk.support.Requests;
import io.quarkus.arc.Arc;
import io.quarkus.test.junit.QuarkusTest;
import io.quarkus.test.junit.QuarkusTestProfile;
import io.quarkus.test.junit.TestProfile;
import io.restassured.http.ContentType;
import io.smallrye.reactive.messaging.kafka.KafkaClientService;
import java.util.Map;
import java.util.UUID;
import org.junit.jupiter.api.Test;

/** D3: la misma imagen con {@code VERABANK_ROLE=stream} no activa el scoring (ni carga el modelo ni mide etapas). */
@QuarkusTest
@TestProfile(StreamRoleIT.StreamRole.class)
class StreamRoleIT {

    public static class StreamRole implements QuarkusTestProfile {
        @Override
        public Map<String, String> getConfigOverrides() {
            return Map.of("verabank.role", "stream", "verabank.api-keys", "ops:ops-test-key-0123456789");
        }
    }

    @Test
    void theScoringRouteDoesNotExistAndTheModelIsNotLoaded() {
        given().contentType(ContentType.JSON).body(Requests.valid(UUID.randomUUID())).post("/internal/v1/scores")
                .then().statusCode(404).contentType("application/problem+json").body("type", equalTo("not-found"));
        given().get("/q/health/ready").then().statusCode(200).body("status", equalTo("UP"));
        assertThat(given().get("/q/metrics").then().statusCode(200).extract().asString()).doesNotContain("scoring_stage_duration");
    }

    @Test
    void theModelIsNotLoadedNorIsTheScoringStateCreated() {
        // D3: con el rol stream no hay ModelRegistry (único punto que crea la sesión de ONNX Runtime) ni lecturas del modelo.
        assertThat(Arc.container().select(ModelRegistry.class).isResolvable()).isFalse();
        assertThat(Arc.container().select(ScoringService.class).isResolvable()).isFalse();
    }

    @Test
    void streamOpensItsConsumersAndNothingElseDoes() {
        KafkaClientService kafka = Arc.container().select(KafkaClientService.class).get();
        assertThat(kafka.getConsumer("transfer-events")).isNotNull();
        assertThat(kafka.getConsumer("fraud-labels")).isNotNull();
    }

    @Test
    void readinessReportsDatabaseAndKafka() {
        given().get("/q/health/ready").then().statusCode(200).body("status", equalTo("UP"))
                .body("checks.find { it.name == 'stream' }.data.database", equalTo("UP"))
                .body("checks.find { it.name == 'stream' }.data.kafka", equalTo("UP"));
    }
}
