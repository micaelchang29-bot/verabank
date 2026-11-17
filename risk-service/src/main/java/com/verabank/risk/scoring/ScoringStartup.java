package com.verabank.risk.scoring;

import io.quarkus.runtime.StartupEvent;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.enterprise.event.Observes;
import jakarta.enterprise.inject.Instance;
import jakarta.inject.Inject;

/** Con el rol scoring carga y valida los artefactos del modelo al arrancar; con otro rol no toca nada. */
@ApplicationScoped
class ScoringStartup {

    @Inject
    Instance<ModelRegistry> registry;

    void onStart(@Observes StartupEvent event) {
        if (registry.isResolvable()) {
            registry.get().load();
        }
    }
}
