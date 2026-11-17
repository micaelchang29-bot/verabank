package com.verabank.risk.shared;

import io.smallrye.config.ConfigMapping;
import io.smallrye.config.WithDefault;
import java.time.Duration;
import java.util.Optional;

/** Configuración de ejecución (se resuelve al arrancar, no al compilar: la imagen es una sola, D3). */
@ConfigMapping(prefix = "verabank")
public interface RiskConfig {

    /** Rol del proceso: {@code scoring} o {@code stream} (VERABANK_ROLE). */
    String role();

    Artifacts artifacts();

    Shadow shadow();

    Scoring scoring();

    Stream stream();

    /** Claves de la API de alertas del rol stream, {@code canal:clave,canal:clave} (igual que transfer-service). */
    Optional<String> apiKeys();

    interface Artifacts {

        /** Directorio con model.onnx, thresholds.json y manifest.json; sin valor, se leen del classpath. */
        Optional<String> dir();
    }

    interface Shadow {

        @WithDefault("false")
        boolean enabled();
    }

    interface Scoring {

        /** Tiempo máximo por consulta a PostgreSQL. */
        @WithDefault("100ms")
        Duration queryTimeout();
    }

    interface Stream {

        /** Derivado del rol ({@link StreamEnabledInterceptor}): solo {@code stream} abre consumidores de Kafka. */
        @WithDefault("false")
        boolean enabled();

        /** Reintentos de un mensaje antes de enviarlo a su {@code .dlt}. */
        @WithDefault("3")
        int retries();

        /** Espera entre reintentos. */
        @WithDefault("1s")
        Duration retryBackoff();

        /** Cada cuánto se vuelven a leer las versiones activas de {@code risk.aml_rules}. */
        @WithDefault("60s")
        Duration rulesRefresh();

        @WithDefault("transfer.events.v1")
        String transferEventsTopic();

        @WithDefault("fraud.labels.v1")
        String fraudLabelsTopic();

        @WithDefault("aml.alerts.v1")
        String amlAlertsTopic();
    }
}
