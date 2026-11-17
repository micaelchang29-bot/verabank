package com.verabank.transfer.saga;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.awaitility.Awaitility.await;
import static org.mockito.ArgumentMatchers.contains;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.verabank.transfer.scoring.FraudScorer;
import com.verabank.transfer.support.Bank;
import com.verabank.transfer.support.Events;
import com.verabank.transfer.support.Http;
import com.verabank.transfer.support.Infra;
import com.verabank.transfer.support.IntegrationTest;
import com.verabank.transfer.support.KafkaProbe;
import com.verabank.transfer.support.RailDouble;
import com.verabank.transfer.support.SagaEnabled;
import com.verabank.transfer.support.Scoring;
import com.verabank.transfer.transfer.TransferRepository;
import io.micrometer.core.instrument.MeterRegistry;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import org.apache.kafka.clients.admin.Admin;
import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.apache.kafka.clients.consumer.OffsetAndMetadata;
import org.apache.kafka.clients.admin.OffsetSpec;
import org.apache.kafka.common.TopicPartition;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoSpyBean;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * Consumidor de rail.replies.v1: T-05 (el rail rechaza el abono), T-08 (mensaje reentregado) y la
 * tabla de respuestas repetidas, tardías y contradictorias de docs/saga.md §5, más los casos que
 * van al DLT (§4). El rail es un doble de prueba; las respuestas se publican a mano.
 */
@SagaEnabled
@TestPropertySource(properties = "verabank.saga.rail-reply-timeout=10m")
class RailRepliesIT extends IntegrationTest {

    static RailDouble rail;

    @MockitoSpyBean
    FraudScorer scorer;

    @Autowired
    PlatformTransactionManager txManager;

    @Autowired
    MeterRegistry meters;

    @MockitoSpyBean
    SagaRepository sagas;

    @MockitoSpyBean
    RailSaga railSaga;

    /** Cuenta cuántas veces se entrega y se parsea cada mensaje: una redelivery se ve como otro parse. */
    @MockitoSpyBean
    ReplySchemas schemas;

    @Autowired
    TransferRepository transfers;

    @Autowired
    ObjectMapper mapper;

    private Http http;
    private Bank bank;
    private UUID source;

    @BeforeAll
    static void startRail() {
        rail = RailDouble.start();
    }

    @AfterAll
    static void stopRail() {
        rail.close();
    }

    @BeforeEach
    void setUp() {
        http = new Http(port);
        bank = new Bank(jdbc, txManager);
        source = bank.account("500.00");
        Scoring.blockAndReview(scorer);
    }

    @AfterEach
    void removeFaults() {
        jdbc.sql("DROP TRIGGER IF EXISTS test_fail_ledger ON core.transactions").update();
        jdbc.sql("DROP FUNCTION IF EXISTS core.test_fail_ledger()").update();
    }

    private UUID externalTransfer(String amount) {
        Http.Response r = http.postTransfer(KEY_WEB, UUID.randomUUID(), Bank.external(source, amount));
        assertThat(r.status()).isEqualTo(201);
        assertThat(r.json().path("status").asText()).isEqualTo("PROCESSING");
        return UUID.fromString(r.json().path("transfer_id").asText());
    }

    private String status(UUID id) {
        return jdbc.sql("SELECT status FROM core.transfers WHERE transfer_id = :t").param("t", id)
                .query(String.class).single();
    }

    private String saga(UUID id) {
        return jdbc.sql("SELECT status || ':' || current_step FROM core.saga_state WHERE saga_id = :t")
                .param("t", id).query(String.class).single();
    }

    private double count(String meter) {
        return meters.counter(meter).count();
    }

    private int inbox(UUID eventId) {
        return jdbc.sql("SELECT count(*) FROM core.processed_messages WHERE consumer = :c AND event_id = :e")
                .param("c", "transfer-service:rail-replies").param("e", eventId).query(Integer.class).single();
    }

    private void awaitProcessed(UUID... eventIds) {
        await().atMost(Duration.ofSeconds(20)).pollInterval(Duration.ofMillis(100)).until(
                () -> List.of(eventIds).stream().allMatch(e -> inbox(e) == 1));
    }

    private List<ConsumerRecord<String, String>> dlt(String key, int expected) {
        return KafkaProbe.readUntil(Infra.RAIL_REPLIES_DLT,
                recs -> recs.stream().filter(r -> key.equals(r.key())).count() >= expected, Duration.ofSeconds(30))
                .stream().filter(r -> key.equals(r.key())).toList();
    }

