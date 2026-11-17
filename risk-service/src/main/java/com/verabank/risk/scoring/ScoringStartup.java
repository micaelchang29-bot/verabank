package com.verabank.risk.scoring;

import io.quarkus.runtime.StartupEvent;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.enterprise.event.Observes;
import jakarta.enterprise.inject.Instance;
import jakarta.inject.Inject;
import java.time.Duration;
import org.eclipse.microprofile.config.inject.ConfigProperty;
import org.jboss.logging.Logger;

/**
 * Con el rol scoring carga y valida los artefactos del modelo al arrancar y calienta el pool de PostgreSQL; con otro
 * rol no toca nada. Sin el calentamiento, la primera solicitud paga el alta de la conexión dentro de su límite de
 * 100 ms por consulta y puede fallar en un arranque en frío.
 */
@ApplicationScoped
class ScoringStartup {

    private static final Logger LOG = Logger.getLogger(ScoringStartup.class);

    @Inject
    Instance<ModelRegistry> registry;
    @Inject
    Instance<ScoreLog> log;
    @ConfigProperty(name = "quarkus.datasource.\"scoring\".reactive.max-size", defaultValue = "10")
    int poolSize;

    void onStart(@Observes StartupEvent event) {
        if (!registry.isResolvable()) {
            return;
        }
        registry.get().load();
        try {
            log.get().warmUp(poolSize).await().atMost(Duration.ofSeconds(5));
            LOG.infof("pool de PostgreSQL calentado (%d conexiones)", poolSize);
        } catch (RuntimeException e) {
            // La base puede no estar lista todavía: el servicio arranca y la readiness lo dirá.
            LOG.warnf("no se pudo calentar el pool de PostgreSQL: %s", e.toString());
        }
    }
}
