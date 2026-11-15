package com.verabank.transfer.scoring;

import java.math.BigDecimal;

public record ScoreResult(
        BigDecimal score,
        Decision decision,
        DecisionSource source,
        String modelVersion,
        int latencyMs) {
}
