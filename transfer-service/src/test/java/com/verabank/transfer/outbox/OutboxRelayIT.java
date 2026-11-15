package com.verabank.transfer.outbox;

import static org.assertj.core.api.Assertions.assertThat;
import static org.awaitility.Awaitility.await;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.verabank.transfer.config.VerabankProperties;
import com.verabank.transfer.support.Bank;
import com.verabank.transfer.support.Http;
import com.verabank.transfer.support.Infra;
import com.verabank.transfer.support.IntegrationTest;
import com.verabank.transfer.support.KafkaProbe;
import java.sql.Connection;
import java.sql.Statement;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.function.Function;
import java.util.stream.Collectors;
import javax.sql.DataSource;
import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.transaction.PlatformTransactionManager;
import org.testcontainers.DockerClientFactory;

/** Salida del relay del outbox: orden por outbox_id, un solo relay activo y caída de Kafka. */
class OutboxRelayIT extends IntegrationTest {

    private static final String ORDER_TOPIC = "relay.order.v1";
    private static final String LOCK_TOPIC = "relay.lock.v1";
    private static final String RACE_TOPIC = "relay.race.v1";
    private static final String OUTAGE_TOPIC = "relay.outage.v1";

    @Autowired
    OutboxWriter writer;
    @Autowired
    OutboxRelay relay;
    @Autowired
    ObjectMapper mapper;
    @Autowired
    DataSource dataSource;
    @Autowired
    KafkaTemplate<String, String> kafka;
    @Autowired
    PlatformTransactionManager txManager;
    @Autowired
    VerabankProperties properties;

    @BeforeAll
    static void topics() {
        Infra.createTopics(Map.of(ORDER_TOPIC, 6, LOCK_TOPIC, 3, RACE_TOPIC, 6, OUTAGE_TOPIC, 6));
    }

    @Test
    void publicaEnOrdenDeOutboxIdPorClaveYSoloUnaVez() {
        Map<UUID, Long> written = write(ORDER_TOPIC, 300, 5);

        List<ConsumerRecord<String, String>> records = readAll(ORDER_TOPIC, written.size());

        assertExactlyOnceInOrder(records, written);
        assertThat(records).allSatisfy(r -> {
            var envelope = json(r.value());
            assertThat(envelope.path("emitted_at").asText()).isNotBlank();
            assertThat(KafkaProbe.header(r, "event_id")).isEqualTo(envelope.path("event_id").asText());
            assertThat(KafkaProbe.header(r, "event_type")).isEqualTo("RelayProbe");
            assertThat(KafkaProbe.header(r, "correlation_id")).isEqualTo(envelope.path("correlation_id").asText());
        });
        assertThat(pending(written)).isZero();
    }

    @Test
    void conElBloqueoConsultivoTomadoNingunRelayPublica() throws Exception {
        try (Connection other = dataSource.getConnection(); Statement st = other.createStatement()) {
            st.execute("SELECT pg_advisory_lock(" + properties.outbox().lockKey() + ")");
            Map<UUID, Long> written;
            try {
                written = write(LOCK_TOPIC, 20, 2);
                assertThat(relay.publishBatch()).as("un relay extra con el bloqueo ocupado").isZero();
                Thread.sleep(1500);   // 15 sondeos del relay programado
                assertThat(pending(written)).as("nadie publicó mientras otro tenía el bloqueo").isEqualTo(20);
            } finally {
                st.execute("SELECT pg_advisory_unlock(" + properties.outbox().lockKey() + ")");
            }
            await().atMost(Duration.ofSeconds(20)).until(() -> pending(written) == 0);
            assertExactlyOnceInOrder(readAll(LOCK_TOPIC, written.size()), written);
        }
    }

    @Test
    void variosRelaysALaVezNoDuplicanNiDesordenan() throws Exception {
        Map<UUID, Long> written = write(RACE_TOPIC, 1000, 7);
        List<OutboxRelay> relays = List.of(relay,
                new OutboxRelay(jdbc, kafka, mapper, txManager, properties),
                new OutboxRelay(jdbc, kafka, mapper, txManager, properties));

        try (ExecutorService pool = Executors.newFixedThreadPool(relays.size())) {
            List<Future<?>> runs = new ArrayList<>();
            for (OutboxRelay r : relays) {
                runs.add(pool.submit(() -> {
                    Instant deadline = Instant.now().plusSeconds(60);
                    while (pending(written) > 0 && Instant.now().isBefore(deadline)) {
                        r.publishBatch();
                    }
                }));
            }
            for (Future<?> f : runs) {
                f.get();
            }
        }

        assertThat(pending(written)).isZero();
        assertExactlyOnceInOrder(readAll(RACE_TOPIC, written.size()), written);
    }

