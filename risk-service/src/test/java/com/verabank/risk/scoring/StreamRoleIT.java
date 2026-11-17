package com.verabank.risk.scoring;

import static io.restassured.RestAssured.given;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.hamcrest.Matchers.equalTo;

import com.verabank.risk.support.Requests;
import io.quarkus.test.junit.QuarkusTest;
import io.quarkus.test.junit.QuarkusTestProfile;
import io.quarkus.test.junit.TestProfile;
import io.restassured.http.ContentType;
import java.util.Map;
import java.util.UUID;
import org.eclipse.microprofile.config.ConfigProvider;
import org.junit.jupiter.api.Test;

/**
 * D3: la misma imagen con {@code VERABANK_ROLE=stream} no activa el scoring (ni carga el modelo ni mide etapas), y el
 * rol scoring tampoco abre consumidores Kafka: el servicio no incluye ningún cliente de Kafka.
 */
@QuarkusTest
@TestProfile(StreamRoleIT.StreamRole.class)
class StreamRoleIT {

    public static class StreamRole implements QuarkusTestProfile {
        @Override
        public Map<String, String> getConfigOverrides() {
            return Map.of("verabank.role", "stream");
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
    void theServiceHasNoKafkaClient() {
        assertThatThrownBy(() -> Class.forName("org.apache.kafka.clients.consumer.KafkaConsumer"))
                .isInstanceOf(ClassNotFoundException.class);
        assertThatThrownBy(() -> Class.forName("io.smallrye.reactive.messaging.kafka.KafkaConnector"))
                .isInstanceOf(ClassNotFoundException.class);
        assertThat(ConfigProvider.getConfig().getPropertyNames())
                .noneMatch(name -> name.startsWith("mp.messaging") || name.startsWith("kafka."));
    }
}
