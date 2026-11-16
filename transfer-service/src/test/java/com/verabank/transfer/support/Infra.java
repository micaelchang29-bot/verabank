package com.verabank.transfer.support;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import org.apache.kafka.clients.admin.Admin;
import org.apache.kafka.clients.admin.AdminClientConfig;
import org.apache.kafka.clients.admin.NewTopic;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.kafka.KafkaContainer;
import org.testcontainers.utility.MountableFile;

/**
 * Contenedores compartidos por toda la suite: se arrancan una vez por JVM.
 *
 * <p>PostgreSQL se inicializa con los mismos scripts de roles que Compose
 * ({@code db/init}), y el servicio migra y opera como {@code verabank_transfer}, igual que
 * en local. Kafka usa la imagen del Compose con la creación automática de tópicos apagada.
 */
public final class Infra {

    public static final String DB_NAME = "verabank";
    public static final String ADMIN_USER = "verabank_admin";
    public static final String ADMIN_PASSWORD = "admin-test-pw";
    public static final String TRANSFER_USER = "verabank_transfer";
    public static final String TRANSFER_PASSWORD = "transfer-test-pw";

    public static final String TRANSFER_EVENTS = "transfer.events.v1";
    public static final String RAIL_COMMANDS = "rail.commands.v1";
    public static final String RAIL_REPLIES = "rail.replies.v1";
    public static final String RAIL_REPLIES_DLT = "rail.replies.v1.dlt";

    public static final PostgreSQLContainer<?> POSTGRES;
    public static final KafkaContainer KAFKA;

    static {
        Path init = repoPath("verabank.db.init.dir", "../db/init");
        POSTGRES = new PostgreSQLContainer<>("postgres:16.16")
                .withDatabaseName(DB_NAME)
                .withUsername(ADMIN_USER)
                .withPassword(ADMIN_PASSWORD)
                .withEnv("VERABANK_TRANSFER_PASSWORD", TRANSFER_PASSWORD)
                .withEnv("VERABANK_RISK_PASSWORD", "risk-test-pw")
                .withEnv("VERABANK_BI_PASSWORD", "bi-test-pw")
                .withCopyFileToContainer(
                        MountableFile.forHostPath(init.resolve("initdb.d/10-roles.sh"), 0755),
                        "/docker-entrypoint-initdb.d/10-roles.sh")
                .withCopyFileToContainer(
                        MountableFile.forHostPath(init.resolve("roles.sql"), 0644), "/sql/roles.sql");
        KAFKA = new KafkaContainer("apache/kafka:4.3.2")
                .withEnv("KAFKA_AUTO_CREATE_TOPICS_ENABLE", "false");
        POSTGRES.start();
        KAFKA.start();
        // Como deploy/kafka/create-topics.sh: los DLT tienen una sola partición (§3.1).
        createTopics(Map.of(TRANSFER_EVENTS, 6, RAIL_COMMANDS, 3, RAIL_REPLIES, 3, RAIL_REPLIES_DLT, 1));
    }

    private Infra() {
    }

    public static void init() {
        // Fuerza la carga de la clase (y el arranque de los contenedores).
    }

    public static Path repoPath(String property, String fallback) {
        Path p = Path.of(System.getProperty(property, fallback)).toAbsolutePath().normalize();
        if (!Files.isDirectory(p)) {
            throw new IllegalStateException("no existe el directorio " + p);
        }
        return p;
    }

    public static void createTopics(Map<String, Integer> topics) {
        try (Admin admin = Admin.create(Map.of(
                AdminClientConfig.BOOTSTRAP_SERVERS_CONFIG, KAFKA.getBootstrapServers()))) {
            var existing = admin.listTopics().names().get(30, TimeUnit.SECONDS);
            List<NewTopic> missing = topics.entrySet().stream()
                    .filter(e -> !existing.contains(e.getKey()))
                    .map(e -> new NewTopic(e.getKey(), e.getValue(), (short) 1))
                    .toList();
            if (!missing.isEmpty()) {
                admin.createTopics(missing).all().get(30, TimeUnit.SECONDS);
            }
        } catch (Exception e) {
            throw new IllegalStateException("no se pudieron crear los tópicos " + topics.keySet(), e);
        }
    }
}
