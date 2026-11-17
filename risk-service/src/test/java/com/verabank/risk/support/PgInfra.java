package com.verabank.risk.support;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.List;
import java.util.stream.Stream;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.utility.MountableFile;

/**
 * PostgreSQL compartido por toda la suite (una vez por JVM), preparado como lo deja {@code make up-core}: roles de
 * db/init, esquema audit (V9–V13), esquema risk (V1–V10, con el dueño verabank_risk) y db/init/grants.sql.
 * El servicio se conecta como risk_svc, igual que en local.
 */
public final class PgInfra {

    public static final String DB = "verabank";
    public static final String ADMIN = "verabank_admin";
    public static final String ADMIN_PASSWORD = "admin-test-pw";
    public static final String RISK_OWNER = "verabank_risk";
    public static final String RISK_OWNER_PASSWORD = "risk-test-pw";
    public static final String SERVICE_USER = "risk_svc";
    public static final String SERVICE_PASSWORD = "risk-svc-test-pw";

    public static final PostgreSQLContainer<?> POSTGRES;

    static {
        Path init = Dirs.dir("test.db.dir").resolve("init");
        POSTGRES = new PostgreSQLContainer<>("postgres:16.16")
                .withDatabaseName(DB)
                .withUsername(ADMIN)
                .withPassword(ADMIN_PASSWORD)
                .withEnv("VERABANK_TRANSFER_PASSWORD", "transfer-test-pw")
                .withEnv("VERABANK_RISK_PASSWORD", RISK_OWNER_PASSWORD)
                .withEnv("VERABANK_BI_PASSWORD", "bi-test-pw")
                .withEnv("RISK_SVC_PASSWORD", SERVICE_PASSWORD)
                .withEnv("SIM_LOADER_PASSWORD", "sim-loader-test-pw")
                .withCopyFileToContainer(MountableFile.forHostPath(init.resolve("initdb.d/10-roles.sh"), 0755),
                        "/docker-entrypoint-initdb.d/10-roles.sh")
                .withCopyFileToContainer(MountableFile.forHostPath(init.resolve("initdb.d/20-roles-runtime.sh"), 0755),
                        "/docker-entrypoint-initdb.d/20-roles-runtime.sh")
                .withCopyFileToContainer(MountableFile.forHostPath(init.resolve("roles.sql"), 0644), "/sql/roles.sql")
                .withCopyFileToContainer(MountableFile.forHostPath(init.resolve("roles_runtime.sql"), 0644),
                        "/sql/roles_runtime.sql");
        POSTGRES.start();
        try {
            Path migrations = Dirs.dir("test.db.dir").resolve("migrations");
            migrate(migrations.resolve("audit"), ADMIN, ADMIN_PASSWORD);
            migrate(migrations.resolve("risk"), RISK_OWNER, RISK_OWNER_PASSWORD);
            try (Connection c = connect(ADMIN, ADMIN_PASSWORD); Statement s = c.createStatement()) {
                s.execute(Files.readString(init.resolve("grants.sql")));
            }
        } catch (IOException | SQLException e) {
            throw new IllegalStateException("no se pudo preparar la base de pruebas", e);
        }
    }

    private PgInfra() {
    }

    public static void init() {
        // Fuerza la carga de la clase (y el arranque del contenedor).
    }

    public static String reactiveUrl() {
        return "postgresql://" + POSTGRES.getHost() + ":" + POSTGRES.getMappedPort(5432) + "/" + DB;
    }

    public static Connection admin() throws SQLException {
        return connect(ADMIN, ADMIN_PASSWORD);
    }

    public static Connection connect(String user, String password) throws SQLException {
        // Sin DriverManager: bajo el cargador de clases de Quarkus no encuentra el controlador registrado.
        java.util.Properties credentials = new java.util.Properties();
        credentials.setProperty("user", user);
        credentials.setProperty("password", password);
        return new org.postgresql.Driver().connect(POSTGRES.getJdbcUrl(), credentials);
    }

    /** Vacía la bitácora entre pruebas. */
    public static void truncateLog() {
        try (Connection c = admin(); Statement s = c.createStatement()) {
            s.execute("TRUNCATE risk.scoring_log");
        } catch (SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    public static Path dir(String property) {
        return Dirs.dir(property);
    }

    private static void migrate(Path directory, String user, String password) throws IOException, SQLException {
        List<Path> files;
        try (Stream<Path> list = Files.list(directory)) {
            files = list.filter(p -> p.getFileName().toString().matches("V\\d+__.*\\.sql"))
                    .sorted((a, b) -> Integer.compare(version(a), version(b))).toList();
        }
        try (Connection c = connect(user, password); Statement s = c.createStatement()) {
            for (Path file : files) {
                s.execute(Files.readString(file));
            }
        }
    }

    private static int version(Path file) {
        String name = file.getFileName().toString();
        return Integer.parseInt(name.substring(1, name.indexOf("__")));
    }
}