    // ------------------------------------------------------------------ S8 y T-08

    @Test
    void t08UnCreditConfirmedReentregadoDejaUnSoloParDeAsientosYUnRegistroEnElInbox() {
        UUID id = externalTransfer("100.00");
        UUID eventId = UUID.randomUUID();
        double duplicates = count("inbox.duplicates");

        rail.send(RailDouble.confirmed(id).withEventId(eventId));
        rail.send(RailDouble.confirmed(id).withEventId(eventId));   // misma entrega, reenviada

        await().atMost(Duration.ofSeconds(20)).until(() -> "COMPLETED".equals(status(id)));
        await().atMost(Duration.ofSeconds(20)).until(() -> count("inbox.duplicates") == duplicates + 1);

        assertThat(inbox(eventId)).as("un solo procesamiento en el inbox").isEqualTo(1);
        assertThat(bank.countEntries(id, "D")).isEqualTo(1);
        assertThat(bank.countEntries(id, "C")).isEqualTo(1);
        assertThat(bank.balance(source)).isEqualByComparingTo("400.00");
        assertThat(bank.held(source)).isEqualByComparingTo("0");
        assertThat(bank.balance(Bank.CLEARING)).isNotNull();
        assertThat(saga(id)).isEqualTo("COMPLETED:DONE");
        assertThat(Events.countByType(jdbc, id).get("TransferCompleted")).isEqualTo(1L);
        assertThat(bank.reconcile()).containsExactly(0, 0);
        Events.assertAllValid(jdbc, id);
    }

    @Test
    void elMismoResultadoConOtroEventIdSeRegistraPeroNoAsientaDeNuevo() {
        UUID id = externalTransfer("100.00");
        UUID first = UUID.randomUUID();
        UUID second = UUID.randomUUID();
        double duplicates = count("inbox.duplicates");

        rail.send(RailDouble.confirmed(id).withEventId(first));
        rail.send(RailDouble.confirmed(id).withEventId(second));
        awaitProcessed(first, second);

        assertThat(status(id)).isEqualTo("COMPLETED");
        assertThat(bank.countEntries(id, "D")).isEqualTo(1);
        assertThat(bank.countEntries(id, "C")).isEqualTo(1);
        assertThat(count("inbox.duplicates")).isEqualTo(duplicates + 1);
        assertThat(Events.countByType(jdbc, id).get("TransferCompleted")).isEqualTo(1L);
        assertThat(bank.reconcile()).containsExactly(0, 0);
    }

    @Test
    void creditStatusConfirmedTambienCompleta() {
        UUID id = externalTransfer("40.00");

        rail.send(RailDouble.status(id, "CONFIRMED"));

        await().atMost(Duration.ofSeconds(20)).until(() -> "COMPLETED".equals(status(id)));
        assertThat(bank.balance(source)).isEqualByComparingTo("460.00");
        assertThat(bank.reconcile()).containsExactly(0, 0);
    }

    // ------------------------------------------------------------------ S9, S10 y T-05

