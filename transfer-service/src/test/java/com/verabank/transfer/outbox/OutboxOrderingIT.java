package com.verabank.transfer.outbox;

import static org.assertj.core.api.Assertions.assertThat;
import static org.awaitility.Awaitility.await;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.doAnswer;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.verabank.transfer.config.VerabankProperties;
import com.verabank.transfer.support.Infra;
import com.verabank.transfer.support.IntegrationTest;
import com.verabank.transfer.support.RelayEnabled;
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
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.stream.Collectors;
import javax.sql.DataSource;
import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.apache.kafka.clients.producer.ProducerRecord;
import org.apache.kafka.common.KafkaException;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.test.context.bean.override.mockito.MockitoSpyBean;

/**
 * Orden por clave y duplicados del relay cuando un envío intermedio de un lote falla, y
 * destino de los errores no reintentables (<tópico>.dlt, §3.1).
 *
 * <p>El lote se arma con el bloqueo consultivo retenido por otra conexión: así todos los
 * eventos entran en un mismo lote cuando se libera.
 */
@RelayEnabled
class OutboxOrderingIT extends IntegrationTest {

    private static final String ORDER_TOPIC = "relay.ordering.v1";
    private static final String BIG_TOPIC = "relay.big.v1";

    // Spring Boot define el bean como KafkaTemplate<?, ?>: el spy usa el tipo crudo.
    @SuppressWarnings("rawtypes")
    @MockitoSpyBean
    KafkaTemplate kafka;

    @Autowired
    OutboxWriter writer;
    @Autowired
    ObjectMapper mapper;
    @Autowired
    DataSource dataSource;
    @Autowired
    VerabankProperties properties;

    @BeforeAll
    static void topics() {
        Infra.createTopics(Map.of(ORDER_TOPIC, 6, BIG_TOPIC, 6, BIG_TOPIC + ".dlt", 1));
    }

    @Test
    void unEnvioIntermedioFallidoNoAdelantaNiDuplicaEventosDeLaMismaClave() throws Exception {
        List<Written> sameKey = new ArrayList<>();
        List<Written> others = new ArrayList<>();
        AtomicBoolean failed = new AtomicBoolean(false);

        withRelayPaused(() -> {
            for (int i = 0; i < 8; i++) {
                sameKey.add(write(ORDER_TOPIC, "key-a", i, 10));
                if (i % 2 == 0) {
                    others.add(write(ORDER_TOPIC, "key-" + (char) ('b' + i / 2), i, 10));
                }
            }
            String failId = sameKey.get(2).eventId().toString();   // el tercero de la clave
            doAnswer(inv -> {
                @SuppressWarnings("unchecked")
                ProducerRecord<String, String> r = inv.getArgument(0);
                if (failId.equals(KafkaProbe.headerOf(r, "event_id")) && failed.compareAndSet(false, true)) {
                    return CompletableFuture.failedFuture(new KafkaException("fallo inyectado en el envío"));
                }
                return inv.callRealMethod();
            }).when(kafka).send(any(ProducerRecord.class));
        });

        List<Written> all = new ArrayList<>(sameKey);
        all.addAll(others);
        await().atMost(Duration.ofSeconds(30)).until(() -> pending(all) == 0);
        assertThat(failed).as("el fallo inyectado ocurrió").isTrue();

        // Se lee un margen extra para que aparezca cualquier duplicado tardío antes de contar.
        List<ConsumerRecord<String, String>> records = KafkaProbe.readUntil(ORDER_TOPIC,
                rs -> false, Duration.ofSeconds(3));

        Map<String, List<Long>> byKey = new LinkedHashMap<>();
        Map<UUID, Written> index = all.stream().collect(Collectors.toMap(Written::eventId, w -> w));
        for (ConsumerRecord<String, String> r : records) {
            UUID id = UUID.fromString(KafkaProbe.header(r, "event_id"));
            byKey.computeIfAbsent(r.key(), k -> new ArrayList<>()).add(index.get(id).outboxId());
        }
        assertThat(byKey.get("key-a"))
                .as("la clave con el envío fallido: orden de outbox_id y cada evento una sola vez")
                .containsExactlyElementsOf(sameKey.stream().map(Written::outboxId).toList());
        for (Written w : others) {
            assertThat(byKey.get(w.key())).as("otra clave del mismo lote no se ve afectada")
                    .containsExactly(w.outboxId());
        }
    }

