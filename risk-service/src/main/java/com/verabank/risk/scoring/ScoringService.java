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
