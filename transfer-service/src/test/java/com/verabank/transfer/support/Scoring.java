package com.verabank.transfer.support;

import static org.mockito.ArgumentMatchers.argThat;
import static org.mockito.Mockito.doReturn;

import com.verabank.transfer.scoring.Decision;
import com.verabank.transfer.scoring.DecisionSource;
import com.verabank.transfer.scoring.FraudScorer;
import com.verabank.transfer.scoring.ScoreResult;
import java.math.BigDecimal;

/** Decisiones del scorer por monto, sobre un {@code @MockitoSpyBean FraudScorer}. */
public final class Scoring {

    public static final BigDecimal BLOCK_AMOUNT = new BigDecimal("66.66");
    public static final BigDecimal REVIEW_AMOUNT = new BigDecimal("77.77");

    private Scoring() {
    }

    public static void blockAndReview(FraudScorer spy) {
        doReturn(new ScoreResult(new BigDecimal("0.99000"), Decision.BLOCK, DecisionSource.MODEL, "test", 3))
                .when(spy).score(argThat(r -> r.amount().compareTo(BLOCK_AMOUNT) == 0));
        doReturn(new ScoreResult(new BigDecimal("0.50000"), Decision.REVIEW, DecisionSource.FALLBACK, "test", 3))
                .when(spy).score(argThat(r -> r.amount().compareTo(REVIEW_AMOUNT) == 0));
    }
}
