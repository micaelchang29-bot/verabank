package com.verabank.transfer.scoring;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import io.github.resilience4j.circuitbreaker.CallNotPermittedException;
import io.github.resilience4j.circuitbreaker.CircuitBreaker;
import io.github.resilience4j.circuitbreaker.CircuitBreakerConfig;
import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.Gauge;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Timer;
import java.io.IOException;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpConnectTimeoutException;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.net.http.HttpTimeoutException;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.boot.context.event.ApplicationReadyEvent;
import org.springframework.context.event.EventListener;
import org.springframework.stereotype.Component;

/**
 * Scoring remoto (ADR-04): REST síncrono a risk-service con tiempo de conexión de 50 ms y total de 120 ms, detrás de
 * un circuit breaker. Si la llamada falla, tarda demasiado, el circuito está abierto o risk-service rechaza la
 * solicitud, responde {@link FallbackScorer}. Con el circuito abierto no se intenta la llamada.
 *
 * <p>Cuentan como fallo del circuito: una excepción, el tiempo agotado, un 5xx y una respuesta malformada. Un 4xx es
 * un defecto nuestro: se usa el fallback y se registra ERROR, pero no cuenta (el circuito lo ignora).
 */
@Component
@ConditionalOnProperty(prefix = "verabank.scoring", name = "mode", havingValue = "remote", matchIfMissing = true)
public class RemoteScorer implements FraudScorer {

    private static final Logger log = LoggerFactory.getLogger(RemoteScorer.class);
    private static final String CORRELATION_HEADER = "X-Correlation-Id";
    private static final String READINESS_PATH = "/q/health/ready";
    private static final Duration WARM_UP_TIMEOUT = Duration.ofSeconds(2);
    /** Tipo que risk-service rechaza con 400: el calentamiento recorre la ruta POST sin puntuar ni guardar nada. */
    static final String WARM_UP_TRANSFER_TYPE = "WARM_UP";
    private static final int WARM_UP_POSTS = 5;
    private static final String WARM_UP_RESPONSE = "{\"score\":0.01234,\"decision\":\"APPROVE\",\"model_version\":\"warm-up\"}";

    private final ScoringProperties properties;
    private final ObjectMapper mapper;
    private final MeterRegistry meters;
    private final HttpClient client;
    private final URI endpoint;
    private final URI readiness;
    private final FallbackScorer fallback;
    private final CircuitBreaker breaker;

    @Autowired
    public RemoteScorer(ScoringProperties properties, ObjectMapper mapper, MeterRegistry meters) {
        this.properties = properties;
        this.mapper = mapper;
        this.meters = meters;
        this.client = HttpClient.newBuilder()
                .version(HttpClient.Version.HTTP_1_1)
                .connectTimeout(properties.remote().connectTimeout())
                .build();
        String base = properties.remote().url().replaceAll("/+$", "");
        this.endpoint = URI.create(base + "/internal/v1/scores");
        this.readiness = URI.create(base + READINESS_PATH);
        this.fallback = new FallbackScorer(properties.fallbackLimit());
        ScoringProperties.CircuitBreaker cb = properties.circuitBreaker();
        this.breaker = CircuitBreaker.of("risk-service", CircuitBreakerConfig.custom()
                .slidingWindowType(CircuitBreakerConfig.SlidingWindowType.COUNT_BASED)
                .slidingWindowSize(cb.windowSize())
                .minimumNumberOfCalls(cb.minimumCalls())
                .failureRateThreshold(cb.failureRatePercent())
                .waitDurationInOpenState(cb.openDuration())
                .permittedNumberOfCallsInHalfOpenState(cb.halfOpenCalls())
                .ignoreExceptions(RemoteScoringException.Rejected.class)
                .build());
        Gauge.builder("scoring.circuit.state", breaker, b -> stateCode(b.getState()))
                .description("Estado del circuito de risk-service: 0 cerrado, 1 semiabierto, 2 abierto")
                .register(meters);
    }

