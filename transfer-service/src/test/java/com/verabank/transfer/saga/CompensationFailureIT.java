package com.verabank.transfer.saga;

import static org.assertj.core.api.Assertions.assertThat;
import static org.awaitility.Awaitility.await;

import com.verabank.transfer.support.Bank;
import com.verabank.transfer.support.Events;
import com.verabank.transfer.support.Http;
import com.verabank.transfer.support.IntegrationTest;
import com.verabank.transfer.support.RailDouble;
import com.verabank.transfer.support.SagaEnabled;
import io.micrometer.core.instrument.MeterRegistry;
import java.time.Duration;
import java.util.Map;
import java.util.TreeMap;
import java.util.UUID;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.test.context.TestPropertySource;
import org.springframework.transaction.PlatformTransactionManager;

/**
 * T-06: la base falla al liberar la retención (trigger de prueba con SQLSTATE 40001). La saga
 * reintenta con backoff exponencial, al quinto fallo queda en COMPENSATION_FAILED con los fondos
 * retenidos y la conciliación en cero (docs/saga.md S10, S14 y S15).
 */
@SagaEnabled
@TestPropertySource(properties = {"verabank.saga.rail-reply-timeout=10m"})
class CompensationFailureIT extends IntegrationTest {

    static RailDouble rail;

    @Autowired
    PlatformTransactionManager txManager;

