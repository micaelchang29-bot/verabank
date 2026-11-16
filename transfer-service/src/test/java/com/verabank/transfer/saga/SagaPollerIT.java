package com.verabank.transfer.saga;

import static org.assertj.core.api.Assertions.assertThat;
import static org.awaitility.Awaitility.await;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.mockingDetails;

import com.verabank.transfer.scoring.FraudScorer;
import com.verabank.transfer.scoring.ScoreRequest;
import com.verabank.transfer.support.Bank;
import com.verabank.transfer.support.Events;
import com.verabank.transfer.support.Http;
import com.verabank.transfer.support.IntegrationTest;
import com.verabank.transfer.support.SagaEnabled;
import com.verabank.transfer.support.SecondInstance;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.context.ConfigurableApplicationContext;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoSpyBean;
import org.springframework.transaction.PlatformTransactionManager;

/**
 * Sondeo de la saga (docs/saga.md §7): T-09 (caída entre la retención y el scoring) y la competencia
 * de dos instancias por las mismas sagas.
 */
@SagaEnabled
@TestPropertySource(properties = {"verabank.saga.rail-reply-timeout=10m", "verabank.saga.poller-batch=10"})
class SagaPollerIT extends IntegrationTest {

    /** Simula la caída del proceso: no es una excepción de datos y nadie la registra como fallo local. */
    static final class SimulatedCrash extends Error {
        SimulatedCrash() {
            super("caída simulada tras la retención", null, false, false);
        }
    }

    @MockitoSpyBean
    FraudScorer scorer;

    @Autowired
    PlatformTransactionManager txManager;

    @Autowired
    SagaPoller poller;

    private final Set<UUID> crashed = ConcurrentHashMap.newKeySet();
    private Http http;
    private Bank bank;

    @BeforeEach
    void setUp() {
        http = new Http(port);
        bank = new Bank(jdbc, txManager);
        crashed.clear();
        // El proceso "cae" en la primera llamada al scorer de cada transferencia: tx1 ya está confirmada.
        doAnswer(inv -> {
            ScoreRequest r = inv.getArgument(0);
            if (crashed.add(r.transferId())) {
                throw new SimulatedCrash();
            }
            return inv.callRealMethod();
        }).when(scorer).score(any());
    }

    private long scoreCalls(UUID transferId) {
        return mockingDetails(scorer).getInvocations().stream()
                .filter(i -> i.getMethod().getName().equals("score"))
                .filter(i -> ((ScoreRequest) i.getArguments()[0]).transferId().equals(transferId))
                .count();
    }

    @Test
    void t09ElSondeoReanudaLaTransferenciaTrasLaCaidaSinQueElClienteReintente() {
        UUID source = bank.account("300.00");
        UUID dest = bank.account("0");

        Http.Response crashedRequest = http.postTransfer(KEY_WEB, UUID.randomUUID(),
                Bank.internal(source, dest, "80.00"));

        assertThat(crashedRequest.status()).as("la solicitud original murió").isEqualTo(500);
        UUID id = jdbc.sql("SELECT transfer_id FROM core.transfers WHERE source_account_id = :s")
                .param("s", source).query(UUID.class).single();
        assertThat(jdbc.sql("SELECT status || ':' || current_step FROM core.saga_state WHERE saga_id = :t")
                .param("t", id).query(String.class).single()).isEqualTo("RUNNING:FUNDS_HELD");
        assertThat(bank.held(source)).as("retención hecha, sin asientos").isEqualByComparingTo("80.00");
        assertThat(bank.reconcile()).containsExactly(0, 0);

        // Nadie reintenta con la clave: el sondeo debe reanudarla al vencer el arrendamiento (2 s).
        await().atMost(Duration.ofSeconds(20)).pollInterval(Duration.ofMillis(200)).until(() -> "COMPLETED".equals(
                jdbc.sql("SELECT status FROM core.transfers WHERE transfer_id = :t").param("t", id)
                        .query(String.class).single()));

        assertThat(bank.balance(source)).isEqualByComparingTo("220.00");
        assertThat(bank.balance(dest)).isEqualByComparingTo("80.00");
        assertThat(bank.held(source)).isEqualByComparingTo("0");
        assertThat(bank.countEntries(id, "D")).isEqualTo(1);
        assertThat(bank.countEntries(id, "C")).isEqualTo(1);
        assertThat(scoreCalls(id)).as("el score se pidió dos veces (idempotente), se registró una").isEqualTo(2);
        assertThat(Events.countByType(jdbc, id)).containsEntry("TransferScored", 1L)
                .containsEntry("TransferCompleted", 1L);
        assertThat(jdbc.sql("SELECT status || ':' || current_step || ':' || attempt FROM core.saga_state "
                + "WHERE saga_id = :t").param("t", id).query(String.class).single()).isEqualTo("COMPLETED:DONE:0");
        assertThat(bank.reconcile()).containsExactly(0, 0);
        Events.assertAllValid(jdbc, id);
    }