    /**
     * Al arrancar, calienta el cliente HTTP (clases, JIT y la conexión que luego se reutiliza) con la readiness y la
     * ruta POST de risk-service. Va en segundo plano: no retrasa el arranque, su fallo solo se registra y no pasa por el circuit
     * breaker ni por las métricas del scoring. Sin él, la primera solicitud real de un proceso frío puede pasar de los
     * 120 ms y caer en fallback.
     */
    @EventListener(ApplicationReadyEvent.class)
    public void warmUpOnStartup() {
        Thread.ofPlatform().daemon().name("scoring-warm-up").start(this::warmUp);
    }

    /**
     * El calentamiento (síncrono). Nunca lanza: cualquier fallo se ignora con un INFO. Hace una petición GET a la
     * readiness y {@value #WARM_UP_POSTS} POST a la ruta de scoring con un {@code transfer_type} inválido (risk-service
     * responde 400 y no puntúa ni escribe nada), y recorre el armado del cuerpo y la lectura de una respuesta. Así la
     * primera solicitud real no paga clases, JIT ni la conexión dentro de sus 120 ms.
     */
    void warmUp() {
        long start = System.nanoTime();
        try {
            HttpRequest request = HttpRequest.newBuilder(readiness).timeout(WARM_UP_TIMEOUT).GET().build();
            HttpResponse<Void> response = client.send(request, HttpResponse.BodyHandlers.discarding());
            int posted = 0;
            for (int i = 0; i < WARM_UP_POSTS; i++) {
                ScoreRequest probe = new ScoreRequest(UUID.randomUUID(), "warm-up-source", "warm-up-dest",
                        BigDecimal.ONE, "PEN", WARM_UP_TRANSFER_TYPE, "PE", java.time.Instant.now());
                HttpRequest post = HttpRequest.newBuilder(endpoint).timeout(WARM_UP_TIMEOUT)
                        .header("Content-Type", "application/json")
                        .POST(HttpRequest.BodyPublishers.ofString(body(probe), StandardCharsets.UTF_8)).build();
                client.send(post, HttpResponse.BodyHandlers.discarding());
                parse(WARM_UP_RESPONSE);
                posted++;
            }
            log.info("calentamiento del cliente de scoring: risk-service respondió {} a la readiness y se enviaron {} POST en {} ms",
                    response.statusCode(), posted, millisSince(start));
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            log.info("calentamiento del cliente de scoring interrumpido; se ignora");
        } catch (Exception e) {
            log.info("calentamiento del cliente de scoring falló ({}); se ignora y el servicio arranca igual", e.toString());
        }
    }

    @Override
    public ScoreResult score(ScoreRequest request) {
        long start = System.nanoTime();
        String reason;
        try {
            ScoreResult result = breaker.executeCallable(() -> call(request));
            return new ScoreResult(result.score(), result.decision(), result.source(), result.modelVersion(),
                    millisSince(start));
        } catch (CallNotPermittedException e) {
            reason = "open";
        } catch (RemoteScoringException.Rejected e) {
            log.error("risk-service rechazó la solicitud de scoring de {}; se usa el fallback y el circuito no lo cuenta: {}",
                    request.transferId(), e.getMessage());
            reason = "error";
        } catch (Exception e) {
            reason = isTimeout(e) ? "timeout" : "error";
            log.warn("scoring remoto de {} falló ({}): {}", request.transferId(), reason, e.toString());
        }
        meters.counter("scoring.fallback.total", "reason", reason).increment();
        ScoreResult decided = fallback.score(request);
        return new ScoreResult(null, decided.decision(), decided.source(), decided.modelVersion(), millisSince(start));
    }

    /** Una llamada a risk-service: mide {@code scoring.remote.duration} y devuelve el resultado o lanza. */
    private ScoreResult call(ScoreRequest request) throws Exception {
        long start = System.nanoTime();
        String outcome = "error";
        try {
            ScoreResult result = send(request);
            outcome = "ok";
            return result;
        } catch (RemoteScoringException.Rejected e) {
            outcome = "rejected";
            throw e;
        } catch (Exception e) {
            outcome = isTimeout(e) ? "timeout" : "error";
            throw e;
        } finally {
            Timer.builder("scoring.remote.duration").description("Duración de la llamada a risk-service")
                    .tag("outcome", outcome).publishPercentileHistogram().register(meters)
                    .record(Duration.ofNanos(System.nanoTime() - start));
        }
    }

