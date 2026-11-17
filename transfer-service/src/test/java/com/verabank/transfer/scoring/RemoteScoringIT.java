package com.verabank.transfer.scoring;

import static com.github.tomakehurst.wiremock.client.WireMock.aResponse;
import static com.github.tomakehurst.wiremock.client.WireMock.equalTo;
import static com.github.tomakehurst.wiremock.client.WireMock.matchingJsonPath;
import static com.github.tomakehurst.wiremock.client.WireMock.post;
import static com.github.tomakehurst.wiremock.client.WireMock.postRequestedFor;
import static com.github.tomakehurst.wiremock.client.WireMock.urlEqualTo;
import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.github.tomakehurst.wiremock.WireMockServer;
import com.github.tomakehurst.wiremock.core.WireMockConfiguration;
import com.verabank.transfer.support.Bank;
import com.verabank.transfer.support.Contracts;
import com.verabank.transfer.support.Events;
import com.verabank.transfer.support.Http;
import io.github.resilience4j.circuitbreaker.CircuitBreaker;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.test.annotation.DirtiesContext;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.transaction.PlatformTransactionManager;

/**
 * Integración con el scoring remoto (modo {@code remote}) sobre PostgreSQL y Kafka reales y risk-service simulado:
 * decisiones del modelo y del fallback, persistencia, eventos y esquema de TransferScored.
 *
 * <p>Usa su propio PostgreSQL ({@link IsolatedPostgres}) y cierra su contexto al terminar: con fallback, TransferScored se
 * emite sin score (D7) y EventContractIT, que lee todo el tópico compartido, supone que siempre lo lleva. Tampoco suma
 * un pool de conexiones al PostgreSQL compartido, que tiene un máximo.
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
        properties = {"verabank.outbox.relay-enabled=false", "verabank.saga.poller-enabled=false",
                "verabank.rail.replies-enabled=false"})
@DirtiesContext(classMode = DirtiesContext.ClassMode.AFTER_CLASS)
class RemoteScoringIT {

    static final String KEY_WEB = "test-key-web-6f1d2c9a7b3e";
    static final String KEY_MOBILE = "test-key-mobile-0c4b8e2f9d1a";

    private static final String PATH = "/internal/v1/scores";
    private static final ObjectMapper JSON = new ObjectMapper();
    private static final WireMockServer RISK = new WireMockServer(WireMockConfiguration.options().dynamicPort());

    static {
        RISK.start();
    }

    @DynamicPropertySource
    static void remoteScoring(DynamicPropertyRegistry r) {
        r.add("spring.datasource.url", IsolatedPostgres.POSTGRES::getJdbcUrl);
        r.add("spring.datasource.username", () -> IsolatedPostgres.TRANSFER_USER);
        r.add("spring.datasource.password", () -> IsolatedPostgres.TRANSFER_PASSWORD);
        r.add("verabank.migrations-dir", () -> IsolatedPostgres.path("verabank.migrations.dir", "../db/migrations").toString());
        r.add("verabank.api-keys", () -> "web:" + KEY_WEB + ",mobile:" + KEY_MOBILE);
        r.add("verabank.saga.resume-after", () -> "2s");
        // El relay y el consumidor del rail están apagados: no se usa Kafka, pero Spring exige una dirección.
        r.add("spring.kafka.bootstrap-servers", () -> "localhost:1");
        r.add("verabank.scoring.mode", () -> "remote");
        r.add("verabank.scoring.remote.url", RISK::baseUrl);
    }

    @AfterAll
    static void stopRisk() {
        RISK.stop();
    }

    @LocalServerPort
    int port;
    @Autowired
    JdbcClient jdbc;
    @Autowired
    PlatformTransactionManager txManager;
    @Autowired
    FraudScorer scorer;

    private Http http;
    private Bank bank;
    private UUID source;

    @BeforeEach
    void setUp() {
        assertThat(scorer).isInstanceOf(RemoteScorer.class);
        ((RemoteScorer) scorer).circuitBreaker().reset();
        RISK.resetAll();
        http = new Http(port);
        bank = new Bank(jdbc, txManager);
        source = bank.account("1000.00");
        // Calienta el cliente HTTP (la primera llamada de un contexto frío puede pasar de 120 ms).
        stub(200, model("APPROVE", "0.01000"), 0);
        for (int i = 0; i < 5; i++) {
            scorer.score(new ScoreRequest(UUID.randomUUID(), "a", "b", new java.math.BigDecimal("1.00"), "PEN",
                    "TRANSFER", "PE", java.time.Instant.now()));
        }
        ((RemoteScorer) scorer).circuitBreaker().reset();
        RISK.resetRequests();
    }

    private static String model(String decision, String score) {
        return "{\"score\":%s,\"decision\":\"%s\",\"model_version\":\"fraud-xgb/6\"}".formatted(score, decision);
    }

    private static void stub(int status, String body, int delayMs) {
        RISK.stubFor(post(urlEqualTo(PATH)).willReturn(aResponse().withStatus(status)
                .withHeader("Content-Type", "application/json").withBody(body).withFixedDelay(delayMs)));
    }

    private UUID transfer(Http.Response r) {
        return UUID.fromString(r.json().path("transfer_id").asText());
    }

    private String row(UUID id, String columns) {
        return jdbc.sql("SELECT " + columns + " FROM core.transfers WHERE transfer_id = :t").param("t", id)
                .query(String.class).single();
    }

    // --- decisiones del modelo ------------------------------------------------------------------------------------

    @Test
    void blockReleasesTheHoldAndEmitsTransferRejectedWithRiskBlock() {
        stub(200, model("BLOCK", "0.97000"), 0);
        UUID dest = bank.account("0");

        Http.Response r = http.postTransfer(KEY_WEB, UUID.randomUUID(), Bank.internal(source, dest, "100.00"));

        assertThat(r.status()).isEqualTo(422);
        assertThat(r.json().path("type").asText()).isEqualTo("transfer-declined");
        UUID id = UUID.fromString(jdbc.sql("SELECT transfer_id FROM core.transfers WHERE source_account_id = :s")
                .param("s", source).query(String.class).single());
        assertThat(row(id, "status || '|' || failure_reason")).isEqualTo("REJECTED|RISK_BLOCK");
        assertThat(row(id, "fraud_score::text || '|' || fraud_decision || '|' || decision_source || '|' || model_version"))
                .isEqualTo("0.97000|BLOCK|MODEL|fraud-xgb/6");
        assertThat(Integer.parseInt(row(id, "scoring_latency_ms::text"))).isBetween(0, 500);
        assertThat(bank.held(source)).isEqualByComparingTo("0");
        assertThat(bank.balance(source)).isEqualByComparingTo("1000.00");
        assertThat(Events.types(jdbc, id)).containsExactly("TransferInitiated", "TransferScored", "TransferRejected");
        assertThat(Events.outbox(jdbc, id).get(2).payload().path("reason").asText()).isEqualTo("RISK_BLOCK");
        assertThat(Events.outbox(jdbc, id).get(1).payload().path("score").asDouble()).isEqualTo(0.97);
        assertThat(bank.reconcile()).containsExactly(0, 0);
        Events.assertAllValid(jdbc, id);
    }

    @Test
    void reviewMovesToInReviewAndTheAnalystDecisionStillWorks() {
        stub(200, model("REVIEW", "0.85000"), 0);
        UUID dest = bank.account("0");

        Http.Response held = http.postTransfer(KEY_WEB, UUID.randomUUID(), Bank.internal(source, dest, "250.00"));

        assertThat(held.status()).isEqualTo(201);
        assertThat(held.json().path("status").asText()).isEqualTo("IN_REVIEW");
        UUID id = transfer(held);
        assertThat(bank.held(source)).isEqualByComparingTo("250.00");
        assertThat(Events.types(jdbc, id)).containsExactly("TransferInitiated", "TransferScored", "TransferHeld");

        Http.Response approved = http.review(KEY_MOBILE, id, "{\"decision\":\"APPROVE\",\"comment\":\"verificado\"}");

        assertThat(approved.status()).isEqualTo(200);
        assertThat(approved.json().path("status").asText()).isEqualTo("COMPLETED");
        assertThat(bank.balance(dest)).isEqualByComparingTo("250.00");
        assertThat(bank.held(source)).isEqualByComparingTo("0");
        assertThat(bank.reconcile()).containsExactly(0, 0);
        Events.assertAllValid(jdbc, id);
    }

    @Test
    void approveCompletesAnInternalTransferWithTheModelsScore() {
        stub(200, model("APPROVE", "0.12345"), 0);
        UUID dest = bank.account("0");

        Http.Response r = http.postTransfer(KEY_WEB, UUID.randomUUID(), Bank.internal(source, dest, "300.00"));

        assertThat(r.status()).isEqualTo(201);
        assertThat(r.json().path("status").asText()).isEqualTo("COMPLETED");
        UUID id = transfer(r);
        assertThat(row(id, "fraud_score::text")).isEqualTo("0.12345");
        assertThat(Events.types(jdbc, id)).containsExactly("TransferInitiated", "TransferScored", "TransferCompleted");
        assertThat(bank.reconcile()).containsExactly(0, 0);
    }

    // --- fallback (D7) --------------------------------------------------------------------------------------------

    @Test
    void fallbackApprovesUpTo500WithoutScoreAndTransferScoredHasNoScore() {
        stub(503, "{\"type\":\"model-not-loaded\"}", 0);
        UUID dest = bank.account("0");

        Http.Response r = http.postTransfer(KEY_WEB, UUID.randomUUID(), Bank.internal(source, dest, "500.00"));

        assertThat(r.status()).isEqualTo(201);
        assertThat(r.json().path("status").asText()).isEqualTo("COMPLETED");
        UUID id = transfer(r);
        assertThat(jdbc.sql("SELECT fraud_score IS NULL FROM core.transfers WHERE transfer_id = :t").param("t", id)
                .query(Boolean.class).single()).isTrue();
        assertThat(row(id, "fraud_decision || '|' || decision_source || '|' || model_version"))
                .isEqualTo("APPROVE|FALLBACK|fallback-v1");
        JsonNode scored = Events.outbox(jdbc, id).get(1).payload();
        assertThat(scored.has("score")).isFalse();
        assertThat(scored.path("decision_source").asText()).isEqualTo("FALLBACK");
        assertThat(scored.path("model_version").asText()).isEqualTo("fallback-v1");
        assertThat(Contracts.payloadErrors("TransferScored", scored)).isEmpty();
        Events.assertAllValid(jdbc, id);
        assertThat(bank.reconcile()).containsExactly(0, 0);
    }

    @Test
    void fallbackSendsAnAmountAbove500ToReviewAndTheAnalystCanRejectIt() {
        stub(200, model("APPROVE", "0.01000"), 500);   // T-12: tarda 500 ms, a los 120 ms ya hay fallback
        UUID dest = bank.account("0");
        long start = System.nanoTime();

        Http.Response held = http.postTransfer(KEY_WEB, UUID.randomUUID(), Bank.internal(source, dest, "500.01"));

        assertThat(TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start)).as("no espera a risk-service").isLessThan(400);
        assertThat(held.status()).isEqualTo(201);
        assertThat(held.json().path("status").asText()).isEqualTo("IN_REVIEW");
        UUID id = transfer(held);
        assertThat(row(id, "fraud_decision || '|' || decision_source || '|' || model_version"))
                .isEqualTo("REVIEW|FALLBACK|fallback-v1");
        assertThat(Events.types(jdbc, id)).containsExactly("TransferInitiated", "TransferScored", "TransferHeld");

        Http.Response rejected = http.review(KEY_WEB, id, "{\"decision\":\"REJECT\",\"comment\":\"sin respaldo\"}");

        assertThat(rejected.status()).isEqualTo(200);
        assertThat(row(id, "status || '|' || failure_reason")).isEqualTo("REJECTED|ANALYST_REJECT");
        assertThat(bank.held(source)).isEqualByComparingTo("0");
        assertThat(bank.reconcile()).containsExactly(0, 0);
        Events.assertAllValid(jdbc, id);
    }

    @Test
    void aBadRequestFromRiskServiceUsesTheFallbackWithoutOpeningTheCircuit() {
        stub(400, "{\"type\":\"validation-error\"}", 0);
        UUID dest = bank.account("0");
        for (int i = 0; i < 12; i++) {
            Http.Response r = http.postTransfer(KEY_WEB, UUID.randomUUID(), Bank.internal(source, dest, "10.00"));
            assertThat(r.status()).isEqualTo(201);
        }
        assertThat(((RemoteScorer) scorer).circuitBreaker().getState()).isEqualTo(CircuitBreaker.State.CLOSED);
        assertThat(jdbc.sql("SELECT count(*) FROM core.transfers WHERE source_account_id = :s AND decision_source = 'FALLBACK'")
                .param("s", source).query(Integer.class).single()).isEqualTo(12);
        assertThat(bank.reconcile()).containsExactly(0, 0);
    }

    @Test
    void withTheCircuitOpenRiskServiceIsNotCalledAndTransfersStillGetADecision() {
        stub(503, "{}", 0);
        UUID dest = bank.account("0");
        for (int i = 0; i < 10; i++) {
            assertThat(http.postTransfer(KEY_WEB, UUID.randomUUID(), Bank.internal(source, dest, "10.00")).status())
                    .isEqualTo(201);
        }
        assertThat(((RemoteScorer) scorer).circuitBreaker().getState()).isEqualTo(CircuitBreaker.State.OPEN);
        int calls = RISK.getAllServeEvents().size();

        Http.Response r = http.postTransfer(KEY_WEB, UUID.randomUUID(), Bank.internal(source, dest, "10.00"));

        assertThat(r.status()).isEqualTo(201);
        assertThat(r.json().path("status").asText()).isEqualTo("COMPLETED");
        assertThat(RISK.getAllServeEvents()).hasSize(calls);
        assertThat(bank.reconcile()).containsExactly(0, 0);
    }

    // --- la llamada ------------------------------------------------------------------------------------------------

    @Test
    void theCallCarriesTheTransferIdAndTheCorrelationIdAndRunsOutsideAnyTransaction() throws Exception {
        stub(200, model("APPROVE", "0.05000"), 300);
        UUID dest = bank.account("0");
        UUID idempotencyKey = UUID.randomUUID();

        CompletableFuture<Http.Response> pending = CompletableFuture.supplyAsync(
                () -> http.postTransfer(KEY_WEB, idempotencyKey, Bank.internal(source, dest, "20.00")));
        // Mientras risk-service tarda, tx1 ya está confirmada: la transferencia PENDING y su retención son visibles.
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
        boolean visibleMidCall = false;
        while (System.nanoTime() < deadline && !pending.isDone()) {
            List<String> rows = jdbc.sql("SELECT status FROM core.transfers WHERE source_account_id = :s")
                    .param("s", source).query(String.class).list();
            if (rows.contains("PENDING") && bank.held(source).signum() > 0) {
                visibleMidCall = true;
                break;
            }
            Thread.sleep(10);
        }
        Http.Response r = pending.get(10, TimeUnit.SECONDS);

        assertThat(visibleMidCall).as("tx1 confirmada y sin transacción abierta durante el scoring").isTrue();
        assertThat(r.status()).isEqualTo(201);
        UUID id = transfer(r);
        RISK.verify(1, postRequestedFor(urlEqualTo(PATH))
                .withRequestBody(matchingJsonPath("$.transfer_id", equalTo(id.toString())))
                .withRequestBody(matchingJsonPath("$.amount", equalTo("20.00")))
                .withRequestBody(matchingJsonPath("$.transfer_type", equalTo("TRANSFER")))
                .withHeader("X-Correlation-Id", equalTo(r.header("X-Correlation-Id"))));
    }

    // --- esquema de TransferScored (único cambio de contrato; anotado para ADR-12) ----------------------------------

    private static JsonNode scored(String source, String score) throws Exception {
        String scoreField = score == null ? "" : "\"score\":" + score + ",";
        return JSON.readTree("""
                {"transfer_id":"%s",%s"decision":"APPROVE","decision_source":"%s","model_version":"x","latency_ms":3}"""
                .formatted(UUID.randomUUID(), scoreField, source));
    }

    @Test
    void theSchemaRequiresScoreWithAModelAndAllowsItsAbsenceInFallback() throws Exception {
        assertThat(Contracts.payloadErrors("TransferScored", scored("MODEL", "0.5"))).isEmpty();
        assertThat(Contracts.payloadErrors("TransferScored", scored("MODEL", null))).isNotEmpty();
        assertThat(Contracts.payloadErrors("TransferScored", scored("FALLBACK", null))).isEmpty();
        assertThat(Contracts.payloadErrors("TransferScored", scored("FALLBACK", "0.5"))).isEmpty();
        assertThat(Contracts.payloadErrors("TransferScored", scored("MODEL", "1.5"))).isNotEmpty();
        assertThat(Contracts.payloadErrors("TransferScored", scored("FALLBACK", "1.5"))).isNotEmpty();
        assertThat(Contracts.payloadErrors("TransferScored", JSON.readTree(
                "{\"transfer_id\":\"%s\",\"decision\":\"APPROVE\",\"model_version\":\"x\",\"latency_ms\":3}"
                        .formatted(UUID.randomUUID())))).as("sin decision_source").isNotEmpty();
    }
}
