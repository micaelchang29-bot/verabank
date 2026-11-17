package com.verabank.risk.shared;

import io.quarkus.runtime.StartupEvent;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.enterprise.event.Observes;
import jakarta.inject.Inject;
import java.util.Set;
import org.jboss.logging.Logger;

/** El rol se elige en tiempo de ejecución (VERABANK_ROLE=scoring|stream, D3); cualquier otro valor impide arrancar. */
@ApplicationScoped
class RoleCheck {

    private static final Logger LOG = Logger.getLogger(RoleCheck.class);
    private static final Set<String> ROLES = Set.of("scoring", "stream");

    @Inject
    RiskConfig config;

    void onStart(@Observes StartupEvent event) {
        if (!ROLES.contains(config.role())) {
            throw new IllegalStateException("VERABANK_ROLE debe ser scoring o stream, no '" + config.role() + "'");
        }
        LOG.infof("risk-service arranca con el rol %s", config.role());
    }
}
