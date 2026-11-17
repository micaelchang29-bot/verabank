package com.verabank.risk.support;

import io.quarkus.test.common.QuarkusTestResourceLifecycleManager;
import java.util.Map;

/** Apunta los pools reactivos (scoring y stream) al PostgreSQL de pruebas, con el rol risk_svc, y Kafka al contenedor. */
public class PostgresResource implements QuarkusTestResourceLifecycleManager {

    @Override
    public Map<String, String> start() {
        PgInfra.init();
        KafkaInfra.init();
        return Map.of(
                "quarkus.datasource.\"scoring\".reactive.url", PgInfra.reactiveUrl(),
                "quarkus.datasource.\"scoring\".username", PgInfra.SERVICE_USER,
                "quarkus.datasource.\"scoring\".password", PgInfra.SERVICE_PASSWORD,
                "quarkus.datasource.\"stream\".reactive.url", PgInfra.reactiveUrl(),
                "quarkus.datasource.\"stream\".username", PgInfra.SERVICE_USER,
                "quarkus.datasource.\"stream\".password", PgInfra.SERVICE_PASSWORD,
                "kafka.bootstrap.servers", KafkaInfra.bootstrap());
    }

    @Override
    public void stop() {
        // El contenedor es compartido por toda la suite y lo detiene Ryuk al terminar la JVM.
    }
}
