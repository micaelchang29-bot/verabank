package com.verabank.risk.scoring;

import io.quarkus.runtime.StartupEvent;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.enterprise.event.Observes;
import jakarta.enterprise.inject.Instance;
import jakarta.inject.Inject;
import java.time.Duration;
import java.util.concurrent.atomic.AtomicBoolean;
import org.eclipse.microprofile.config.inject.ConfigProperty;
import org.jboss.logging.Logger;

/**
 * Con el rol scoring carga y valida los artefactos del modelo al arrancar, calienta el pool de PostgreSQL y recorre
 * la ruta completa de scoring con una solicitud sintética; con otro rol no toca nada. Hasta terminar el calentamiento
 * {@link ScoringReadiness} responde que no está listo: sin él, las primeras solicitudes reales de un proceso frío
 * pasan de los 120 ms de {@code transfer-service} y caen en fallback.
 */
@ApplicationScoped
class ScoringStartup {

    private static final Logger LOG = Logger.getLogger(ScoringStartup.class);

    @Inject
    Instance<ModelRegistry> registry;
    @Inject
    Instance<ScoreLog> log;
    @Inject
    Instance<ScoringService> service;
    @ConfigProperty(name = "quarkus.datasource.\"scoring\".reactive.max-size", defaultValue = "10")
    int poolSize;
    @ConfigProperty(name = "verabank.scoring.warmup-rounds", defaultValue = "200")
    int rounds;

    private final AtomicBoolean warmed = new AtomicBoolean();
    private final AtomicBoolean running = new AtomicBoolean();

    void onStart(@Observes StartupEvent event) {
        if (!registry.isResolvable()) {
            warmed.set(true);   // sin rol scoring no hay nada que calentar
            return;
        }
        registry.get().load();
        warmUp();
    }

    /** Calentamiento completo; si la base no estaba lista, la readiness lo reintenta con {@link #retryWarmUp()}. */
    boolean warmUp() {
        long start = System.nanoTime();
        try {
            log.get().warmUp(poolSize).await().atMost(Duration.ofSeconds(5));
            LOG.infof("pool de PostgreSQL calentado (%d conexiones)", poolSize);
            if (registry.get().state().loaded()) {
                service.get().warmUp(rounds).await().atMost(Duration.ofSeconds(30));
                LOG.infof("ruta de scoring calentada (%d pasadas, %d ms)", rounds, (System.nanoTime() - start) / 1_000_000);
            }
            warmed.set(true);
        } catch (RuntimeException e) {
            // La base puede no estar lista todavía: el servicio arranca y la readiness lo reintentará.
            LOG.warnf("no se pudo calentar el servicio: %s", e.toString());
        }
        return warmed.get();
    }

    boolean isWarmed() {
        return warmed.get();
    }

    /** Un solo reintento a la vez, para que varias comprobaciones de readiness no lo repitan en paralelo. */
    boolean retryWarmUp() {
        if (warmed.get()) {
            return true;
        }
        if (!running.compareAndSet(false, true)) {
            return false;
        }
        try {
            return warmUp();
        } finally {
            running.set(false);
        }
    }
}