    @Test
    void t09UnaTransferenciaExternaTambienSeReanudaYEmiteSuInstruccion() {
        UUID source = bank.account("300.00");

        Http.Response crashedRequest = http.postTransfer(KEY_WEB, UUID.randomUUID(), Bank.external(source, "50.00"));
        assertThat(crashedRequest.status()).isEqualTo(500);
        UUID id = jdbc.sql("SELECT transfer_id FROM core.transfers WHERE source_account_id = :s")
                .param("s", source).query(UUID.class).single();

        await().atMost(Duration.ofSeconds(20)).until(() -> "PROCESSING".equals(
                jdbc.sql("SELECT status FROM core.transfers WHERE transfer_id = :t").param("t", id)
                        .query(String.class).single()));

        assertThat(Events.types(jdbc, id)).containsExactly("TransferInitiated", "TransferScored", "CreditInstruction");
        assertThat(bank.held(source)).isEqualByComparingTo("50.00");
        assertThat(bank.reconcile()).containsExactly(0, 0);
    }

    @Test
    void dosInstanciasCompitiendoPorElSondeoNoDuplicanNingunEfecto() {
        UUID source = bank.account("100000.00");
        UUID dest = bank.account("0");
        int resumable = 60;
        int compensable = 60;

        // 60 transferencias que "caen" tras la retención (RUNNING / FUNDS_HELD con el arrendamiento por vencer)...
        List<UUID> running = new ArrayList<>();
        for (int i = 0; i < resumable; i++) {
            http.postTransfer(KEY_WEB, UUID.randomUUID(), Bank.internal(source, dest, "1.00"));
        }
        running.addAll(jdbc.sql("SELECT transfer_id FROM core.transfers WHERE source_account_id = :s")
                .param("s", source).query(UUID.class).list());
        assertThat(running).hasSize(resumable);
        // ... y 60 con el rechazo del rail ya recibido (S9 hecha, falta S10).
        List<UUID> compensating = new ArrayList<>();
        for (int i = 0; i < compensable; i++) {
            Http.Response r = http.postTransfer(KEY_WEB, UUID.randomUUID(), Bank.external(source, "2.00"));
            // la primera llamada al scorer cae; el sondeo la reanuda y la deja PROCESSING
            assertThat(r.status()).isEqualTo(500);
        }
        List<UUID> externals = jdbc.sql("SELECT transfer_id FROM core.transfers WHERE source_account_id = :s "
                + "AND rail = 'EXTERNAL'").param("s", source).query(UUID.class).list();
        assertThat(externals).hasSize(compensable);

        // Arranca la segunda instancia: las dos compiten por las 120 sagas.
        try (ConfigurableApplicationContext second = SecondInstance.start(
                "--verabank.saga.poller-interval=130ms", "--verabank.saga.poller-batch=10",
                "--verabank.saga.rail-reply-timeout=10m", "--verabank.saga.resume-after=2s")) {
            SagaPoller other = second.getBean(SagaPoller.class);

            await().atMost(Duration.ofSeconds(60)).pollInterval(Duration.ofMillis(250)).until(() ->
                    jdbc.sql("SELECT count(*) FROM core.transfers WHERE source_account_id = :s AND status = 'PROCESSING'")
                            .param("s", source).query(Integer.class).single() == compensable
                    && jdbc.sql("SELECT count(*) FROM core.transfers WHERE source_account_id = :s AND status = 'COMPLETED'")
                            .param("s", source).query(Integer.class).single() == resumable);

            // Ahora el rail rechazó las externas: S9 ya hecha para todas; las dos instancias hacen S10.
            jdbc.sql("""
                    UPDATE core.saga_state SET status = 'COMPENSATING', current_step = 'COMPENSATING', wake_at = now(),
                        payload = payload || '{"fail_reason":"RAIL_REJECTED","compensation_started_at":"2026-03-01T10:00:00Z"}'::jsonb,
                        version = version + 1
                    WHERE saga_id IN (SELECT transfer_id FROM core.transfers
                                      WHERE source_account_id = :s AND rail = 'EXTERNAL')""")
                    .param("s", source).update();

            await().atMost(Duration.ofSeconds(60)).pollInterval(Duration.ofMillis(250)).until(() ->
                    jdbc.sql("SELECT count(*) FROM core.transfers WHERE source_account_id = :s AND status = 'FAILED'")
                            .param("s", source).query(Integer.class).single() == compensable);

            // Cada efecto ocurrió exactamente una vez.
            assertThat(bank.held(source)).isEqualByComparingTo("0");
            assertThat(bank.balance(source)).isEqualByComparingTo("99940.00");   // 100000 - 60 transferencias de 1.00
            assertThat(bank.balance(dest)).isEqualByComparingTo("60.00");
            for (UUID id : running) {
                assertThat(bank.countEntries(id, "D")).isEqualTo(1);
                assertThat(bank.countEntries(id, "C")).isEqualTo(1);
                assertThat(Events.countByType(jdbc, id)).containsEntry("TransferScored", 1L)
                        .containsEntry("TransferCompleted", 1L);
            }
            for (UUID id : externals) {
                assertThat(Events.countByType(jdbc, id)).containsEntry("TransferFailed", 1L)
                        .containsEntry("CreditInstruction", 1L);
                assertThat(bank.countEntries(id, "D")).isZero();
            }
            assertThat(bank.reconcile()).containsExactly(0, 0);
            // Las dos instancias trabajaron: se repartieron las sagas sin pisarse.
            assertThat(poller.handled()).as("instancia principal").isPositive();
            assertThat(other.handled()).as("segunda instancia").isPositive();
        }
    }
}
