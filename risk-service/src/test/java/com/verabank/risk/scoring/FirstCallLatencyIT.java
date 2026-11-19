package com.verabank.risk.scoring;

import static org.assertj.core.api.Assertions.assertThat;

import com.verabank.risk.support.PgInfra;
import io.micrometer.core.instrument.MeterRegistry;
import io.quarkus.test.common.http.TestHTTPResource;
import io.quarkus.test.junit.QuarkusTest;
import io.quarkus.test.junit.QuarkusTestProfile;
import io.quarkus.test.junit.TestProfile;
import jakarta.inject.Inject;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Test;

/**
 * Arranque en frío: con un perfil propio Quarkus inicia un proceso nuevo para esta clase, así que la llamada medida es
 * la primera solicitud real de scoring de esa instancia. transfer-service espera como máximo 120 ms (ADR-04). El
 * cliente es el HttpClient del JDK, como el de un servicio: se calienta con la readiness (misma conexión keep-alive),
 * que no toca la ruta de scoring; así lo medido es el servidor en frío más el loopback, no el arranque del cliente.
 */
@QuarkusTest
@TestProfile(FirstCallLatencyIT.FreshStart.class)
class FirstCallLatencyIT {

    static final long BUDGET_MS = 120;

    public static class FreshStart implements QuarkusTestProfile {
        @Override
        public Map<String, String> getConfigOverrides() {
            // Cualquier diferencia de configuración obliga a Quarkus a arrancar un proceso propio para esta clase.
            return Map.of("quarkus.log.category.\"com.verabank.risk.scoring.FirstCallLatencyIT\".level", "INFO");
        }
    }

    @TestHTTPResource("/")
    URI base;
    @Inject
    MeterRegistry meters;

    private static String body() {
        return """
                {"transfer_id":"%s","source_ref":"src-cold","dest_ref":"dst-cold","amount":"1250.00","currency":"PEN",
                 "transfer_type":"TRANSFER","dest_country":"PE","occurred_at":"2026-01-10T10:00:00Z"}""".formatted(UUID.randomUUID());
    }

    private HttpRequest score() {
        return HttpRequest.newBuilder(base.resolve("/internal/v1/scores")).timeout(Duration.ofSeconds(10))
                .header("Content-Type", "application/json").POST(HttpRequest.BodyPublishers.ofString(body())).build();
    }

    @Test
    void theFirstRealRequestAfterReadyAnswersWithinTheTransferServiceBudget() throws Exception {
        PgInfra.truncateLog();
        HttpClient client = HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1).build();
        HttpResponse<String> ready = client.send(HttpRequest.newBuilder(base.resolve("/q/health/ready")).build(),
                HttpResponse.BodyHandlers.ofString());
        assertThat(ready.statusCode()).isEqualTo(200);

        long start = System.nanoTime();
        HttpResponse<String> first = client.send(score(), HttpResponse.BodyHandlers.ofString());
        long firstMs = (System.nanoTime() - start) / 1_000_000;
        double serverMs = meters.find("scoring.stage.duration").tag("stage", "total").timers().stream()
                .mapToDouble(t -> t.max(TimeUnit.MILLISECONDS)).max().orElse(-1);

        long second = System.nanoTime();
        assertThat(client.send(score(), HttpResponse.BodyHandlers.ofString()).statusCode()).isEqualTo(200);
        long secondMs = (System.nanoTime() - second) / 1_000_000;

        System.out.printf("primera solicitud de scoring tras ready: %d ms (HTTP %d; etapa total del servicio %.1f ms); "
                + "segunda: %d ms%n", firstMs, first.statusCode(), serverMs, secondMs);
        assertThat(first.statusCode()).isEqualTo(200);
        assertThat(firstMs).as("primera llamada tras ready, ms").isLessThan(BUDGET_MS);
    }
}