    @Test
    void t05UnCreditRejectedLiberaLaRetencionYDejaFailedConTransferFailed() {
        UUID id = externalTransfer("100.00");
        assertThat(bank.held(source)).isEqualByComparingTo("100.00");
        long compensations = meters.timer("saga.compensation.duration").count();

        rail.send(RailDouble.rejected(id, "ACCOUNT_CLOSED"));

        await().atMost(Duration.ofSeconds(20)).until(() -> "FAILED".equals(status(id)));
        assertThat(saga(id)).isEqualTo("COMPENSATED:DONE");
        assertThat(bank.held(source)).isEqualByComparingTo("0");
        assertThat(bank.balance(source)).isEqualByComparingTo("500.00");
        assertThat(bank.countEntries(id, "D")).isZero();
        assertThat(bank.reconcile()).containsExactly(0, 0);
        assertThat(jdbc.sql("SELECT failure_reason FROM core.transfers WHERE transfer_id = :t").param("t", id)
                .query(String.class).single()).isEqualTo("RAIL_REJECTED");
        JsonNode payload = jdbc.sql("SELECT payload::text FROM core.saga_state WHERE saga_id = :t")
                .param("t", id).query(String.class).optional().map(s -> {
                    try {
                        return mapper.readTree(s);
                    } catch (Exception e) {
                        throw new IllegalStateException(e);
                    }
                }).orElseThrow();
        assertThat(payload.path("fail_reason").asText()).isEqualTo("RAIL_REJECTED");
        assertThat(payload.path("compensation_started_at").asText()).isNotBlank();

        List<Events.Row> events = Events.outbox(jdbc, id);
        assertThat(events).extracting(Events.Row::eventType)
                .containsExactly("TransferInitiated", "TransferScored", "CreditInstruction", "TransferFailed");
        Events.Row failed = events.get(3);
        assertThat(failed.topic()).isEqualTo("transfer.events.v1");
        assertThat(failed.payload().path("reason").asText()).isEqualTo("RAIL_REJECTED");
        assertThat(failed.payload().path("compensated").asBoolean()).isTrue();
        assertThat(meters.timer("saga.compensation.duration").count()).isEqualTo(compensations + 1);
        assertThat(http.get(KEY_WEB, "/api/v1/transfers/" + id).json().path("status").asText()).isEqualTo("FAILED");
        Events.assertAllValid(jdbc, id);
    }

    @Test
    void creditStatusRejectedYCancelledCompensanConSuPropiaCausa() {
        UUID rejected = externalTransfer("10.00");
        UUID cancelled = externalTransfer("20.00");

        rail.send(RailDouble.status(rejected, "REJECTED"));
        rail.send(RailDouble.status(cancelled, "CANCELLED"));

        await().atMost(Duration.ofSeconds(20)).until(
                () -> "FAILED".equals(status(rejected)) && "FAILED".equals(status(cancelled)));
        assertThat(jdbc.sql("SELECT failure_reason FROM core.transfers WHERE transfer_id = :t").param("t", rejected)
                .query(String.class).single()).isEqualTo("RAIL_REJECTED");
        assertThat(jdbc.sql("SELECT failure_reason FROM core.transfers WHERE transfer_id = :t").param("t", cancelled)
                .query(String.class).single()).isEqualTo("RAIL_CANCELLED");
        assertThat(Events.outbox(jdbc, cancelled).get(3).payload().path("reason").asText()).isEqualTo("RAIL_CANCELLED");
        assertThat(bank.held(source)).isEqualByComparingTo("0");
        assertThat(bank.reconcile()).containsExactly(0, 0);
    }

    // ------------------------------------------------------------------ §5: tardías y contradictorias

    @Test
    void unCreditConfirmedTrasCompensatedNoCambiaNadaYSeContaComoContradiccion() {
        UUID id = externalTransfer("100.00");
        rail.send(RailDouble.rejected(id, "FUNDS"));
        await().atMost(Duration.ofSeconds(20)).until(() -> "FAILED".equals(status(id)));
        double conflicts = count("saga.conflicting_reply");
        long version = sagas.find(id).orElseThrow().version();

        UUID lateEvent = UUID.randomUUID();
        rail.send(RailDouble.confirmed(id).withEventId(lateEvent));
        awaitProcessed(lateEvent);

        assertThat(count("saga.conflicting_reply")).isEqualTo(conflicts + 1);
        assertThat(status(id)).isEqualTo("FAILED");
        assertThat(saga(id)).isEqualTo("COMPENSATED:DONE");
        assertThat(sagas.find(id).orElseThrow().version()).as("sin cambios en la saga").isEqualTo(version);
        assertThat(bank.countEntries(id, "D")).isZero();
        assertThat(bank.countEntries(id, "C")).isZero();
        assertThat(bank.held(source)).isEqualByComparingTo("0");
        assertThat(Events.countByType(jdbc, id)).doesNotContainKey("TransferCompleted");
        assertThat(bank.reconcile()).containsExactly(0, 0);
    }

