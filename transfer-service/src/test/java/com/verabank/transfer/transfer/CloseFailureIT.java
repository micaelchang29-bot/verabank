package com.verabank.transfer.transfer;

import static org.assertj.core.api.Assertions.assertThat;
import static org.awaitility.Awaitility.await;

import com.verabank.transfer.support.Bank;
import com.verabank.transfer.support.Http;
import com.verabank.transfer.support.IntegrationTest;
import java.time.Duration;
import java.util.UUID;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.transaction.PlatformTransactionManager;

/**
 * Fallo forzado entre la retención (tx1) y el cierre (tx2): la base rechaza el abono al
 * destino cuando el débito del origen ya se aplicó dentro de tx2. tx2 debe revertirse entera
 * y la retención debe quedar ligada a una transferencia abierta, nunca huérfana.
 */
class CloseFailureIT extends IntegrationTest {

    @Autowired
    PlatformTransactionManager txManager;

    private Http http;
    private Bank bank;
    private UUID source;
    private UUID dest;

    @BeforeEach
    void setUp() {
        http = new Http(port);
        bank = new Bank(jdbc, txManager);
        source = bank.account("300.00");
        dest = bank.account("0");
        jdbc.sql("""
                CREATE OR REPLACE FUNCTION core.test_fail_credit() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                  IF NEW.direction = 'C' AND NEW.account_id = '%s'::uuid THEN
                    RAISE EXCEPTION 'fallo inyectado en el abono';
                  END IF;
                  RETURN NEW;
                END $$""".formatted(dest)).update();
        jdbc.sql("""
                CREATE TRIGGER test_fail_credit BEFORE INSERT ON core.transactions
                FOR EACH ROW EXECUTE FUNCTION core.test_fail_credit()""").update();
    }

    @AfterEach
    void removeFault() {
        jdbc.sql("DROP TRIGGER IF EXISTS test_fail_credit ON core.transactions").update();
        jdbc.sql("DROP FUNCTION IF EXISTS core.test_fail_credit()").update();
    }

    @Test
    void unFalloEnElCierreNoDejaRetencionesHuerfanasYElReintentoLoReanuda() {
        UUID key = UUID.randomUUID();
        String body = Bank.internal(source, dest, "80.00");

        Http.Response failed = http.postTransfer(KEY_WEB, key, body);

        assertThat(failed.status()).isEqualTo(503);
        assertThat(failed.json().path("type").asText()).isEqualTo("service-unavailable");

        // Enseguida, antes de vencer el plazo de reanudación, el reintento informa que sigue en curso.
        Http.Response early = http.postTransfer(KEY_WEB, key, body);
        assertThat(early.status()).isEqualTo(409);
        assertThat(early.json().path("type").asText()).isEqualTo("idempotency-in-progress");
        UUID transferId = jdbc.sql("SELECT transfer_id FROM core.transfers WHERE client_id = :c AND idempotency_key = :k")
                .param("c", CLIENT_WEB).param("k", key).query(UUID.class).single();

        // tx2 se revirtió completa: sin asientos, saldo intacto, retención ligada a la PENDING.
        assertThat(status(transferId)).isEqualTo("PENDING");
        assertThat(bank.countEntries(transferId, "D")).isZero();
        assertThat(bank.countEntries(transferId, "C")).isZero();
        assertThat(bank.balance(source)).isEqualByComparingTo("300.00");
        assertThat(bank.balance(dest)).isEqualByComparingTo("0");
        assertThat(bank.held(source)).isEqualByComparingTo("80.00");
        assertThat(bank.reconcile()).as("cuentas descuadradas y retenciones huérfanas").containsExactly(0, 0);
        assertThat(jdbc.sql("SELECT last_error FROM core.saga_state WHERE saga_id = :t")
                .param("t", transferId).query(String.class).single()).contains("fallo inyectado");
        assertThat(jdbc.sql("SELECT count(*) FROM core.outbox WHERE aggregate_id = :t AND event_type = 'TransferCompleted'")
                .param("t", transferId).query(Integer.class).single()).isZero();

        // Se repara la causa; pasado el plazo, el reintento con la misma clave cierra la transferencia.
        removeFault();
        await().atMost(Duration.ofSeconds(10)).pollInterval(Duration.ofMillis(250)).until(() -> {
            Http.Response retry = http.postTransfer(KEY_WEB, key, body);
            return retry.status() == 200 && "COMPLETED".equals(retry.json().path("status").asText())
                    && "true".equals(retry.header("Idempotent-Replayed"));
        });

        assertThat(status(transferId)).isEqualTo("COMPLETED");
        assertThat(bank.countEntries(transferId, "D")).isEqualTo(1);
        assertThat(bank.countEntries(transferId, "C")).isEqualTo(1);
        assertThat(bank.balance(source)).isEqualByComparingTo("220.00");
        assertThat(bank.balance(dest)).isEqualByComparingTo("80.00");
        assertThat(bank.held(source)).isEqualByComparingTo("0");
        assertThat(bank.reconcile()).containsExactly(0, 0);
    }

    @Test
    void unFalloAlRetenerNoDejaNada() {
        // La base rechaza la retención dentro de tx1, después de insertar la transferencia.
        jdbc.sql("""
                CREATE OR REPLACE FUNCTION core.test_fail_credit() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'fallo inyectado en la retención'; END $$""").update();
        jdbc.sql("""
                CREATE TRIGGER test_fail_hold BEFORE UPDATE OF held ON core.accounts
                FOR EACH ROW WHEN (NEW.held > OLD.held) EXECUTE FUNCTION core.test_fail_credit()""").update();
        try {
            UUID key = UUID.randomUUID();
            Http.Response failed = http.postTransfer(KEY_WEB, key, Bank.internal(source, dest, "80.00"));

            assertThat(failed.status()).isGreaterThanOrEqualTo(500);
            assertThat(bank.countTransfers(CLIENT_WEB, key)).as("tx1 se revirtió entera").isZero();
            assertThat(bank.held(source)).isEqualByComparingTo("0");
            assertThat(bank.reconcile()).containsExactly(0, 0);
        } finally {
            jdbc.sql("DROP TRIGGER IF EXISTS test_fail_hold ON core.accounts").update();
        }
    }

    private String status(UUID transferId) {
        return jdbc.sql("SELECT status FROM core.transfers WHERE transfer_id = :t")
                .param("t", transferId).query(String.class).single();
    }
}
