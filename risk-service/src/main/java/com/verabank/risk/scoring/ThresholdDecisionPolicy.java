package com.verabank.risk.scoring;

import java.math.BigDecimal;
import java.math.RoundingMode;

/**
 * Regla de servicio de ml/README.md, sección «Contrato con el servicio de scoring», (a): Python hace
 * {@code round(score, 5)}, que redondea al par sobre el valor decimal exacto del {@code double}. El equivalente
 * exacto es {@code new BigDecimal(double).setScale(5, HALF_EVEN)} (no {@code Math.round(x * 1e5) / 1e5}).
 * {@code BLOCK} si {@code score >= t_block}; si no, {@code REVIEW} si {@code score >= t_review}; si no, {@code APPROVE}.
 */
public final class ThresholdDecisionPolicy implements DecisionPolicy {

    private final BigDecimal reviewThreshold;
    private final BigDecimal blockThreshold;
    private final int decimals;

    public ThresholdDecisionPolicy(BigDecimal reviewThreshold, BigDecimal blockThreshold, int decimals) {
        this.reviewThreshold = reviewThreshold;
        this.blockThreshold = blockThreshold;
        this.decimals = decimals;
    }

    @Override
    public Scored evaluate(double probability) {
        if (!Double.isFinite(probability) || probability < 0 || probability > 1) {
            throw new IllegalArgumentException("probabilidad fuera de [0, 1]: " + probability);
        }
        BigDecimal score = new BigDecimal(probability).setScale(decimals, RoundingMode.HALF_EVEN);
        Decision decision;
        if (score.compareTo(blockThreshold) >= 0) {
            decision = Decision.BLOCK;
        } else if (score.compareTo(reviewThreshold) >= 0) {
            decision = Decision.REVIEW;
        } else {
            decision = Decision.APPROVE;
        }
        return new Scored(probability, score, decision);
    }
}
