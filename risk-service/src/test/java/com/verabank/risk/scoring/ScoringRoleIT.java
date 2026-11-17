package com.verabank.risk.scoring;

import static io.restassured.RestAssured.given;
import static org.assertj.core.api.Assertions.assertThat;

import io.quarkus.arc.Arc;
import io.quarkus.test.junit.QuarkusTest;
import io.smallrye.reactive.messaging.kafka.KafkaClientService;
import org.junit.jupiter.api.Test;

/** Contraparte de StreamRoleIT: el rol scoring (el de por defecto) no abre ningún consumidor de Kafka ni la API de alertas. */
@QuarkusTest
class ScoringRoleIT {

    @Test
    void scoringLoadsTheModelAndOpensNoKafkaConsumer() {
        assertThat(Arc.container().select(ModelRegistry.class).isResolvable()).isTrue();
        KafkaClientService kafka = Arc.container().select(KafkaClientService.class).get();
        assertThat(kafka.getConsumer("transfer-events")).isNull();
        assertThat(kafka.getConsumer("fraud-labels")).isNull();
    }

    @Test
    void theAlertsApiDoesNotExistWithTheScoringRole() {
        given().get("/api/v1/aml/alerts").then().statusCode(404);
    }
}
