package com.verabank.risk.support;

import io.quarkus.test.common.QuarkusTestResourceLifecycleManager;
import java.util.Map;

/** Apunta el pool reactivo del servicio al PostgreSQL de pruebas, con el rol risk_svc. */
public class PostgresResource implements QuarkusTestResourceLifecycleManager {

    @Override
    public Map<String, String> start() {
        PgInfra.init();
        return Map.of(
                "quarkus.datasource.\"scoring\".reactive.url", PgInfra.reactiveUrl(),
                "quarkus.datasource.\"scoring\".username", PgInfra.SERVICE_USER,
                "quarkus.datasource.\"scoring\".password", PgInfra.SERVICE_PASSWORD);
    }

    @Override
    public void stop() {
        // El contenedor es compartido por toda la suite y lo detiene Ryuk al terminar la JVM.
    }
}