    @Test
    void unErrorNoReintentableVaAlDltYSeMarcaPublicadoSinBloquearLaClave() throws Exception {
        List<Written> all = new ArrayList<>();
        Written before;
        Written huge;
        Written after;
        Written other;

        Written[] w = new Written[4];
        // Con el relay en pausa, los cuatro entran en un mismo lote.
        withRelayPaused(() -> {
            w[0] = write(BIG_TOPIC, "key-big", 1, 10);
            w[1] = write(BIG_TOPIC, "key-big", 2, 2_000_000);   // > max.request.size (1 MB)
            w[2] = write(BIG_TOPIC, "key-big", 3, 10);
            w[3] = write(BIG_TOPIC, "key-other", 4, 10);
        });
        before = w[0];
        huge = w[1];
        after = w[2];
        other = w[3];
        all.addAll(List.of(w));

        await().atMost(Duration.ofSeconds(30)).until(() -> pending(all) == 0);

        List<ConsumerRecord<String, String>> main = KafkaProbe.readUntil(BIG_TOPIC,
                rs -> rs.size() >= 3, Duration.ofSeconds(20));
        assertThat(main).extracting(r -> KafkaProbe.header(r, "event_id"))
                .containsExactlyInAnyOrder(before.eventId().toString(), after.eventId().toString(),
                        other.eventId().toString());
        List<String> bigKeyOrder = main.stream().filter(r -> "key-big".equals(r.key()))
                .map(r -> KafkaProbe.header(r, "event_id")).toList();
        assertThat(bigKeyOrder).containsExactly(before.eventId().toString(), after.eventId().toString());

        List<ConsumerRecord<String, String>> dlt = KafkaProbe.readUntil(BIG_TOPIC + ".dlt",
                rs -> !rs.isEmpty(), Duration.ofSeconds(20));
        assertThat(dlt).hasSize(1);
        ConsumerRecord<String, String> dead = dlt.get(0);
        assertThat(dead.key()).isEqualTo("key-big");
        assertThat(KafkaProbe.header(dead, "event_id")).isEqualTo(huge.eventId().toString());
        assertThat(KafkaProbe.header(dead, "original_topic")).isEqualTo(BIG_TOPIC);
        assertThat(KafkaProbe.header(dead, "dlt_reason")).contains("RecordTooLarge");
    }

    // ---------------------------------------------------------------- ayudas

    private record Written(UUID eventId, long outboxId, String key) {
    }

    /** Retiene el bloqueo del relay mientras se prepara el escenario y lo libera al terminar. */
    private void withRelayPaused(ThrowingRunnable setup) throws Exception {
        try (Connection c = dataSource.getConnection(); Statement st = c.createStatement()) {
            st.execute("SELECT pg_advisory_lock(" + properties.outbox().lockKey() + ")");
            try {
                setup.run();
            } finally {
                st.execute("SELECT pg_advisory_unlock(" + properties.outbox().lockKey() + ")");
            }
        }
    }

    private interface ThrowingRunnable {
        void run() throws Exception;
    }

    private Written write(String topic, String key, int seq, int blobChars) {
        var payload = mapper.createObjectNode().put("seq", seq)
                .put("blob", "x".repeat(blobChars));
        UUID eventId = writer.append(topic, "probe", UUID.randomUUID(), key, "RelayProbe", Instant.now(),
                UUID.randomUUID(), payload);
        long outboxId = jdbc.sql("SELECT outbox_id FROM core.outbox WHERE event_id = :e")
                .param("e", eventId).query(Long.class).single();
        return new Written(eventId, outboxId, key);
    }

    private long pending(List<Written> written) {
        return jdbc.sql("SELECT count(*) FROM core.outbox WHERE published_at IS NULL AND outbox_id IN (:ids)")
                .param("ids", written.stream().map(Written::outboxId).toList()).query(Long.class).single();
    }
}