    /** T-10 abreviado: con Kafka detenido la API acepta y el outbox se vacía en orden al volver. */
    @Test
    void trasUnaCaidaDeKafkaReintentaSinPerderNiDuplicar() throws Exception {
        Bank bank = new Bank(jdbc, txManager);
        Http http = new Http(port);
        UUID source = bank.account("1000.00");
        UUID dest = bank.account("0");
        Map<UUID, Long> before = write(OUTAGE_TOPIC, 30, 3);
        await().atMost(Duration.ofSeconds(20)).until(() -> pending(before) == 0);

        var docker = DockerClientFactory.instance().client();
        String kafkaId = Infra.KAFKA.getContainerId();
        docker.pauseContainerCmd(kafkaId).exec();
        Map<UUID, Long> during;
        List<UUID> transfers = new ArrayList<>();
        try {
            during = write(OUTAGE_TOPIC, 60, 3);
            for (int i = 0; i < 5; i++) {
                Http.Response r = http.postTransfer(KEY_WEB, UUID.randomUUID(), Bank.internal(source, dest, "10.00"));
                assertThat(r.status()).as("la API sigue aceptando con Kafka caído").isEqualTo(201);
                transfers.add(UUID.fromString(r.json().path("transfer_id").asText()));
            }
            // Más que request.timeout.ms (10 s): el productor reintenta con Kafka aún caído.
            Thread.sleep(12_000);
            assertThat(pending(during)).as("sin Kafka nada se marca publicado").isEqualTo(during.size());
        } finally {
            docker.unpauseContainerCmd(kafkaId).exec();
        }

        await().atMost(Duration.ofSeconds(90)).until(() -> pending(during) == 0 && pendingFor(transfers) == 0);

        Map<UUID, Long> all = new LinkedHashMap<>(before);
        all.putAll(during);
        assertExactlyOnceInOrder(readAll(OUTAGE_TOPIC, all.size()), all);

        List<ConsumerRecord<String, String>> transferEvents = KafkaProbe.readUntil(Infra.TRANSFER_EVENTS,
                rs -> completedFor(rs, transfers) == transfers.size(), Duration.ofSeconds(30));
        assertThat(completedFor(transferEvents, transfers)).isEqualTo(transfers.size());
        Map<String, Long> perEvent = transferEvents.stream()
                .collect(Collectors.groupingBy(r -> KafkaProbe.header(r, "event_id"), Collectors.counting()));
        assertThat(perEvent.values()).as("ningún evento duplicado").allMatch(n -> n == 1);
    }

    // ---------------------------------------------------------------- ayudas

    /** Escribe n eventos de prueba repartidos en {@code keys} claves; devuelve event_id → outbox_id. */
    private Map<UUID, Long> write(String topic, int n, int keys) {
        Map<UUID, Long> written = new LinkedHashMap<>();
        for (int i = 0; i < n; i++) {
            UUID aggregate = UUID.randomUUID();
            var payload = mapper.createObjectNode().put("seq", i);
            UUID eventId = writer.append(topic, "probe", aggregate, topic + "-key-" + (i % keys), "RelayProbe",
                    Instant.now(), UUID.randomUUID(), payload);
            long outboxId = jdbc.sql("SELECT outbox_id FROM core.outbox WHERE event_id = :e")
                    .param("e", eventId).query(Long.class).single();
            written.put(eventId, outboxId);
        }
        return written;
    }

    private long pending(Map<UUID, Long> written) {
        return jdbc.sql("SELECT count(*) FROM core.outbox WHERE published_at IS NULL AND outbox_id IN (:ids)")
                .param("ids", written.values()).query(Long.class).single();
    }

    private long pendingFor(List<UUID> transfers) {
        return jdbc.sql("SELECT count(*) FROM core.outbox WHERE published_at IS NULL AND aggregate_id IN (:ids)")
                .param("ids", transfers).query(Long.class).single();
    }

    private long completedFor(List<ConsumerRecord<String, String>> records, List<UUID> transfers) {
        var ids = transfers.stream().map(UUID::toString).collect(Collectors.toSet());
        return records.stream()
                .filter(r -> "TransferCompleted".equals(KafkaProbe.header(r, "event_type")))
                .filter(r -> ids.contains(json(r.value()).path("aggregate_id").asText()))
                .count();
    }

    private List<ConsumerRecord<String, String>> readAll(String topic, int expected) {
        return KafkaProbe.readUntil(topic, rs -> rs.size() >= expected, Duration.ofSeconds(60));
    }

    /** Cada evento escrito aparece exactamente una vez y, por clave, en orden de outbox_id. */
    private void assertExactlyOnceInOrder(List<ConsumerRecord<String, String>> records, Map<UUID, Long> written) {
        Function<ConsumerRecord<String, String>, UUID> eventId = r -> UUID.fromString(KafkaProbe.header(r, "event_id"));
        List<ConsumerRecord<String, String>> ours = records.stream()
                .filter(r -> written.containsKey(eventId.apply(r)))
                .toList();
        Map<UUID, Long> counts = ours.stream().collect(Collectors.groupingBy(eventId, Collectors.counting()));
        assertThat(counts.keySet()).as("ningún evento perdido").containsExactlyInAnyOrderElementsOf(written.keySet());
        assertThat(counts.values()).as("ningún evento duplicado").allMatch(c -> c == 1);

        Map<String, List<Long>> byKey = new LinkedHashMap<>();
        for (ConsumerRecord<String, String> r : ours) {   // KafkaProbe conserva el orden de cada partición
            byKey.computeIfAbsent(r.key(), k -> new ArrayList<>()).add(written.get(eventId.apply(r)));
        }
        byKey.forEach((key, ids) -> assertThat(ids).as("orden de la clave %s", key).isSorted());
    }

    private com.fasterxml.jackson.databind.JsonNode json(String value) {
        try {
            return mapper.readTree(value);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }
}
