package com.verabank.risk.scoring;

import io.quarkus.runtime.StartupEvent;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.enterprise.event.Observes;
import jakarta.enterprise.inject.Instance;
import jakarta.inject.Inject;
import java.time.Duration;
import java.util.concurrent.atomic.AtomicBoolean;
import org.eclipse.microprofile.config.inject.ConfigProperty;
import com.verabank.risk.shared.RiskConfig;
import org.jboss.logging.Logger;

/**
 * Con el rol scoring carga y valida los artefactos del modelo al arrancar y calienta antes de declararse listo: cada
 * conexión del pool de PostgreSQL ejecuta una consulta de cada tipo de la ruta de scoring (con el INSERT revertido) y
 * después {@code verabank.scoring.warmup-rounds} pasadas sintéticas completas, con inferencia, recorren la ruta; todo
 * dentro de {@code verabank.scoring.warmup-timeout}. Con otro rol no toca nada. Hasta terminar, {@link ScoringReadiness}
 * responde que no está listo: sin él, las primeras solicitudes reales de un proceso frío pasan de los 120 ms de
 * {@code transfer-service} y caen en fallback. No escribe en {@code risk.scoring_log}.
 */
@ApplicationScoped
class ScoringStartup {

    private static final Logger LOG = Logger.getLogger(ScoringStartup.class);

    @Inject
    Instance<ModelRegistry> registry;
    @Inject
    Instance<ScoringService> service;
    @Inject
    Instance<ScoringMetrics> metrics;
    @ConfigProperty(name = "quarkus.datasource.\"scoring\".reactive.max-size", defaultValue = "10")
    int poolSize;
    @Inject
    RiskConfig config;

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
        long deadline = start + config.scoring().warmupTimeout().toNanos();
        try {
            if (!registry.get().state().loaded()) {
                return false;                  // sin modelo no hay readiness que dar
            }
            metrics.get().ensureRegistered();   // histogramas de etapa y contadores creados antes de la primera solicitud
            int connections = service.get().warmConnections(poolSize).await().atMost(remaining(deadline));
            LOG.infof("pool de PostgreSQL calentado: %d conexiones con la ruta de scoring", connections);
            service.get().warmUp(config.scoring().warmupRounds()).await().atMost(remaining(deadline));
            LOG.infof("ruta de scoring calentada (%d pasadas, %d ms en total)", config.scoring().warmupRounds(), (System.nanoTime() - start) / 1_000_000);
            warmed.set(true);
        } catch (RuntimeException e) {
            // La base puede no estar lista todavía: el servicio arranca y la readiness lo reintentará.
            LOG.warnf("no se pudo calentar el servicio: %s", e.toString());
        }
        return warmed.get();
    }

    private static Duration remaining(long deadline) {
        return Duration.ofNanos(Math.max(1, deadline - System.nanoTime()));
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
