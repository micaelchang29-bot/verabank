package com.verabank.risk.scoring;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.verabank.risk.scoring.FeatureComputer.Windows;
import com.verabank.risk.shared.ApiException;
import io.quarkus.arc.lookup.LookupIfProperty;
import io.smallrye.mutiny.Uni;
import io.smallrye.mutiny.infrastructure.Infrastructure;
import io.vertx.core.Context;
import io.vertx.core.Vertx;
import jakarta.annotation.PostConstruct;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.inject.Inject;
import java.math.BigDecimal;
import java.util.Map;
import java.util.UUID;

/**
 * Orquesta el scoring de una solicitud: idempotencia por {@code transfer_id}, features (dos consultas concurrentes),
 * inferencia en el pool de workers, decisión, bitácora y sombra. Las etapas se miden en
 * {@code scoring.stage.duration{stage}}.
 */
@ApplicationScoped
@LookupIfProperty(name = "verabank.role", stringValue = "scoring")
class ScoringService {

    record Outcome(BigDecimal score, Decision decision, String modelVersion) {
    }

    @Inject
    ModelRegistry registry;
    @Inject
    FeatureQueries queries;
    @Inject
    ScoreLog log;
    @Inject
    ShadowScorer shadow;
    @Inject
    ScoringMetrics metrics;
    @Inject
    ObjectMapper objectMapper;

    private final FeatureComputer computer = new FeatureComputer();
    private ScoreRequestParser parser;

    @PostConstruct
    void init() {
        parser = new ScoreRequestParser(objectMapper);
    }

    /** Valida el cuerpo y puntúa. Los errores salen como {@link ApiException} y se cuentan en {@code scoring.errors}. */
    Uni<Outcome> handle(String body) {
        ScoreInput input;
        try {
            input = parser.parse(body);
        } catch (ApiException e) {
            metrics.error("validation");
            throw e;
        }
        return score(input);
    }

    Uni<Outcome> score(ScoreInput in) {
        ModelState state = registry.state();
        if (!state.loaded()) {
            metrics.error("model_not_loaded");
            return Uni.createFrom().failure(ApiException.modelNotLoaded("El modelo no está cargado; el servicio no está listo"));
        }
        ModelState.Champion champion = state.champion();
        long start = System.nanoTime();
        return timed("log", log.find(in.transferId()))
                .flatMap(existing -> existing.isPresent()
                        ? Uni.createFrom().item(toOutcome(existing.get()))
                        : compute(in, champion))
                .onTermination().invoke((outcome, failure, cancelled) -> {
                    metrics.stage("total", start);
                    if (failure != null) {
                        metrics.error(failure instanceof ApiException api ? api.type() : "internal-error");
                    }
                });
    }

    private Uni<Outcome> compute(ScoreInput in, ModelState.Champion champion) {
        Windows windows = Windows.of(in.occurredAt());
        Uni<Map<String, Double>> features = timed("features",
                Uni.combine().all().unis(queries.destination(in, windows), queries.source(in, windows)).asTuple()
                        .map(t -> computer.compute(in, t.getItem1(), t.getItem2())));
        return features.flatMap(f -> {
            float[] row = FeatureComputer.row(f, champion.featureOrder());
            return timed("inference", infer(champion.model(), row)).flatMap(probability -> {
                DecisionPolicy.Scored scored = champion.policy().evaluate(probability);
                return timed("log", log.insert(in, f, scored, champion.modelVersion())).flatMap(inserted -> {
                    if (inserted.isPresent()) {
                        metrics.decision(scored.decision());
                        shadow.schedule(in.transferId(), row);
                        return Uni.createFrom().item(toOutcome(inserted.get()));
                    }
                    // Otra solicitud con el mismo transfer_id ganó la carrera: se devuelve lo que guardó.
                    return timed("log", log.find(in.transferId())).map(stored -> toOutcome(stored.orElseThrow()));
                });
            });
        });
    }