    @Autowired
    MeterRegistry meters;

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
    }

    /** La liberación de la retención (held baja) falla con el SQLSTATE dado. */
    private void failRelease(String sqlState) {
        jdbc.sql("""
                CREATE OR REPLACE FUNCTION core.test_fail_release() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'fallo inyectado al liberar la retención' USING ERRCODE = '%s'; END $$"""
                .formatted(sqlState)).update();
        jdbc.sql("""
                CREATE TRIGGER test_fail_release BEFORE UPDATE OF held ON core.accounts
                FOR EACH ROW WHEN (NEW.held < OLD.held) EXECUTE FUNCTION core.test_fail_release()""").update();
    }

    @AfterEach
    void removeFault() {
        jdbc.sql("DROP TRIGGER IF EXISTS test_fail_release ON core.accounts").update();
        jdbc.sql("DROP FUNCTION IF EXISTS core.test_fail_release()").update();
    }

    @Test
    void t06ConBackoffExponencialAlQuintoFalloQuedaCompensationFailedConLosFondosRetenidos() {
        Http.Response created = http.postTransfer(KEY_WEB, UUID.randomUUID(), Bank.external(source, "100.00"));
        UUID id = UUID.fromString(created.json().path("transfer_id").asText());
        failRelease("40001");
        double failedBefore = meters.counter("saga.compensation_failed").count();

        rail.send(RailDouble.rejected(id, "ACCOUNT_CLOSED"));

        // Se observa cada fallo: el retardo hasta el siguiente intento es wake_at - updated_at.
        Map<Integer, Double> delays = new TreeMap<>();
        await().atMost(Duration.ofSeconds(30)).pollInterval(Duration.ofMillis(20)).until(() -> {
            var row = jdbc.sql("""
                    SELECT attempt, status, EXTRACT(EPOCH FROM (wake_at - updated_at)) AS delay
                    FROM core.saga_state WHERE saga_id = :t""").param("t", id).query().singleRow();
            if (row.get("delay") != null && (Integer) row.get("attempt") > 0) {   // attempt 0 es S9, antes del primer fallo
                delays.putIfAbsent((Integer) row.get("attempt"), ((Number) row.get("delay")).doubleValue());
            }
            return "COMPENSATION_FAILED".equals(row.get("status"));
        });

        // base 300 ms, tope 2 s: 0,3 · 0,6 · 1,2 · min(2,4; 2) = 2,0 tras los fallos 1 a 4.
        assertThat(delays).containsOnlyKeys(1, 2, 3, 4);
        assertThat(delays.get(1)).isCloseTo(0.3, org.assertj.core.data.Offset.offset(0.001));
        assertThat(delays.get(2)).isCloseTo(0.6, org.assertj.core.data.Offset.offset(0.001));
        assertThat(delays.get(3)).isCloseTo(1.2, org.assertj.core.data.Offset.offset(0.001));
        assertThat(delays.get(4)).isCloseTo(2.0, org.assertj.core.data.Offset.offset(0.001));

        var saga = jdbc.sql("""
                SELECT status, current_step, attempt, wake_at, last_error, payload->>'resume_status' AS resume_status
                FROM core.saga_state WHERE saga_id = :t""").param("t", id).query().singleRow();
        assertThat(saga.get("status")).isEqualTo("COMPENSATION_FAILED");
        assertThat(saga.get("current_step")).isEqualTo("COMPENSATING");
        assertThat(saga.get("attempt")).isEqualTo(5);
        assertThat(saga.get("wake_at")).as("no se reintenta sola").isNull();
        assertThat((String) saga.get("last_error")).contains("fallo inyectado al liberar");
        assertThat(saga.get("resume_status")).isEqualTo("COMPENSATING");
        assertThat(meters.counter("saga.compensation_failed").count()).isEqualTo(failedBefore + 1);

        // Los fondos siguen retenidos; transfers.status no cambió; nada perdido ni duplicado.
        assertThat(jdbc.sql("SELECT status FROM core.transfers WHERE transfer_id = :t").param("t", id)
                .query(String.class).single()).isEqualTo("PROCESSING");
        assertThat(bank.held(source)).isEqualByComparingTo("100.00");
        assertThat(bank.balance(source)).isEqualByComparingTo("500.00");
        assertThat(bank.countEntries(id, "D")).isZero();
        assertThat(bank.reconcile()).as("cuentas descuadradas y retenciones huérfanas").containsExactly(0, 0);
        assertThat(Events.countByType(jdbc, id)).doesNotContainKey("TransferFailed");
        assertThat(jdbc.sql("SELECT action FROM audit.audit_log WHERE entity_id = :t ORDER BY audit_id")
                .param("t", id.toString()).query(String.class).list())
                .contains("SAGA_COMPENSATION_STARTED", "SAGA_COMPENSATION_FAILED");

        // Detenida de verdad: pasado el mayor backoff, el sondeo no la toca.
        long version = jdbc.sql("SELECT version FROM core.saga_state WHERE saga_id = :t").param("t", id)
                .query(Long.class).single();
        try {
            Thread.sleep(2500);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        assertThat(jdbc.sql("SELECT version FROM core.saga_state WHERE saga_id = :t").param("t", id)
                .query(Long.class).single()).isEqualTo(version);
    }

    /** Lleva una compensación hasta COMPENSATION_FAILED con el fallo inyectado y devuelve la transferencia. */
    private UUID stopCompensation() {
        Http.Response created = http.postTransfer(KEY_WEB, UUID.randomUUID(), Bank.external(source, "100.00"));
        UUID id = UUID.fromString(created.json().path("transfer_id").asText());
        failRelease("40001");
        rail.send(RailDouble.rejected(id, "ACCOUNT_CLOSED"));
        await().atMost(Duration.ofSeconds(30)).until(() -> "COMPENSATION_FAILED".equals(
                jdbc.sql("SELECT status FROM core.saga_state WHERE saga_id = :t").param("t", id)
                        .query(String.class).single()));
        return id;
    }

    private String transferStatus(UUID id) {
        return jdbc.sql("SELECT status FROM core.transfers WHERE transfer_id = :t").param("t", id)
                .query(String.class).single();
    }

    @Test
    void t06LaReanudacionOperativaCompletaLaCompensacionUnaVezReparadaLaCausa() {
        UUID id = stopCompensation();
        assertThat(bank.held(source)).isEqualByComparingTo("100.00");
        removeFault();   // se repara la causa

        Http.Response resumed = http.post(KEY_MOBILE, "/internal/v1/sagas/" + id + "/resume");

        assertThat(resumed.status()).isEqualTo(200);
        assertThat(resumed.json().path("saga_id").asText()).isEqualTo(id.toString());
        assertThat(resumed.json().path("status").asText()).isEqualTo("COMPENSATING");
        assertThat(resumed.json().path("current_step").asText()).isEqualTo("COMPENSATING");
        assertThat(resumed.json().path("attempt").asInt()).isZero();
        await().atMost(Duration.ofSeconds(20)).until(() -> "FAILED".equals(transferStatus(id)));

        assertThat(jdbc.sql("SELECT status || ':' || current_step || ':' || attempt FROM core.saga_state "
                + "WHERE saga_id = :t").param("t", id).query(String.class).single()).isEqualTo("COMPENSATED:DONE:0");
        assertThat(bank.held(source)).isEqualByComparingTo("0");
        assertThat(bank.balance(source)).isEqualByComparingTo("500.00");
        assertThat(bank.reconcile()).containsExactly(0, 0);
        assertThat(Events.countByType(jdbc, id)).containsEntry("TransferFailed", 1L);
        assertThat(jdbc.sql("SELECT actor || ':' || (detail->>'to_status') FROM audit.audit_log "
                + "WHERE entity_id = :t AND action = 'SAGA_RESUMED'").param("t", id.toString())
                .query(String.class).single()).isEqualTo("mobile:COMPENSATING");
        Events.assertAllValid(jdbc, id);

        // Ya no está detenida: una segunda reanudación es 409.
        Http.Response again = http.post(KEY_MOBILE, "/internal/v1/sagas/" + id + "/resume");
        assertThat(again.status()).isEqualTo(409);
        assertThat(again.json().path("type").asText()).isEqualTo("saga-not-resumable");
        assertThat(com.verabank.transfer.support.Contracts.openApiErrors("Problem", again.json())).isEmpty();
    }

    @Test
    void reanudarSinRepararLaCausaVuelveAQuedarEnCompensationFailedSinPerderLosFondos() {
        UUID id = stopCompensation();
        double failed = meters.counter("saga.compensation_failed").count();

        Http.Response resumed = http.post(KEY_WEB, "/internal/v1/sagas/" + id + "/resume");   // la causa persiste

        assertThat(resumed.status()).isEqualTo(200);
        await().atMost(Duration.ofSeconds(30)).until(() -> {
            var row = jdbc.sql("SELECT status, attempt FROM core.saga_state WHERE saga_id = :t").param("t", id)
                    .query().singleRow();
            return "COMPENSATION_FAILED".equals(row.get("status")) && (Integer) row.get("attempt") == 5;
        });
        assertThat(meters.counter("saga.compensation_failed").count()).isEqualTo(failed + 1);
        assertThat(transferStatus(id)).isEqualTo("PROCESSING");
        assertThat(bank.held(source)).isEqualByComparingTo("100.00");
        assertThat(bank.reconcile()).containsExactly(0, 0);
    }

    @Test
    void laReanudacionExigeApiKeyYDistingueInexistenteDeNoReanudable() {
        UUID id = UUID.fromString(http.postTransfer(KEY_WEB, UUID.randomUUID(), Bank.external(source, "5.00"))
                .json().path("transfer_id").asText());

        assertThat(http.post(null, "/internal/v1/sagas/" + id + "/resume").status()).isEqualTo(401);
        Http.Response missing = http.post(KEY_WEB, "/internal/v1/sagas/" + UUID.randomUUID() + "/resume");
        assertThat(missing.status()).isEqualTo(404);
        assertThat(missing.json().path("type").asText()).isEqualTo("not-found");
        Http.Response notStopped = http.post(KEY_WEB, "/internal/v1/sagas/" + id + "/resume");
        assertThat(notStopped.status()).isEqualTo(409);
        assertThat(notStopped.json().path("type").asText()).isEqualTo("saga-not-resumable");
        assertThat(http.send("GET", "/internal/v1/sagas/" + id + "/resume", java.util.Map.of("X-API-Key", KEY_WEB),
                null).status()).isEqualTo(405);
        assertThat(jdbc.sql("SELECT status FROM core.saga_state WHERE saga_id = :t").param("t", id)
                .query(String.class).single()).isEqualTo("WAITING_REPLY");
    }
}
