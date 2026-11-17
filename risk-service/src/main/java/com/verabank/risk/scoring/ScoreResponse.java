package com.verabank.risk.scoring;

import com.fasterxml.jackson.annotation.JsonProperty;
import java.math.BigDecimal;

/** ScoreResponse de contracts/openapi.yaml: score con 5 decimales, decisión y versión del modelo. */
public record ScoreResponse(
        BigDecimal score,
        Decision decision,
        @JsonProperty("model_version") String modelVersion) {
}