    /**
     * Calentamiento al arrancar: {@code rounds} pasadas por la ruta completa (lectura de idempotencia, las dos consultas
     * de features, inferencia, decisión e INSERT) con una solicitud sintética. El INSERT va en una transacción que se
     * revierte: no deja filas en la bitácora. No toca las métricas de etapa ni las de decisión.
     */
    Uni<Void> warmUp(int rounds) {
        ModelState state = registry.state();
        if (!state.loaded()) {
            return Uni.createFrom().failure(new IllegalStateException("el modelo no está cargado"));
        }
        ModelState.Champion champion = state.champion();
        Uni<Void> chain = Uni.createFrom().voidItem();
        for (int i = 0; i < rounds; i++) {
            chain = chain.flatMap(v -> warmRound(champion));
        }
        return chain;
    }

    /** Calienta cada conexión del pool con la ruta de scoring (lecturas y un INSERT revertido) sin inferencia. */
    Uni<Integer> warmConnections(int connections) {
        ModelState state = registry.state();
        if (!state.loaded()) {
            return Uni.createFrom().failure(new IllegalStateException("el modelo no está cargado"));
        }
        ModelState.Champion champion = state.champion();
        ScoreInput in = new ScoreInput(UUID.randomUUID(), "warmup-source", "warmup-dest", new BigDecimal("100.00"), "PEN",
                "TRANSFER", "PE", java.time.Instant.parse("2026-01-01T12:00:00Z"));
        Map<String, Double> features = new java.util.LinkedHashMap<>();
        FeatureComputer.FEATURE_NAMES.forEach(name -> features.put(name, 0.0));
        return log.warmConnections(connections, in, Windows.of(in.occurredAt()), features, champion.policy().evaluate(0.0),
                champion.modelVersion());
    }

    private static final String WARMUP_BODY = """
            {"transfer_id":"%s","source_ref":"warmup-source","dest_ref":"warmup-dest","amount":"100.00","currency":"PEN",
             "transfer_type":"TRANSFER","dest_country":"PE","occurred_at":"2026-01-01T12:00:00Z"}""";

    /** Una pasada de la ruta real menos la escritura: cuerpo JSON por el parser, features, inferencia, INSERT revertido y
     *  la respuesta serializada con el mismo ObjectMapper que la capa REST. */
    private Uni<Void> warmRound(ModelState.Champion champion) {
        ScoreInput in = parser.parse(WARMUP_BODY.formatted(UUID.randomUUID()));
        Windows windows = Windows.of(in.occurredAt());
        return log.find(in.transferId())
                .flatMap(existing -> Uni.combine().all().unis(queries.destination(in, windows), queries.source(in, windows))
                        .asTuple().map(t -> computer.compute(in, t.getItem1(), t.getItem2())))
                .flatMap(f -> {
                    float[] row = FeatureComputer.row(f, champion.featureOrder());
                    return infer(champion.model(), row).flatMap(probability -> {
                        DecisionPolicy.Scored scored = champion.policy().evaluate(probability);
                        return log.insertAndRollBack(in, f, scored, champion.modelVersion()).invoke(() -> serialize(
                                new ScoreResponse(scored.score(), scored.decision(), champion.modelVersion())));
                    });
                });
    }

    private void serialize(ScoreResponse response) {
        try {
            objectMapper.writeValueAsBytes(response);
        } catch (com.fasterxml.jackson.core.JsonProcessingException e) {
            throw new IllegalStateException("no se pudo serializar la respuesta del calentamiento", e);
        }
    }

    /** ONNX Runtime es bloqueante: va al pool de workers y la continuación vuelve al contexto de Vert.x. */
    private Uni<Double> infer(OnnxModel model, float[] row) {
        Context context = Vertx.currentContext();
        Uni<Double> inference = Uni.createFrom().item(() -> model.score(row))
                .runSubscriptionOn(Infrastructure.getDefaultWorkerPool());
        return context == null ? inference : inference.emitOn(task -> context.runOnContext(v -> task.run()));
    }

    private <T> Uni<T> timed(String stage, Uni<T> uni) {
        return Uni.createFrom().deferred(() -> {
            long start = System.nanoTime();
            return uni.onTermination().invoke(() -> metrics.stage(stage, start));
        });
    }

    private static Outcome toOutcome(ScoreLog.Stored stored) {
        return new Outcome(stored.score(), stored.decision(), stored.modelVersion());
    }

    static UUID idOf(ScoreInput in) {
        return in.transferId();
    }
}