    private ScoreResult send(ScoreRequest request) throws Exception {
        Duration timeout = properties.remote().timeout();
        HttpRequest http = HttpRequest.newBuilder(endpoint)
                .timeout(timeout)
                .header("Content-Type", "application/json")
                .header(CORRELATION_HEADER, correlationId())
                .POST(HttpRequest.BodyPublishers.ofString(body(request), StandardCharsets.UTF_8))
                .build();
        // El tiempo total (conexión, cabeceras y cuerpo) se acota sobre el futuro, no solo hasta las cabeceras.
        CompletableFuture<HttpResponse<String>> future = client.sendAsync(http, HttpResponse.BodyHandlers.ofString());
        HttpResponse<String> response;
        try {
            response = future.get(timeout.toMillis(), TimeUnit.MILLISECONDS);
        } catch (TimeoutException e) {
            future.cancel(true);
            throw e;
        } catch (ExecutionException e) {
            if (e.getCause() instanceof Exception cause) {
                throw cause;
            }
            throw e;
        } catch (InterruptedException e) {
            future.cancel(true);
            Thread.currentThread().interrupt();
            throw e;
        }
        int status = response.statusCode();
        if (status >= 500) {
            throw new RemoteScoringException.ServerError(status);
        }
        if (status >= 400) {
            throw new RemoteScoringException.Rejected(status, abbreviate(response.body()));
        }
        if (status != 200) {
            throw new RemoteScoringException.Malformed("estado inesperado " + status, null);
        }
        return parse(response.body());
    }

    private String body(ScoreRequest r) {
        ObjectNode node = mapper.createObjectNode();
        node.put("transfer_id", r.transferId().toString());
        node.put("source_ref", r.sourceRef());
        node.put("dest_ref", r.destRef());
        node.put("amount", r.amount().toPlainString());
        node.put("currency", r.currency());
        node.put("transfer_type", r.transferType());
        node.put("dest_country", r.destCountry());
        node.put("occurred_at", r.occurredAt().toString());
        return node.toString();
    }

    private ScoreResult parse(String body) {
        try {
            JsonNode json = mapper.readTree(body);
            JsonNode score = json.get("score");
            JsonNode decision = json.get("decision");
            JsonNode version = json.get("model_version");
            if (score == null || !score.isNumber() || decision == null || !decision.isTextual()
                    || version == null || !version.isTextual() || version.asText().isEmpty()) {
                throw new IllegalArgumentException("faltan o son inválidos score, decision o model_version");
            }
            BigDecimal value = score.decimalValue().setScale(5, RoundingMode.HALF_EVEN);
            if (value.signum() < 0 || value.compareTo(BigDecimal.ONE) > 0) {
                throw new IllegalArgumentException("score fuera de [0, 1]");
            }
            return new ScoreResult(value, Decision.valueOf(decision.asText()), DecisionSource.MODEL, version.asText(), 0);
        } catch (IOException | IllegalArgumentException e) {
            throw new RemoteScoringException.Malformed("respuesta malformada de risk-service: " + e.getMessage(), e);
        }
    }

    private static String correlationId() {
        String current = MDC.get("correlation_id");
        if (current != null && !current.isBlank()) {
            return current;
        }
        return UUID.randomUUID().toString();
    }

    private static boolean isTimeout(Throwable e) {
        return e instanceof TimeoutException || e instanceof HttpTimeoutException
                || e instanceof HttpConnectTimeoutException;
    }

    private static int millisSince(long startNanos) {
        return (int) Math.min(Integer.MAX_VALUE, TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - startNanos));
    }

    private static String abbreviate(String body) {
        if (body == null) {
            return "";
        }
        return body.length() > 300 ? body.substring(0, 300) + "…" : body;
    }

    private static int stateCode(CircuitBreaker.State state) {
        return switch (state) {
            case CLOSED -> 0;
            case HALF_OPEN -> 1;
            case OPEN, FORCED_OPEN -> 2;
            default -> 0;
        };
    }

    /** Estado actual del circuito (para pruebas y diagnóstico). */
    CircuitBreaker circuitBreaker() {
        return breaker;
    }

    Counter fallbackCounter(String reason) {
        return meters.counter("scoring.fallback.total", "reason", reason);
    }
}
