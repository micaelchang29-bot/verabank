package com.verabank.transfer.scoring;

import java.nio.file.Files;
import java.nio.file.Path;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.utility.MountableFile;

/**
 * PostgreSQL propio de las pruebas de scoring remoto, con los mismos roles que el de la suite (db/init).
 *
 * <p>Aislado a propósito: con fallback, TransferScored se emite sin score (D7) y EventContractIT, que lee todo el tópico
 * y supone que cada TransferScored lleva score, fallaría al encontrar estas filas en la base compartida.
 */
final class IsolatedPostgres {

    static final String DB = "verabank";
    static final String ADMIN = "verabank_admin";
    static final String TRANSFER_USER = "verabank_transfer";
    static final String TRANSFER_PASSWORD = "transfer-test-pw";

    static final PostgreSQLContainer<?> POSTGRES;

    static {
        Path init = path("verabank.db.init.dir", "../db/init");
        POSTGRES = new PostgreSQLContainer<>("postgres:16.16")
                .withDatabaseName(DB)
                .withUsername(ADMIN)
                .withPassword("admin-test-pw")
                .withEnv("VERABANK_TRANSFER_PASSWORD", TRANSFER_PASSWORD)
                .withEnv("VERABANK_RISK_PASSWORD", "risk-test-pw")
                .withEnv("VERABANK_BI_PASSWORD", "bi-test-pw")
                .withCopyFileToContainer(MountableFile.forHostPath(init.resolve("initdb.d/10-roles.sh"), 0755),
                        "/docker-entrypoint-initdb.d/10-roles.sh")
                .withCopyFileToContainer(MountableFile.forHostPath(init.resolve("roles.sql"), 0644), "/sql/roles.sql");
        POSTGRES.start();
    }

    private IsolatedPostgres() {
    }

    static Path path(String property, String fallback) {
        Path p = Path.of(System.getProperty(property, fallback)).toAbsolutePath().normalize();
        if (!Files.isDirectory(p)) {
            throw new IllegalStateException("no existe el directorio " + p);
        }
        return p;
    }
}