    @Test
    void unRechazoRepetidoTrasCompensatedNoCambiaNadaNiCuentaComoContradiccion() {
        UUID id = externalTransfer("100.00");
        rail.send(RailDouble.rejected(id, "FUNDS"));
        await().atMost(Duration.ofSeconds(20)).until(() -> "FAILED".equals(status(id)));
        double conflicts = count("saga.conflicting_reply");

        UUID again = UUID.randomUUID();
        UUID cancelAgain = UUID.randomUUID();
        rail.send(RailDouble.rejected(id, "FUNDS").withEventId(again));
        rail.send(RailDouble.status(id, "CANCELLED").withEventId(cancelAgain));
        awaitProcessed(again, cancelAgain);

        assertThat(count("saga.conflicting_reply")).isEqualTo(conflicts);
        assertThat(Events.countByType(jdbc, id).get("TransferFailed")).isEqualTo(1L);
        assertThat(bank.held(source)).isEqualByComparingTo("0");
        assertThat(bank.reconcile()).containsExactly(0, 0);
    }

    @Test
    void unRechazoTrasCompletedEsLaMismaContradiccion() {
        UUID id = externalTransfer("100.00");
        rail.send(RailDouble.confirmed(id));
        await().atMost(Duration.ofSeconds(20)).until(() -> "COMPLETED".equals(status(id)));
        double conflicts = count("saga.conflicting_reply");

        UUID lateReject = UUID.randomUUID();
        rail.send(RailDouble.status(id, "REJECTED").withEventId(lateReject));
        awaitProcessed(lateReject);

        assertThat(count("saga.conflicting_reply")).isEqualTo(conflicts + 1);
        assertThat(status(id)).isEqualTo("COMPLETED");
        assertThat(saga(id)).isEqualTo("COMPLETED:DONE");
        assertThat(bank.countEntries(id, "D")).isEqualTo(1);
        assertThat(bank.held(source)).isEqualByComparingTo("0");
        assertThat(bank.reconcile()).containsExactly(0, 0);
    }

    // ------------------------------------------------------------------ §5: COMPENSATION_FAILED según resume_status

    /** Deja la saga detenida como lo haría S15, con el estado previo dado en {@code payload.resume_status}. */
    private void stopSaga(UUID id, String resumeStatus) {
        String patch = "COMPENSATING".equals(resumeStatus)
                ? "{\"resume_status\":\"COMPENSATING\",\"compensation_started_at\":\"2026-11-16T10:00:00Z\","
                        + "\"fail_reason\":\"RAIL_REJECTED\"}"
                : "{\"resume_status\":\"" + resumeStatus + "\"}";
        jdbc.sql("""
                UPDATE core.saga_state
                SET status = 'COMPENSATION_FAILED', current_step = :step, wake_at = NULL, attempt = 5,
                    payload = payload || CAST(:patch AS jsonb), version = version + 1
                WHERE saga_id = :t""")
                .param("step", "COMPENSATING".equals(resumeStatus) ? "COMPENSATING" : "CREDIT_REQUESTED")
                .param("patch", patch).param("t", id).update();
    }

    private ListAppender<ILoggingEvent> capture(Class<?> logger) {
        ListAppender<ILoggingEvent> appender = new ListAppender<>();
        appender.start();
        ((ch.qos.logback.classic.Logger) LoggerFactory.getLogger(logger)).addAppender(appender);
        return appender;
    }

    private static void release(Class<?> logger, ListAppender<ILoggingEvent> appender) {
        ((ch.qos.logback.classic.Logger) LoggerFactory.getLogger(logger)).detachAppender(appender);
    }

    @Test
    void unConfirmedConLaSagaDetenidaCompensandoEsContradiccionConErrorYMetrica() {
        UUID id = externalTransfer("100.00");
        stopSaga(id, "COMPENSATING");
        double conflicts = count("saga.conflicting_reply");
        long version = sagas.find(id).orElseThrow().version();
        ListAppender<ILoggingEvent> log = capture(RailReplyConsumer.class);
        try {
            UUID lateEvent = UUID.randomUUID();
            rail.send(RailDouble.confirmed(id).withEventId(lateEvent));
            awaitProcessed(lateEvent);

            assertThat(count("saga.conflicting_reply")).isEqualTo(conflicts + 1);
            assertThat(log.list).anySatisfy(e -> {
                assertThat(e.getLevel()).isEqualTo(Level.ERROR);
                assertThat(e.getFormattedMessage()).contains("contradictoria").contains(id.toString());
            });
        } finally {
            release(RailReplyConsumer.class, log);
        }
        assertThat(sagas.find(id).orElseThrow().version()).as("sin cambios en la saga").isEqualTo(version);
        assertThat(saga(id)).isEqualTo("COMPENSATION_FAILED:COMPENSATING");
        assertThat(status(id)).isEqualTo("PROCESSING");
        assertThat(bank.countEntries(id, "D")).isZero();
        assertThat(bank.held(source)).isEqualByComparingTo("100.00");
    }

