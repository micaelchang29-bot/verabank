package com.verabank.transfer.scoring;

import static com.github.tomakehurst.wiremock.client.WireMock.aResponse;
import static com.github.tomakehurst.wiremock.client.WireMock.equalTo;
import static com.github.tomakehurst.wiremock.client.WireMock.equalToJson;
import static com.github.tomakehurst.wiremock.client.WireMock.get;
import static com.github.tomakehurst.wiremock.client.WireMock.getRequestedFor;
import static com.github.tomakehurst.wiremock.client.WireMock.matching;
import static com.github.tomakehurst.wiremock.client.WireMock.post;
import static com.github.tomakehurst.wiremock.client.WireMock.postRequestedFor;
import static com.github.tomakehurst.wiremock.client.WireMock.urlEqualTo;
import static org.awaitility.Awaitility.await;
import static org.assertj.core.api.Assertions.assertThat;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.github.tomakehurst.wiremock.WireMockServer;
import com.github.tomakehurst.wiremock.core.WireMockConfiguration;
import io.github.resilience4j.circuitbreaker.CircuitBreaker;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import java.math.BigDecimal;
import java.net.ServerSocket;
import java.time.Duration;
import java.time.Instant;
import java.util.UUID;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;

/** T-12 y el comportamiento del scoring remoto frente a risk-service simulado con WireMock; sin base de datos. */
class RemoteScorerTest {

    private static final String PATH = "/internal/v1/scores";
    private static final String OK = "{\"score\":0.91234,\"decision\":\"BLOCK\",\"model_version\":\"fraud-xgb/6\"}";

    private static WireMockServer server;
    private SimpleMeterRegistry meters;
    private RemoteScorer scorer;

    @BeforeAll
    static void start() {
        server = new WireMockServer(WireMockConfiguration.options().dynamicPort());
        server.start();
        // Calienta clases y JIT del cliente HTTP: la primera llamada de una JVM fría no debe medirse.
        server.stubFor(post(urlEqualTo(PATH)).willReturn(aResponse().withStatus(200).withBody(OK)));
        RemoteScorer warm = scorer(server.baseUrl(), Duration.ofSeconds(10), new SimpleMeterRegistry());
        for (int i = 0; i < 20; i++) {
            warm.score(request("10.00"));
        }
    }

    @AfterAll
    static void stop() {
        server.stop();
    }

    @BeforeEach
    void setUp() {
        server.resetAll();
        meters = new SimpleMeterRegistry();
        scorer = scorer(server.baseUrl(), Duration.ofMillis(600), meters);
    }

    private static RemoteScorer scorer(String url, Duration openDuration, SimpleMeterRegistry meters) {
        ScoringProperties properties = new ScoringProperties("remote", new BigDecimal("500.00"),
                new ScoringProperties.Remote(url, Duration.ofMillis(50), Duration.ofMillis(120)),
                new ScoringProperties.CircuitBreaker(20, 10, 50, openDuration, 5));
        return new RemoteScorer(properties, new ObjectMapper(), meters);
    }

    private static ScoreRequest request(String amount) {
        return new ScoreRequest(UUID.randomUUID(), "11111111-1111-1111-1111-111111111111",
                "22222222-2222-2222-2222-222222222222", new BigDecimal(amount), "PEN", "TRANSFER", "PE",
                Instant.parse("2026-01-10T10:00:00Z"));
    }

    private void stub(int status, String body, int delayMs) {
        server.stubFor(post(urlEqualTo(PATH)).willReturn(aResponse().withStatus(status)
                .withHeader("Content-Type", "application/json").withBody(body).withFixedDelay(delayMs)));
    }

    private double fallbacks(String reason) {
        var counter = meters.find("scoring.fallback.total").tag("reason", reason).counter();
        return counter == null ? 0 : counter.count();
    }

    // --- camino feliz ---------------------------------------------------------------------------------------------

