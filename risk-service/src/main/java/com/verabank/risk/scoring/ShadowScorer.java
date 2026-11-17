package com.verabank.risk.scoring;

import io.quarkus.arc.lookup.LookupIfProperty;
import io.smallrye.mutiny.Uni;
import io.smallrye.mutiny.infrastructure.Infrastructure;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.inject.Inject;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.UUID;
import org.jboss.logging.Logger;

/**
 * Modelo en sombra (D8). Después de responder puntúa el challenger y escribe {@code shadow_score} solo si está
 * vacío. Un fallo se registra y se cuenta en {@code scoring.shadow.failures}; nunca cambia la respuesta.
 */
@ApplicationScoped
@LookupIfProperty(name = "verabank.role", stringValue = "scoring")
class ShadowScorer {

    private static final Logger LOG = Logger.getLogger(ShadowScorer.class);

    @Inject
    ModelRegistry registry;
    @Inject
    ScoreLog log;
    @Inject
    ScoringMetrics metrics;

    /** No bloquea al llamador: la inferencia va al pool de workers y el resultado no se espera. */
    void schedule(UUID transferId, float[] row) {
        OnnxModel challenger = registry.state().challenger();
        if (challenger == null) {
            return;
        }
        try {
            Uni.createFrom().item(() -> run(challenger, row))
                    .runSubscriptionOn(Infrastructure.getDefaultWorkerPool())
                    .flatMap(p -> log.updateShadow(transferId, round(p)))
                    .subscribe().with(updated -> { }, failure -> failed(transferId, failure));
        } catch (RuntimeException e) {
            failed(transferId, e);
        }
    }

    /** Punto de extensión de las pruebas: una inferencia del challenger. */
    double run(OnnxModel challenger, float[] row) {
        return challenger.score(row);
    }

    private void failed(UUID transferId, Throwable failure) {
        metrics.shadowFailure();
        LOG.warnf("falló el modelo en sombra para %s: %s", transferId, failure.toString());
    }

    private static BigDecimal round(double probability) {
        return new BigDecimal(probability).setScale(5, RoundingMode.HALF_EVEN);
    }
}