    @Test
    void unConfirmedConLaSagaDetenidaAntesDeCompensarSeIgnoraConWarnYElSondeoLaResuelveTrasReanudar() {
        UUID id = externalTransfer("100.00");
        stopSaga(id, "WAITING_REPLY");
        double conflicts = count("saga.conflicting_reply");
        long version = sagas.find(id).orElseThrow().version();
        ListAppender<ILoggingEvent> log = capture(RailSaga.class);
        try {
            UUID lateEvent = UUID.randomUUID();
            rail.send(RailDouble.confirmed(id).withEventId(lateEvent));
            awaitProcessed(lateEvent);

            assertThat(count("saga.conflicting_reply")).as("no es una contradicción").isEqualTo(conflicts);
            assertThat(log.list).anySatisfy(e -> {
                assertThat(e.getLevel()).isEqualTo(Level.WARN);
                assertThat(e.getFormattedMessage()).contains(id.toString()).contains("antes de compensar");
            });
            assertThat(log.list).noneMatch(e -> e.getLevel() == Level.ERROR);
        } finally {
            release(RailSaga.class, log);
        }
        assertThat(sagas.find(id).orElseThrow().version()).as("sin cambios en la saga").isEqualTo(version);
        assertThat(saga(id)).isEqualTo("COMPENSATION_FAILED:CREDIT_REQUESTED");
        assertThat(status(id)).isEqualTo("PROCESSING");
        assertThat(bank.held(source)).isEqualByComparingTo("100.00");

        // Reanudada, el sondeo vuelve a preguntar al rail y el CreditStatus CONFIRMED la completa.
        assertThat(http.post(KEY_WEB, "/internal/v1/sagas/" + id + "/resume").status()).isEqualTo(200);
        await().atMost(Duration.ofSeconds(20)).until(() -> rail.count(id, "CreditStatusQuery") >= 1);
        rail.send(RailDouble.status(id, "CONFIRMED"));
        await().atMost(Duration.ofSeconds(20)).until(() -> "COMPLETED".equals(status(id)));
        assertThat(bank.countEntries(id, "D")).isEqualTo(1);
        assertThat(bank.reconcile()).containsExactly(0, 0);
    }

    @Test
    void creditStatusUnknownSeRegistraEnElInboxSinCambiarElEstado() {
        UUID id = externalTransfer("100.00");
        long version = sagas.find(id).orElseThrow().version();
        UUID unknown = UUID.randomUUID();

        rail.send(RailDouble.status(id, "UNKNOWN").withEventId(unknown));
        awaitProcessed(unknown);

        assertThat(saga(id)).isEqualTo("WAITING_REPLY:CREDIT_REQUESTED");
        assertThat(status(id)).isEqualTo("PROCESSING");
        assertThat(sagas.find(id).orElseThrow().version()).isEqualTo(version);
        assertThat(bank.held(source)).isEqualByComparingTo("100.00");
    }

    // ------------------------------------------------------------------ §4: al DLT

    @Test
    void unMensajeQueNoValidaContraSuEsquemaVaDirectoAlDltEnLaParticionCero() {
        String key = UUID.randomUUID().toString();
        double dlt = count("inbox.dlt");
        // CreditConfirmed sin rail_ref: el payload no cumple su esquema.
        UUID id = UUID.randomUUID();
        rail.sendRaw(key, RailDouble.envelope(new RailDouble.Reply("CreditConfirmed", id,
                Map.of("transfer_id", id.toString()), null)).toString());

        List<ConsumerRecord<String, String>> records = dlt(key, 1);

        assertThat(records).hasSize(1);
        assertThat(records.get(0).partition()).isZero();
        assertThat(count("inbox.dlt")).isEqualTo(dlt + 1);
    }

    @Test
    void unMensajeQueNoEsJsonNiTieneEventTypeConocidoTambienVaAlDlt() {
        String notJson = UUID.randomUUID().toString();
        String unknownType = UUID.randomUUID().toString();
        UUID id = UUID.randomUUID();
        rail.sendRaw(notJson, "esto no es json");
        rail.sendRaw(unknownType, RailDouble.envelope(new RailDouble.Reply("CreditExploded", id,
                Map.of("transfer_id", id.toString()), null)).toString());

        assertThat(dlt(notJson, 1)).hasSize(1);
        assertThat(dlt(unknownType, 1)).hasSize(1);
    }