    @Test
    void aGoodResponseIsTheModelsAndTheRequestFollowsTheContract() {
        stub(200, OK, 0);
        UUID id = UUID.randomUUID();
        String correlation = UUID.randomUUID().toString();
        MDC.put("correlation_id", correlation);
        ScoreResult result;
        try {
            result = scorer.score(new ScoreRequest(id, "src-uuid", "ext-ref-9", new BigDecimal("1250.50"), "PEN",
                    "CASH_OUT", "PE", Instant.parse("2026-01-10T10:00:00Z")));
        } finally {
            MDC.remove("correlation_id");
        }
        assertThat(result.source()).isEqualTo(DecisionSource.MODEL);
        assertThat(result.decision()).isEqualTo(Decision.BLOCK);
        assertThat(result.score()).isEqualByComparingTo("0.91234");
        assertThat(result.modelVersion()).isEqualTo("fraud-xgb/6");
        assertThat(result.latencyMs()).isBetween(0, 120);
        server.verify(1, postRequestedFor(urlEqualTo(PATH))
                .withHeader("X-Correlation-Id", equalTo(correlation))
                .withHeader("Content-Type", equalTo("application/json"))
                .withRequestBody(equalToJson("""
                        {"transfer_id":"%s","source_ref":"src-uuid","dest_ref":"ext-ref-9","amount":"1250.50",
                         "currency":"PEN","transfer_type":"CASH_OUT","dest_country":"PE",
                         "occurred_at":"2026-01-10T10:00:00Z"}""".formatted(id))));
        assertThat(meters.find("scoring.remote.duration").tag("outcome", "ok").timer().count()).isEqualTo(1);
    }

    @Test
    void withoutACorrelationInTheLoggingContextAFreshUuidIsSent() {
        stub(200, OK, 0);
        scorer.score(request("10.00"));
        server.verify(postRequestedFor(urlEqualTo(PATH)).withHeader("X-Correlation-Id",
                matching("[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")));
    }

    // --- T-12 -----------------------------------------------------------------------------------------------------

    @Test
    void t12_aSlowResponseFallsBackAt120msByAmountWithoutScore() {
        stub(200, OK, 500);
        long start = System.nanoTime();
        ScoreResult small = scorer.score(request("100.00"));
        long elapsedMs = (System.nanoTime() - start) / 1_000_000;
        assertThat(elapsedMs).as("no espera los 500 ms de risk-service").isBetween(100L, 350L);
        assertThat(small.source()).isEqualTo(DecisionSource.FALLBACK);
        assertThat(small.decision()).isEqualTo(Decision.APPROVE);
        assertThat(small.score()).isNull();
        assertThat(small.modelVersion()).isEqualTo("fallback-v1");
        assertThat(small.latencyMs()).isBetween(100, 350);
        assertThat(scorer.score(request("900.00")).decision()).isEqualTo(Decision.REVIEW);
        assertThat(fallbacks("timeout")).isEqualTo(2);
        assertThat(meters.find("scoring.remote.duration").tag("outcome", "timeout").timer().count()).isEqualTo(2);
    }

    @Test
    void t12_enoughFailuresOpenTheCircuitThenItHalfOpensAndClosesOnRecovery() throws Exception {
        stub(200, OK, 500);
        for (int i = 0; i < 10; i++) {   // mínimo de 10 llamadas con 100 % de fallos
            assertThat(scorer.score(request("100.00")).source()).isEqualTo(DecisionSource.FALLBACK);
        }
        assertThat(scorer.circuitBreaker().getState()).isEqualTo(CircuitBreaker.State.OPEN);
        assertThat(meters.get("scoring.circuit.state").gauge().value()).isEqualTo(2.0);
        int received = server.getAllServeEvents().size();

        // Abierto: no se intenta la llamada y la respuesta es inmediata.
        long start = System.nanoTime();
        ScoreResult open = scorer.score(request("100.00"));
        assertThat((System.nanoTime() - start) / 1_000_000).isLessThan(50);
        assertThat(open.source()).isEqualTo(DecisionSource.FALLBACK);
        assertThat(server.getAllServeEvents()).hasSize(received);
        assertThat(fallbacks("open")).isEqualTo(1);

        // risk-service se recupera: pasado el tiempo abierto, semiabierto y, con 5 llamadas buenas, cerrado.
        stub(200, OK, 0);
        Thread.sleep(700);
        assertThat(scorer.score(request("100.00")).source()).isEqualTo(DecisionSource.MODEL);
        assertThat(scorer.circuitBreaker().getState()).isEqualTo(CircuitBreaker.State.HALF_OPEN);
        assertThat(meters.get("scoring.circuit.state").gauge().value()).isEqualTo(1.0);
        for (int i = 0; i < 4; i++) {
            assertThat(scorer.score(request("100.00")).source()).isEqualTo(DecisionSource.MODEL);
        }
        assertThat(scorer.circuitBreaker().getState()).isEqualTo(CircuitBreaker.State.CLOSED);
        assertThat(meters.get("scoring.circuit.state").gauge().value()).isEqualTo(0.0);
    }

