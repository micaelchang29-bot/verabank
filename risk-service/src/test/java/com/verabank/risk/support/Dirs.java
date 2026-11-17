package com.verabank.risk.support;

import java.nio.file.Files;
import java.nio.file.Path;

/** Directorios del monorepo que el pom pasa a las pruebas como propiedades del sistema. */
public final class Dirs {

    private Dirs() {
    }

    public static Path dir(String property) {
        Path p = Path.of(System.getProperty(property, "")).toAbsolutePath().normalize();
        if (!Files.isDirectory(p)) {
            throw new IllegalStateException("falta la propiedad " + property + " o no existe " + p);
        }
        return p;
    }

    public static Path artifacts() {
        return dir("test.artifacts.dir");
    }

    public static Path contracts() {
        return dir("test.contracts.dir");
    }
}