    @Test
    void unaTransferenciaInexistenteVaDirectoAlDltYElConsumidorSigueAvanzando() {
        UUID ghost = UUID.randomUUID();
        UUID eventId = UUID.randomUUID();
        rail.send(RailDouble.confirmed(ghost).withEventId(eventId));
        UUID real = externalTransfer("30.00");
        rail.send(RailDouble.confirmed(real));

        List<ConsumerRecord<String, String>> records = dlt(ghost.toString(), 1);
        await().atMost(Duration.ofSeconds(20)).until(() -> "COMPLETED".equals(status(real)));

        assertThat(records).hasSize(1);
        assertThat(records.get(0).partition()).isZero();
        assertThat(inbox(eventId)).as("el efecto fallido no deja rastro en el inbox").isZero();
    }

    @Test
    void unaRespuestaParaUnaTransferenciaInternaOSinInstruccionEmitidaVaAlDlt() {
        UUID dest = bank.account("0");
        Http.Response internal = http.postTransfer(KEY_WEB, UUID.randomUUID(), Bank.internal(source, dest, "5.00"));
        UUID internalId = UUID.fromString(internal.json().path("transfer_id").asText());
        Http.Response review = http.postTransfer(KEY_WEB, UUID.randomUUID(), Bank.external(source, "77.77"));
        assertThat(review.json().path("status").asText()).isEqualTo("IN_REVIEW");
        UUID reviewId = UUID.fromString(review.json().path("transfer_id").asText());

        rail.send(RailDouble.confirmed(internalId));
        rail.send(RailDouble.confirmed(reviewId));

        assertThat(dlt(internalId.toString(), 1)).hasSize(1);
        assertThat(dlt(reviewId.toString(), 1)).hasSize(1);
        assertThat(status(reviewId)).isEqualTo("IN_REVIEW");
        assertThat(bank.held(source)).isEqualByComparingTo("77.77");
    }

    @Test
    void unFalloDeDatosSeReintientaTresVecesYLuegoVaAlDltSinEnsuciarElInbox() {
        UUID id = externalTransfer("100.00");
        // nextval no se revierte con la transacción: cuenta los intentos reales.
        jdbc.sql("CREATE SEQUENCE IF NOT EXISTS core.test_attempts").update();
        jdbc.sql("SELECT setval('core.test_attempts', 1, false)").query().listOfRows();
        jdbc.sql("""
                CREATE OR REPLACE FUNCTION core.test_fail_ledger() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                  PERFORM nextval('core.test_attempts');
                  RAISE EXCEPTION 'fallo inyectado en el asiento' USING ERRCODE = '23514';
                END $$""").update();
        jdbc.sql("""
                CREATE TRIGGER test_fail_ledger BEFORE INSERT ON core.transactions
                FOR EACH ROW EXECUTE FUNCTION core.test_fail_ledger()""").update();
        UUID eventId = UUID.randomUUID();

        rail.send(RailDouble.confirmed(id).withEventId(eventId));

        List<ConsumerRecord<String, String>> records = dlt(id.toString(), 1);
        assertThat(records).hasSize(1);
        assertThat(records.get(0).partition()).isZero();
        long attempts = jdbc.sql("SELECT last_value - 1 + CASE WHEN is_called THEN 1 ELSE 0 END FROM core.test_attempts")
                .query(Long.class).single();
        assertThat(attempts).as("el intento original más tres reintentos").isEqualTo(4);
        assertThat(inbox(eventId)).isZero();
        assertThat(saga(id)).isEqualTo("WAITING_REPLY:CREDIT_REQUESTED");
        assertThat(bank.held(source)).isEqualByComparingTo("100.00");
        assertThat(bank.reconcile()).containsExactly(0, 0);

        // Reparada la causa, el rail reenvía el mismo evento y se procesa: el inbox no lo marcó.
        removeFaults();
        rail.send(RailDouble.confirmed(id).withEventId(eventId));
        await().atMost(Duration.ofSeconds(20)).until(() -> "COMPLETED".equals(status(id)));
        assertThat(inbox(eventId)).isEqualTo(1);
        assertThat(bank.reconcile()).containsExactly(0, 0);
    }

