package com.verabank.transfer.scoring;

/**
 * Strategy de scoring (§2.4). Debe ser idempotente por {@code transfer_id}: la saga puede
 * volver a pedir el score de una transferencia que ya lo pidió (§2.3).
 */
public interface FraudScorer {

    ScoreResult score(ScoreRequest request);
}
