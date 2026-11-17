package com.verabank.transfer.scoring;

import java.math.BigDecimal;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

/** Scorer de la fase 1: siempre aprueba. Se activa con {@code verabank.scoring.mode=stub}; el real es RemoteScorer. */
@Component
@ConditionalOnProperty(prefix = "verabank.scoring", name = "mode", havingValue = "stub")
public class StubScorer implements FraudScorer {

    public static final String MODEL_VERSION = "stub-0";

    @Override
    public ScoreResult score(ScoreRequest request) {
        return new ScoreResult(BigDecimal.ZERO.setScale(5), Decision.APPROVE, DecisionSource.MODEL, MODEL_VERSION, 0);
    }
}
