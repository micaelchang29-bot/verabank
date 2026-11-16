package com.verabank.transfer.saga;

import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import com.verabank.transfer.scoring.FraudScorer;
import com.verabank.transfer.support.Bank;
import com.verabank.transfer.support.Contracts;
import com.verabank.transfer.support.Events;
import com.verabank.transfer.support.Http;
import com.verabank.transfer.support.IntegrationTest;
import com.verabank.transfer.support.Scoring;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.test.context.bean.override.mockito.MockitoSpyBean;
import org.springframework.transaction.PlatformTransactionManager;

/** Revisión del analista (docs/saga.md §6): S6, S7, los 404 y 409, la auditoría y la competencia de dos analistas. */
class AnalystReviewIT extends IntegrationTest {

    @MockitoSpyBean
    FraudScorer scorer;

    @Autowired
    PlatformTransactionManager txManager;

    private Http http;
    private Bank bank;
    private UUID source;

    @BeforeEach
    void setUp() {
        http = new Http(port);
        bank = new Bank(jdbc, txManager);
        source = bank.account("1000.00");
        Scoring.blockAndReview(scorer);
    }

    private UUID held(String body) {
        Http.Response r = http.postTransfer(KEY_WEB, UUID.randomUUID(), body);
        assertThat(r.status()).isEqualTo(201);
        assertThat(r.json().path("status").asText()).isEqualTo("IN_REVIEW");
        return UUID.fromString(r.json().path("transfer_id").asText());
    }

    private static String decision(String decision, String comment) {
        return "{\"decision\":\"%s\",\"comment\":\"%s\"}".formatted(decision, comment);
    }

    private String saga(UUID id) {
        return jdbc.sql("SELECT status || ':' || current_step FROM core.saga_state WHERE saga_id = :t")
                .param("t", id).query(String.class).single();
    }

    @Test
    void approveSobreUnaInternaSeCompletaComoS4YSeAudita() {
        UUID dest = bank.account("0");
        UUID id = held(Bank.internal(source, dest, "77.77"));
        assertThat(bank.held(source)).isEqualByComparingTo("77.77");
        assertThat(jdbc.sql("SELECT wake_at IS NULL FROM core.saga_state WHERE saga_id = :t").param("t", id)
                .query(Boolean.class).single()).isTrue();

        Http.Response r = http.review(KEY_MOBILE, id, decision("APPROVE", "cliente verificado por teléfono"));

        assertThat(r.status()).isEqualTo(200);
        assertThat(r.json().path("status").asText()).isEqualTo("COMPLETED");
        assertThat(Contracts.openApiErrors("Transfer", r.json())).isEmpty();
        assertThat(saga(id)).isEqualTo("COMPLETED:DONE");
        assertThat(bank.balance(source)).isEqualByComparingTo("922.23");
        assertThat(bank.balance(dest)).isEqualByComparingTo("77.77");
        assertThat(bank.held(source)).isEqualByComparingTo("0");
        assertThat(bank.countEntries(id, "D")).isEqualTo(1);
        assertThat(Events.types(jdbc, id)).containsExactly("TransferInitiated", "TransferScored", "TransferHeld",
                "TransferCompleted");
        // Auditoría TRANSFER_REVIEWED: actor = client_id del canal, con decisión y comentario.
        var audit = jdbc.sql("""
                SELECT actor, detail->>'decision' AS decision, detail->>'comment' AS comment,
                       detail->>'from_status' AS from_status, detail->>'to_status' AS to_status
                FROM audit.audit_log WHERE entity_id = :t AND action = 'TRANSFER_REVIEWED'""")
                .param("t", id.toString()).query().singleRow();
        assertThat(audit).containsEntry("actor", CLIENT_MOBILE).containsEntry("decision", "APPROVE")
                .containsEntry("comment", "cliente verificado por teléfono")
                .containsEntry("from_status", "IN_REVIEW").containsEntry("to_status", "COMPLETED");
        assertThat(bank.reconcile()).containsExactly(0, 0);
        Events.assertAllValid(jdbc, id);
    }

