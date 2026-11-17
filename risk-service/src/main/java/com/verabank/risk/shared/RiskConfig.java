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
}
