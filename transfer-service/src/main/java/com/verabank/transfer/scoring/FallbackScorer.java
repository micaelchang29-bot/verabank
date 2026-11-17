package com.verabank.transfer.scoring;

import java.math.BigDecimal;

/**
 * Decisión de respaldo (ADR-04): sin modelo no se rechaza ni se aprueba todo. Hasta {@code fallback-limit} aprueba y
 * un monto mayor pasa a revisión. D7: sin score y con {@code model_version = 'fallback-v1'}.
 */
public final class FallbackScorer implements FraudScorer {

    public static final String MODEL_VERSION = "fallback-v1";

    private final BigDecimal limit;

    public FallbackScorer(BigDecimal limit) {
        this.limit = limit;
    }

    @Override
    public ScoreResult score(ScoreRequest request) {
        Decision decision = request.amount().compareTo(limit) <= 0 ? Decision.APPROVE : Decision.REVIEW;
        return new ScoreResult(null, decision, DecisionSource.FALLBACK, MODEL_VERSION, 0);
    }
}
