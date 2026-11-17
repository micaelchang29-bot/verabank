package com.verabank.risk.stream;

import static com.verabank.risk.stream.StreamSupport.T0;
import static com.verabank.risk.stream.StreamSupport.sendAndWait;
import static io.restassured.RestAssured.given;
import static org.assertj.core.api.Assertions.assertThat;

import com.verabank.risk.support.KafkaInfra;
import io.quarkus.test.junit.QuarkusTest;
import io.quarkus.test.junit.TestProfile;
import java.time.Duration;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
import org.awaitility.Awaitility;
import org.junit.jupiter.api.Test;

/** Métricas del rol stream en /q/metrics: retraso del consumidor, alertas por regla, latencia de persistencia y DLT. */
@QuarkusTest
@TestProfile(StreamProfile.class)
class StreamMetricsIT {

    @Test
    void exposesTheStreamMetrics() {
        StreamDb.reset();
        UUID source = UUID.randomUUID();
        sendAndWait(Events.transfer(source, "12000.00", T0));
        KafkaInfra.send(KafkaInfra.TRANSFER_EVENTS, source + "-metrics", "no es json");
        Awaitility.await().atMost(30, TimeUnit.SECONDS).pollInterval(Duration.ofMillis(100)).until(
                () -> given().get("/q/metrics").asString().contains("stream_dlt_total"));

        String metrics = given().get("/q/metrics").then().statusCode(200).extract().asString();
        assertThat(metrics).contains("stream_consumer_delay_seconds_count{topic=\"transfer.events.v1\"}");
        assertThat(metrics).contains("aml_alerts_raised_total{rule_code=\"THRESHOLD\"}");
        assertThat(metrics).contains("aml_alert_persist_latency_seconds_bucket{le=\"5.0\"}");
        assertThat(metrics).contains("stream_dlt_total{reason=\"invalid\",topic=\"transfer.events.v1\"}");
        assertThat(metrics).contains("stream_processing_attempts_total{consumer=\"risk-stream:transfer-events\"}");
        // Retraso de offsets del consumidor (binder de Kafka).
        assertThat(metrics).contains("kafka_consumer_fetch_manager_records_lag");
    }
}