    // ------------------------------------------------------------------ §4: conflicto de versión, ack, un solo intento

    /** Grupo del consumidor: suma de offsets confirmados y suma de los finales de rail.replies.v1. */
    private long[] committedAndEnd() throws Exception {
        try (Admin admin = Admin.create(Map.of("bootstrap.servers", Infra.KAFKA.getBootstrapServers()))) {
            Map<TopicPartition, OffsetAndMetadata> committed = admin.listConsumerGroupOffsets("transfer-service")
                    .partitionsToOffsetAndMetadata().get(10, TimeUnit.SECONDS);
            long c = committed.entrySet().stream().filter(e -> e.getKey().topic().equals(Infra.RAIL_REPLIES))
                    .mapToLong(e -> e.getValue().offset()).sum();
            Map<TopicPartition, OffsetSpec> latest = new java.util.HashMap<>();
            for (int p = 0; p < 3; p++) {
                latest.put(new TopicPartition(Infra.RAIL_REPLIES, p), OffsetSpec.latest());
            }
            long end = admin.listOffsets(latest).all().get(10, TimeUnit.SECONDS).values().stream()
                    .mapToLong(r -> r.offset()).sum();
            return new long[] {c, end};
        }
    }

    @Test
    void unConflictoDeVersionSeReintentaEnElConsumidorYTerminaProcesadoSinPasarPorElDlt() {
        UUID id = externalTransfer("100.00");
        UUID eventId = UUID.randomUUID();
        double dlt = count("inbox.dlt");
        AtomicInteger finds = new AtomicInteger();
        // Otro actor transita la saga entre la lectura del consumidor y su escritura: solo la primera vez.
        doAnswer(inv -> {
            Object row = inv.callRealMethod();
            if (finds.getAndIncrement() == 0) {
                jdbc.sql("UPDATE core.saga_state SET version = version + 1 WHERE saga_id = :t").param("t", id)
                        .update();
            }
            return row;
        }).when(sagas).find(id);

        rail.send(RailDouble.confirmed(id).withEventId(eventId));

        await().atMost(Duration.ofSeconds(20)).until(() -> "COMPLETED".equals(status(id)));
        assertThat(finds.get()).as("lectura original más la del reintento").isEqualTo(2);
        assertThat(inbox(eventId)).as("una sola fila en el inbox").isEqualTo(1);
        assertThat(count("inbox.dlt")).as("sin DLT").isEqualTo(dlt);
        assertThat(bank.countEntries(id, "D")).isEqualTo(1);
        assertThat(bank.countEntries(id, "C")).isEqualTo(1);
        // El reintento ocurre dentro del consumidor: el mensaje no se vuelve a entregar ni a parsear.
        verify(schemas, times(1)).parse(contains(eventId.toString()));
        assertThat(bank.reconcile()).containsExactly(0, 0);
    }

    @Test
    void elOffsetDelGrupoNoAvanzaMientrasLaTransaccionNoHaTerminadoYAvanzaDespues() throws Exception {
        UUID id = externalTransfer("100.00");
        UUID eventId = UUID.randomUUID();
        await().atMost(Duration.ofSeconds(20)).until(() -> {
            long[] o = committedAndEnd();
            return o[0] == o[1];   // el grupo está al día antes de empezar
        });
        long committedBefore = committedAndEnd()[0];
        CountDownLatch inside = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        doAnswer(inv -> {
            if (((RailReply) inv.getArgument(0)).eventId().equals(eventId)) {
                inside.countDown();
                if (!release.await(30, TimeUnit.SECONDS)) {
                    throw new IllegalStateException("la prueba no liberó el consumidor");
                }
            }
            return inv.callRealMethod();
        }).when(railSaga).onReply(org.mockito.ArgumentMatchers.any(), org.mockito.ArgumentMatchers.any(),
                org.mockito.ArgumentMatchers.any());

        rail.send(RailDouble.confirmed(id).withEventId(eventId));

        try {
            assertThat(inside.await(20, TimeUnit.SECONDS)).as("el consumidor está dentro de la transacción").isTrue();
            // Con el mensaje en proceso (transacción abierta, sin confirmar) el offset sigue donde estaba.
            Thread.sleep(1500);
            assertThat(committedAndEnd()[0]).as("offset confirmado con la transacción abierta")
                    .isEqualTo(committedBefore);
            assertThat(status(id)).isEqualTo("PROCESSING");
        } finally {
            release.countDown();
        }
        await().atMost(Duration.ofSeconds(20)).until(() -> "COMPLETED".equals(status(id)));
        await().atMost(Duration.ofSeconds(20)).until(() -> {
            try {
                return committedAndEnd()[0] == committedBefore + 1;
            } catch (Exception e) {
                return false;
            }
        });
    }

