package com.verabank.risk.scoring;

import io.smallrye.health.api.AsyncHealthCheck;
import io.smallrye.mutiny.Uni;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.enterprise.inject.Instance;
import jakarta.inject.Inject;
import java.time.Duration;
import org.eclipse.microprofile.health.HealthCheckResponse;
import org.eclipse.microprofile.health.Readiness;

/** Con el rol scoring, lista = modelo cargado y validado + PostgreSQL alcanzable. */
@Readiness
@ApplicationScoped
class ScoringReadiness implements AsyncHealthCheck {

    private static final String NAME = "scoring";

    @Inject
    Instance<ModelRegistry> registry;
    @Inject
    Instance<ScoreLog> log;
    @Inject
    ScoringStartup startup;

    @Override
    public Uni<HealthCheckResponse> call() {
        if (!registry.isResolvable()) {
            return Uni.createFrom().item(HealthCheckResponse.up("rol sin scoring"));
        }
        ModelState state = registry.get().state();
        if (!state.loaded()) {
            return Uni.createFrom().item(HealthCheckResponse.named(NAME).down()
                    .withData("model", "no cargado").withData("problems", state.problems().size()).build());
        }
        if (!startup.isWarmed()) {
            // Calentamiento pendiente (p. ej. la base no estaba lista al arrancar): se reintenta fuera del hilo de E/S.
            return Uni.createFrom().item(() -> startup.retryWarmUp())
                    .runSubscriptionOn(io.smallrye.mutiny.infrastructure.Infrastructure.getDefaultWorkerPool())
                    .map(done -> done
                            ? HealthCheckResponse.named(NAME).up().withData("model", state.champion().modelVersion()).build()
                            : HealthCheckResponse.named(NAME).down().withData("warmup", "pendiente").build());
        }
        return log.get().ping().ifNoItem().after(Duration.ofSeconds(1)).fail()
                .map(v -> HealthCheckResponse.named(NAME).up().withData("model", state.champion().modelVersion()).build())
                .onFailure().recoverWithItem(e -> HealthCheckResponse.named(NAME).down().withData("database", "inaccesible").build());
    }
}
