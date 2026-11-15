package com.verabank.transfer.scoring;

import java.math.BigDecimal;
import org.springframework.stereotype.Component;

/** Scorer de la fase 1: siempre aprueba. El scoring real (RemoteScorer) llega en la fase 4. */
@Component
public class StubScorer implements FraudScorer {

    public static final String MODEL_VERSION = "stub-0";

    @Override
    public ScoreResult score(ScoreRequest request) {
        return new ScoreResult(BigDecimal.ZERO.setScale(5), Decision.APPROVE, DecisionSource.MODEL, MODEL_VERSION, 0);
    }
}
