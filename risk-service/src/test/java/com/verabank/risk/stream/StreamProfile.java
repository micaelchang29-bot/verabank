package com.verabank.risk.stream;

import io.quarkus.test.junit.QuarkusTestProfile;
import java.util.Map;

/** Rol stream para todas las pruebas del paquete: un solo arranque de Quarkus para toda la serie. */
public class StreamProfile implements QuarkusTestProfile {

    public static final String KEY_OPS = "ops-test-key-0123456789";
    public static final String KEY_COMPLIANCE = "compliance-key-0123456789";

    @Override
    public Map<String, String> getConfigOverrides() {
        return Map.of(
                "verabank.role", "stream",
                "verabank.stream.retry-backoff", "100ms",
                "verabank.api-keys", "ops:" + KEY_OPS + ",compliance:" + KEY_COMPLIANCE,
                // Los grupos arrancan siempre desde el principio de los tópicos compartidos de la suite.
                "mp.messaging.incoming.transfer-events.group.id", "risk-stream",
                "quarkus.log.category.\"com.verabank.risk.stream\".level", "INFO");
    }
}
