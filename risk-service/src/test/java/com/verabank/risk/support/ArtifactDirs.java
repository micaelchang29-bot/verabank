package com.verabank.risk.support;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;

/** Copias de ml/artifacts en un directorio temporal, para probar artefactos alterados sin tocar los reales. */
public final class ArtifactDirs {

    private ArtifactDirs() {
    }

    public static Path copy() {
        try {
            Path source = Dirs.artifacts();
            Path target = Files.createTempDirectory("verabank-artifacts-");
            for (String name : new String[] {"model.onnx", "thresholds.json", "manifest.json"}) {
                Files.copy(source.resolve(name), target.resolve(name), StandardCopyOption.REPLACE_EXISTING);
            }
            Files.createDirectories(target.resolve("challenger"));
            Files.copy(source.resolve("challenger/model.onnx"), target.resolve("challenger/model.onnx"),
                    StandardCopyOption.REPLACE_EXISTING);
            return target;
        } catch (IOException e) {
            throw new IllegalStateException(e);
        }
    }

    /** Copia con un byte de model.onnx cambiado: el SHA-256 deja de coincidir con thresholds.json y manifest.json. */
    public static Path withCorruptModel() {
        try {
            Path dir = copy();
            byte[] bytes = Files.readAllBytes(dir.resolve("model.onnx"));
            bytes[bytes.length / 2] ^= 0x01;
            Files.write(dir.resolve("model.onnx"), bytes);
            return dir;
        } catch (IOException e) {
            throw new IllegalStateException(e);
        }
    }

    /** Copia con t_block cambiado en thresholds.json (el modelo no cambia). */
    public static Path withChangedThreshold() {
        try {
            Path dir = copy();
            String text = Files.readString(dir.resolve("thresholds.json"));
            String changed = text.replaceFirst("\"t_block\": [0-9.]+", "\"t_block\": 0.5");
            if (changed.equals(text)) {
                throw new IllegalStateException("no se encontró t_block en thresholds.json");
            }
            Files.writeString(dir.resolve("thresholds.json"), changed);
            return dir;
        } catch (IOException e) {
            throw new IllegalStateException(e);
        }
    }
}
