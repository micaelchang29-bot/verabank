package com.verabank.risk.scoring;

import java.math.BigDecimal;
import java.util.List;

/** Lo que el servicio usa de ml/artifacts/thresholds.json. */
record Thresholds(
        String modelVersion,
        String modelSha256,
        List<String> featureOrder,
        BigDecimal reviewThreshold,
        BigDecimal blockThreshold,
        int roundingDecimals) {
}