    @Test
    void approveSobreUnaExternaReanudaComoS5ConSuInstruccion() {
        UUID id = held(Bank.external(source, "77.77"));

        Http.Response r = http.review(KEY_WEB, id, decision("APPROVE", "ok"));

        assertThat(r.status()).isEqualTo(200);
        assertThat(r.json().path("status").asText()).isEqualTo("PROCESSING");
        assertThat(saga(id)).isEqualTo("WAITING_REPLY:CREDIT_REQUESTED");
        assertThat(jdbc.sql("SELECT wake_at IS NOT NULL FROM core.saga_state WHERE saga_id = :t").param("t", id)
                .query(Boolean.class).single()).isTrue();
        assertThat(Events.types(jdbc, id)).containsExactly("TransferInitiated", "TransferScored", "TransferHeld",
                "CreditInstruction");
        assertThat(bank.held(source)).isEqualByComparingTo("77.77");
        assertThat(bank.reconcile()).containsExactly(0, 0);
        Events.assertAllValid(jdbc, id);
    }

    @Test
    void rejectLiberaLaRetencionYDejaRejectedConAnalystReject() {
        UUID id = held(Bank.external(source, "77.77"));

        Http.Response r = http.review(KEY_WEB, id, decision("REJECT", "sospecha de fraude"));

        assertThat(r.status()).isEqualTo(200);
        assertThat(r.json().path("status").asText()).isEqualTo("REJECTED");
        assertThat(r.json().path("failure_reason").asText()).isEqualTo("ANALYST_REJECT");
        assertThat(saga(id)).isEqualTo("COMPENSATED:DONE");
        assertThat(bank.held(source)).isEqualByComparingTo("0");
        assertThat(bank.balance(source)).isEqualByComparingTo("1000.00");
        assertThat(Events.types(jdbc, id)).containsExactly("TransferInitiated", "TransferScored", "TransferHeld",
                "TransferRejected");
        assertThat(Events.outbox(jdbc, id).get(3).payload().path("reason").asText()).isEqualTo("ANALYST_REJECT");
        assertThat(jdbc.sql("SELECT actor || ':' || (detail->>'decision') FROM audit.audit_log "
                + "WHERE entity_id = :t AND action = 'TRANSFER_REVIEWED'").param("t", id.toString())
                .query(String.class).single()).isEqualTo("web:REJECT");
        assertThat(bank.reconcile()).containsExactly(0, 0);
        Events.assertAllValid(jdbc, id);
    }

    @Test
    void unaTransferenciaQueYaNoEstaEnRevisionDa409YUnaInexistenteDa404() {
        UUID id = held(Bank.external(source, "77.77"));
        assertThat(http.review(KEY_WEB, id, decision("REJECT", "no")).status()).isEqualTo(200);

        Http.Response again = http.review(KEY_WEB, id, decision("APPROVE", "me arrepentí"));
        assertThat(again.status()).isEqualTo(409);
        assertThat(again.json().path("type").asText()).isEqualTo("review-not-pending");
        assertThat(again.header("Content-Type")).startsWith("application/problem+json");
        assertThat(Contracts.openApiErrors("Problem", again.json())).isEmpty();
        assertThat(jdbc.sql("SELECT status FROM core.transfers WHERE transfer_id = :t").param("t", id)
                .query(String.class).single()).isEqualTo("REJECTED");

        // Una transferencia que nunca pasó por revisión tampoco admite decisión.
        UUID dest = bank.account("0");
        Http.Response done = http.postTransfer(KEY_WEB, UUID.randomUUID(), Bank.internal(source, dest, "5.00"));
        UUID completed = UUID.fromString(done.json().path("transfer_id").asText());
        assertThat(http.review(KEY_WEB, completed, decision("REJECT", "x")).json().path("type").asText())
                .isEqualTo("review-not-pending");

        Http.Response missing = http.review(KEY_WEB, UUID.randomUUID(), decision("APPROVE", "x"));
        assertThat(missing.status()).isEqualTo(404);
        assertThat(missing.json().path("type").asText()).isEqualTo("transfer-not-found");
    }