    @Test
    void theCircuitUsesTheSpecifiedConfiguration() {
        var config = scorer.circuitBreaker().getCircuitBreakerConfig();
        assertThat(config.getSlidingWindowType()).isEqualTo(
                io.github.resilience4j.circuitbreaker.CircuitBreakerConfig.SlidingWindowType.COUNT_BASED);
        assertThat(config.getSlidingWindowSize()).isEqualTo(20);
        assertThat(config.getMinimumNumberOfCalls()).isEqualTo(10);
        assertThat(config.getFailureRateThreshold()).isEqualTo(50f);
        assertThat(config.getPermittedNumberOfCallsInHalfOpenState()).isEqualTo(5);
    }

    @Test
    void halfTheFailuresWithTheMinimumOfCallsOpenItAndFewerDoNot() {
        stub(503, "{}", 0);
        for (int i = 0; i < 9; i++) {
            scorer.score(request("100.00"));
        }
        assertThat(scorer.circuitBreaker().getState()).as("9 llamadas: aún por debajo del mínimo")
                .isEqualTo(CircuitBreaker.State.CLOSED);
        scorer.score(request("100.00"));
        assertThat(scorer.circuitBreaker().getState()).isEqualTo(CircuitBreaker.State.OPEN);
    }

    // --- fronteras del fallback -----------------------------------------------------------------------------------

    @Test
    void fallbackBoundaryIs500Exactly() {
        FallbackScorer fallback = new FallbackScorer(new BigDecimal("500.00"));
        assertThat(fallback.score(request("500.00")).decision()).isEqualTo(Decision.APPROVE);
        assertThat(fallback.score(request("500.01")).decision()).isEqualTo(Decision.REVIEW);
        assertThat(fallback.score(request("0.01")).decision()).isEqualTo(Decision.APPROVE);
        stub(503, "{}", 0);
        assertThat(scorer.score(request("500.00")).decision()).isEqualTo(Decision.APPROVE);
        assertThat(scorer.score(request("500.01")).decision()).isEqualTo(Decision.REVIEW);
        assertThat(scorer.score(request("500.00")).source()).isEqualTo(DecisionSource.FALLBACK);
    }

    // --- fallos de la llamada -------------------------------------------------------------------------------------

    @Test
    void connectionRefusedFallsBack() throws Exception {
        int closedPort;
        try (ServerSocket socket = new ServerSocket(0)) {
            closedPort = socket.getLocalPort();
        }
        RemoteScorer lonely = scorer("http://localhost:" + closedPort, Duration.ofSeconds(10), meters);
        ScoreResult result = lonely.score(request("600.00"));
        assertThat(result.source()).isEqualTo(DecisionSource.FALLBACK);
        assertThat(result.decision()).isEqualTo(Decision.REVIEW);
        assertThat(result.score()).isNull();
        assertThat(fallbacks("error")).isEqualTo(1);
        assertThat(lonely.circuitBreaker().getMetrics().getNumberOfFailedCalls()).isEqualTo(1);
    }

    @Test
    void aServerErrorFallsBackAndCountsForTheCircuit() {
        stub(503, "{\"type\":\"model-not-loaded\"}", 0);
        ScoreResult result = scorer.score(request("100.00"));
        assertThat(result.source()).isEqualTo(DecisionSource.FALLBACK);
        assertThat(result.decision()).isEqualTo(Decision.APPROVE);
        assertThat(fallbacks("error")).isEqualTo(1);
        assertThat(scorer.circuitBreaker().getMetrics().getNumberOfFailedCalls()).isEqualTo(1);
    }

    @Test
    void malformedResponsesFallBackAndCountForTheCircuit() {
        String[] bodies = {"no es json", "{}", "{\"score\":0.5}", "{\"score\":0.5,\"decision\":\"MAYBE\",\"model_version\":\"x\"}",
                "{\"score\":2.5,\"decision\":\"APPROVE\",\"model_version\":\"x\"}",
                "{\"score\":\"0.5\",\"decision\":\"APPROVE\",\"model_version\":\"x\"}",
                "{\"score\":0.5,\"decision\":\"APPROVE\",\"model_version\":\"\"}"};
        for (String body : bodies) {
            stub(200, body, 0);
            ScoreResult result = scorer.score(request("100.00"));
            assertThat(result.source()).as(body).isEqualTo(DecisionSource.FALLBACK);
            assertThat(result.score()).as(body).isNull();
        }
        assertThat(scorer.circuitBreaker().getMetrics().getNumberOfFailedCalls()).isEqualTo(bodies.length);
    }

