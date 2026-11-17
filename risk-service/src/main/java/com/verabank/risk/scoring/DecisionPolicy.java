package com.verabank.risk.scoring;

import java.math.BigDecimal;

/** Strategy: de la probabilidad del modelo a score redondeado y decisión. */
public interface DecisionPolicy {

    /** Resultado de aplicar la política: la probabilidad original, el score redondeado y la decisión. */
    record Scored(double probability, BigDecimal score, Decision decision) {
    }

    Scored evaluate(double probability);
}
