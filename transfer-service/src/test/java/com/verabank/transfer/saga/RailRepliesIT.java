package com.verabank.transfer.saga;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.awaitility.Awaitility.await;

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
import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
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

    @Autowired
    SagaRepository sagas;

    @Autowired
    RailSaga railSaga;

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
