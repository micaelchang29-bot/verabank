package com.verabank.risk.stream.messaging;

import com.verabank.risk.shared.RiskConfig;
import io.quarkus.reactive.datasource.ReactiveDataSource;
import io.smallrye.health.api.AsyncHealthCheck;
import io.smallrye.mutiny.Uni;
import io.vertx.mutiny.pgclient.PgPool;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.inject.Inject;
import java.time.Duration;
import org.eclipse.microprofile.health.HealthCheckResponse;
import org.eclipse.microprofile.health.HealthCheckResponseBuilder;
import org.eclipse.microprofile.health.Readiness;

/** Con el rol stream, lista = PostgreSQL alcanzable + Kafka alcanzable. Con otro rol no aporta nada. */
@Readiness
@ApplicationScoped
class StreamReadiness implements AsyncHealthCheck {

    private static final Duration TIMEOUT = Duration.ofSeconds(2);

    @Inject
    RiskConfig config;
    @Inject
    @ReactiveDataSource("stream")
    PgPool pool;
    @Inject
    KafkaGateway kafka;

    @Override
    public Uni<HealthCheckResponse> call() {
        if (!"stream".equals(config.role())) {
            return Uni.createFrom().item(HealthCheckResponse.up("rol sin stream"));
        }
        Uni<Boolean> database = pool.query("SELECT 1").execute().ifNoItem().after(TIMEOUT).fail()
                .replaceWith(true).onFailure().recoverWithItem(false);
        Uni<Boolean> broker = kafka.ping(TIMEOUT).replaceWith(true).onFailure().recoverWithItem(false);
        return Uni.combine().all().unis(database, broker).with((db, kf) -> {
            HealthCheckResponseBuilder b = HealthCheckResponse.named("stream")
                    .withData("database", db ? "UP" : "inaccesible").withData("kafka", kf ? "UP" : "inaccesible");
            return (db && kf ? b.up() : b.down()).build();
        });
    }
}