    @Test
    void losMensajesInvalidosVanAlDltConUnSoloIntentoSinReintentos() {
        // (1) transferencia inexistente, (2) respuesta para una interna, (3) payload que no valida.
        UUID ghost = UUID.randomUUID();
        UUID ghostEvent = UUID.randomUUID();
        UUID dest = bank.account("0");
        Http.Response internal = http.postTransfer(KEY_WEB, UUID.randomUUID(), Bank.internal(source, dest, "5.00"));
        UUID internalId = UUID.fromString(internal.json().path("transfer_id").asText());
        UUID internalEvent = UUID.randomUUID();
        UUID badId = UUID.randomUUID();
        UUID badEvent = UUID.randomUUID();

        rail.send(RailDouble.confirmed(ghost).withEventId(ghostEvent));
        rail.send(RailDouble.confirmed(internalId).withEventId(internalEvent));
        rail.sendRaw(badId.toString(), RailDouble.envelope(new RailDouble.Reply("CreditConfirmed", badId,
                Map.of("transfer_id", badId.toString()), badEvent)).toString());

        assertThat(dlt(ghost.toString(), 1)).hasSize(1);
        assertThat(dlt(internalId.toString(), 1)).hasSize(1);
        assertThat(dlt(badId.toString(), 1)).hasSize(1);
        // Con reintentos (incluso solo los de DefaultErrorHandler) cada mensaje se habría entregado 4 veces.
        verify(schemas, times(1)).parse(contains(ghostEvent.toString()));
        verify(schemas, times(1)).parse(contains(internalEvent.toString()));
        verify(schemas, times(1)).parse(contains(badEvent.toString()));
    }

    @Test
    void unaFechaMalFormadaEnOccurredAtSeRechazaPorElEsquemaYVaAlDlt() {
        UUID id = externalTransfer("100.00");
        UUID eventId = UUID.randomUUID();
        var envelope = RailDouble.envelope(RailDouble.confirmed(id).withEventId(eventId));
        envelope.put("occurred_at", "ayer");

        rail.sendRaw(id.toString(), envelope.toString());

        List<ConsumerRecord<String, String>> records = dlt(id.toString(), 1);
        assertThat(records).hasSize(1);
        assertThat(records.get(0).partition()).isZero();
        assertThat(inbox(eventId)).isZero();
        assertThat(status(id)).as("el abono no se confirmó").isEqualTo("PROCESSING");
        assertThat(bank.held(source)).isEqualByComparingTo("100.00");
        verify(schemas, times(1)).parse(contains(eventId.toString()));
    }

    // ------------------------------------------------------------------ versión

    @Test
    void s8SeCondicionaALaVersionDeLaSagaYUnPerdedorNoDejaEfectos() {
        UUID id = externalTransfer("100.00");
        SagaRow stale = sagas.find(id).orElseThrow();
        // Otro actor transita la saga entre la lectura y la escritura (p. ej. el sondeo o un rechazo).
        jdbc.sql("UPDATE core.saga_state SET version = version + 1 WHERE saga_id = :t").param("t", id).update();
        RailReply reply = new RailReply(UUID.randomUUID(), "CreditConfirmed", id, UUID.randomUUID(),
                ReplyKind.CONFIRM, null);

        TransactionTemplate tx = new TransactionTemplate(txManager);
        assertThatThrownBy(() -> tx.executeWithoutResult(s -> railSaga.complete(stale,
                transfers.find(id).orElseThrow(), reply)))
                .isInstanceOf(SagaConflictException.class);

        assertThat(status(id)).isEqualTo("PROCESSING");
        assertThat(bank.countEntries(id, "D")).isZero();
        assertThat(bank.countEntries(id, "C")).isZero();
        assertThat(bank.held(source)).isEqualByComparingTo("100.00");
        assertThat(saga(id)).isEqualTo("WAITING_REPLY:CREDIT_REQUESTED");
    }
}
