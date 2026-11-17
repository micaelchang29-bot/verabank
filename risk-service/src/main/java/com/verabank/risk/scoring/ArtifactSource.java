package com.verabank.risk.scoring;

import com.verabank.risk.shared.RiskConfig;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.inject.Inject;
import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;

/** Lee los artefactos de ml/artifacts: del directorio configurado o, por defecto, del classpath (artifacts/). */
@ApplicationScoped
class ArtifactSource {

    @Inject
    RiskConfig config;

    byte[] read(String name) throws IOException {
        if (config.artifacts().dir().isPresent()) {
            return Files.readAllBytes(Path.of(config.artifacts().dir().get()).resolve(name));
        }
        try (InputStream in = Thread.currentThread().getContextClassLoader().getResourceAsStream("artifacts/" + name)) {
            if (in == null) {
                throw new IOException("no está en el classpath: artifacts/" + name);
            }
            return in.readAllBytes();
        }
    }
}