    @Test
    void aBadRequestFallsBackLogsErrorAndNeverOpensTheCircuit() {
        stub(400, "{\"type\":\"validation-error\",\"detail\":\"amount inválido\"}", 0);
        Logger logger = (Logger) LoggerFactory.getLogger(RemoteScorer.class);
        ListAppender<ILoggingEvent> appender = new ListAppender<>();
        appender.start();
        logger.addAppender(appender);
        try {
            for (int i = 0; i < 30; i++) {
                ScoreResult result = scorer.score(request("100.00"));
                assertThat(result.source()).isEqualTo(DecisionSource.FALLBACK);
                assertThat(result.decision()).isEqualTo(Decision.APPROVE);
            }
        } finally {
            logger.detachAppender(appender);
        }
        assertThat(scorer.circuitBreaker().getState()).isEqualTo(CircuitBreaker.State.CLOSED);
        assertThat(scorer.circuitBreaker().getMetrics().getNumberOfFailedCalls()).isZero();
        assertThat(server.getAllServeEvents()).as("el circuito sigue intentando la llamada").hasSize(30);
        assertThat(appender.list).filteredOn(e -> e.getLevel() == Level.ERROR).hasSize(30);
        assertThat(fallbacks("error")).isEqualTo(30);
    }

    // --- calentamiento al arrancar ------------------------------------------------------------------------------------

    private static final String READY = "/q/health/ready";

    @Test
    void warmUpHitsTheReadinessAndThePostRouteWithAnInvalidTypeAndLeavesNoTraceInTheScoringMetrics() {
        server.stubFor(get(urlEqualTo(READY)).willReturn(aResponse().withStatus(200).withBody("{\"status\":\"UP\"}")));
        server.stubFor(post(urlEqualTo(PATH)).willReturn(aResponse().withStatus(400).withBody("{}")));

        scorer.warmUp();

        server.verify(1, getRequestedFor(urlEqualTo(READY)));
        // La ruta POST se calienta con un tipo inválido: risk-service responde 400 y no puntúa ni deja filas.
        server.verify(5, postRequestedFor(urlEqualTo(PATH)).withRequestBody(
                com.github.tomakehurst.wiremock.client.WireMock.containing("\"transfer_type\":\"WARM_UP\"")));
        server.verify(0, postRequestedFor(urlEqualTo(PATH)).withRequestBody(
                com.github.tomakehurst.wiremock.client.WireMock.containing("\"transfer_type\":\"TRANSFER\"")));
        assertThat(scorer.circuitBreaker().getMetrics().getNumberOfBufferedCalls()).isZero();
        assertThat(meters.find("scoring.remote.duration").timer()).isNull();
        assertThat(meters.find("scoring.fallback.total").counter()).isNull();
    }

    @Test
    void warmUpFailuresAreIgnoredAndNeverOpenOrFeedTheCircuit() throws Exception {
        server.stubFor(get(urlEqualTo(READY)).willReturn(aResponse().withStatus(503)));
        for (int i = 0; i < 25; i++) {
            scorer.warmUp();   // un 503, 25 veces: sería suficiente para abrir el circuito si contara
        }
        server.stubFor(get(urlEqualTo(READY)).willReturn(aResponse().withFixedDelay(3_000).withStatus(200)));
        scorer.warmUp();       // más lento que su tiempo máximo
        int closedPort;
        try (ServerSocket socket = new ServerSocket(0)) {
            closedPort = socket.getLocalPort();
        }
        scheduleWarmUp("http://localhost:" + closedPort);   // conexión rechazada

        assertThat(scorer.circuitBreaker().getState()).isEqualTo(CircuitBreaker.State.CLOSED);
        assertThat(scorer.circuitBreaker().getMetrics().getNumberOfBufferedCalls()).isZero();
        assertThat(meters.find("scoring.fallback.total").counter()).isNull();
        // y el scoring posterior funciona con normalidad
        stub(200, OK, 0);
        assertThat(scorer.score(request("10.00")).source()).isEqualTo(DecisionSource.MODEL);
    }

    private void scheduleWarmUp(String url) {
        scorer(url, Duration.ofSeconds(10), meters).warmUp();
    }

    @Test
    void warmUpOnStartupRunsInTheBackgroundWithoutBlockingTheCaller() {
        server.stubFor(get(urlEqualTo(READY)).willReturn(aResponse().withStatus(200).withFixedDelay(1_000)));

        long start = System.nanoTime();
        scorer.warmUpOnStartup();
        long elapsedMs = (System.nanoTime() - start) / 1_000_000;

        assertThat(elapsedMs).as("no espera la respuesta de risk-service").isLessThan(200);
        await().atMost(Duration.ofSeconds(5)).untilAsserted(() -> server.verify(1, getRequestedFor(urlEqualTo(READY))));
    }

    @AfterEach
    void closeCircuit() {
        scorer.circuitBreaker().reset();
    }
}