    @Test
    void unaDecisionInvalidaOSinClaveSeRechazaAntesDeTocarNada() {
        UUID id = held(Bank.external(source, "77.77"));

        assertThat(http.review(KEY_WEB, id, "{\"decision\":\"MAYBE\",\"comment\":\"x\"}").status()).isEqualTo(400);
        assertThat(http.review(KEY_WEB, id, "{\"decision\":\"APPROVE\"}").status()).isEqualTo(400);
        assertThat(http.review(KEY_WEB, id, "{\"decision\":\"APPROVE\",\"comment\":\"\"}").status()).isEqualTo(400);
        assertThat(http.review(KEY_WEB, id, "{\"decision\":\"APPROVE\",\"comment\":\"" + "x".repeat(1001) + "\"}")
                .status()).isEqualTo(400);
        assertThat(http.review(null, id, decision("APPROVE", "x")).status()).isEqualTo(401);
        assertThat(http.review("clave-inválida", id, decision("APPROVE", "x")).status()).isEqualTo(401);

        assertThat(jdbc.sql("SELECT status FROM core.transfers WHERE transfer_id = :t").param("t", id)
                .query(String.class).single()).isEqualTo("IN_REVIEW");
        assertThat(bank.held(source)).isEqualByComparingTo("77.77");
    }

    @Test
    void dosAnalistasAlMismoTiempoUnoRecibe200YElOtro409SinDobleEfecto() throws Exception {
        UUID dest = bank.account("0");
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            for (int round = 0; round < 6; round++) {
                UUID id = held(Bank.internal(source, dest, "77.77"));
                CountDownLatch go = new CountDownLatch(1);
                // Decisiones contrapuestas desde dos canales: solo una puede ganar.
                Future<Http.Response> approve = pool.submit(() -> {
                    go.await();
                    return http.review(KEY_WEB, id, decision("APPROVE", "a"));
                });
                Future<Http.Response> reject = pool.submit(() -> {
                    go.await();
                    return http.review(KEY_MOBILE, id, decision("REJECT", "b"));
                });
                go.countDown();
                List<Integer> statuses = new ArrayList<>(List.of(approve.get().status(), reject.get().status()));
                statuses.sort(Integer::compare);

                assertThat(statuses).as("ronda %d", round).containsExactly(200, 409);
                String status = jdbc.sql("SELECT status FROM core.transfers WHERE transfer_id = :t").param("t", id)
                        .query(String.class).single();
                assertThat(status).isIn("COMPLETED", "REJECTED");
                assertThat(bank.countEntries(id, "D")).isEqualTo("COMPLETED".equals(status) ? 1 : 0);
                assertThat(bank.countEntries(id, "C")).isEqualTo("COMPLETED".equals(status) ? 1 : 0);
                assertThat(jdbc.sql("SELECT count(*) FROM audit.audit_log WHERE entity_id = :t "
                        + "AND action = 'TRANSFER_REVIEWED'").param("t", id.toString()).query(Integer.class).single())
                        .isEqualTo(1);
                assertThat(Events.types(jdbc, id).stream().filter(t -> t.equals("TransferCompleted")
                        || t.equals("TransferRejected")).count()).isEqualTo(1);
                assertThat(bank.reconcile()).containsExactly(0, 0);
            }
            assertThat(bank.held(source)).isEqualByComparingTo("0");
        } finally {
            pool.shutdownNow();
        }
    }

    @Test
    void dosAprobacionesSimultaneasTampocoDuplicanElAsiento() throws Exception {
        UUID dest = bank.account("0");
        UUID id = held(Bank.internal(source, dest, "77.77"));
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            CountDownLatch go = new CountDownLatch(1);
            Future<Http.Response> a = pool.submit(() -> {
                go.await();
                return http.review(KEY_WEB, id, decision("APPROVE", "a"));
            });
            Future<Http.Response> b = pool.submit(() -> {
                go.await();
                return http.review(KEY_MOBILE, id, decision("APPROVE", "b"));
            });
            go.countDown();
            List<Integer> statuses = new ArrayList<>(List.of(a.get().status(), b.get().status()));
            statuses.sort(Integer::compare);
            assertThat(statuses).containsExactly(200, 409);
            assertThat(bank.countEntries(id, "D")).isEqualTo(1);
            assertThat(bank.balance(dest)).isEqualByComparingTo("77.77");
            JsonNode view = http.get(KEY_WEB, "/api/v1/transfers/" + id).json();
            assertThat(view.path("status").asText()).isEqualTo("COMPLETED");
        } finally {
            pool.shutdownNow();
        }
    }
}
